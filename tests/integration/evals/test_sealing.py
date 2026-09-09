"""S9-T2 GREEN 目标（RED 先行）：真实 PostgreSQL 上的 runner → seal → verify → report 全链路。

覆盖：EvalRunner 经 EvalFoundationService.record_outcome 驱动 pending 单位（含
provider error / refusal 故障注入），确定性 scorer 产出 pass/fail 终态；service seal
（无 usage 块，向后兼容）与 sealing 层 usage-sealed artifact（七项 ROI 指标参与
digest）双路径均可 verify 并出报告；分母完整、digest 稳定。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.reports import EvalReportScopeInput, build_eval_report
from zhiwei.evals.runner import EvalRunner, MappingReferenceLookup
from zhiwei.evals.runs import CreateEvalRunCommand, EvalRunState, SealedEvalRun
from zhiwei.evals.scorers.generic import ExactMatchScorer
from zhiwei.evals.sealing import build_sealed_artifact, verify_sealed_artifact
from zhiwei.models.usage import RunUsageSnapshot, TokenWeights, compute_run_usage
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
ADMIN_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
MIGRATION_REVISION = "0014_cost_ledger"

UNITS = (
    RegisteredUnit(sample_id="s-1", unit_id="u-1"),
    RegisteredUnit(sample_id="s-2", unit_id="u-1"),
    RegisteredUnit(sample_id="s-3", unit_id="u-1"),
    RegisteredUnit(sample_id="s-4", unit_id="u-1"),
)

DatabaseFixture = tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    TenantContext,
    PosixObjectStore,
]


class _ScriptedExecutor:
    """按 sample_id 脚本化返回结果或抛异常（故障注入：provider error）。"""

    def __init__(self, script: Mapping[str, SampleOutcome | Exception]) -> None:
        self._script = dict(script)

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        action = self._script[unit.sample_id]
        if isinstance(action, Exception):
            raise action
        return action


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[DatabaseFixture]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="S9-T2")
    try:
        yield engine, sessions, context, PosixObjectStore(tmp_path / "objects")
    finally:
        await engine.dispose()


def _create_command() -> CreateEvalRunCommand:
    return CreateEvalRunCommand(
        mode=EvalMode.OFFLINE,
        registered_units=UNITS,
        dataset_payload={"samples": [unit.sample_id for unit in UNITS]},
        code_digest=digest_bytes(b"code"),
        config_digest=digest_bytes(b"config"),
        schema_digest=digest_bytes(b"schema"),
    )


def _scripted_executor() -> _ScriptedExecutor:
    return _ScriptedExecutor(
        {
            "s-1": SampleOutcome(
                unit=UNITS[0],
                status=SampleStatus.COMPLETED,
                result={"answer": "42"},
            ),
            "s-2": SampleOutcome(
                unit=UNITS[1],
                status=SampleStatus.COMPLETED,
                result={"answer": "41"},
            ),
            "s-3": RuntimeError("provider connection reset"),
            "s-4": SampleOutcome(
                unit=UNITS[3],
                status=SampleStatus.REFUSED,
                result={"reason": "policy_refusal"},
            ),
        }
    )


def _reference_lookup() -> MappingReferenceLookup:
    return MappingReferenceLookup(
        {
            ("s-1", "u-1"): {"answer": "42"},
            ("s-2", "u-1"): {"answer": "42"},
        }
    )


def _usage() -> RunUsageSnapshot:
    return RunUsageSnapshot(
        total_new_input_tokens=500,
        total_cache_read_tokens=200,
        total_output_tokens=1000,
        authoritative_tokens_sent=300,
        total_tokens_sent=700,
        verified_evidence_count=2,
        recoverable_reload_tokens=50,
        context_window=8000,
        compression_input_tokens=400,
        compression_output_tokens=100,
        completed_task_count=1,
        weights=TokenWeights(),
    )


def _scope() -> EvalReportScopeInput:
    return EvalReportScopeInput(
        model="internal-llm",
        version="agent-2026.09",
        date="2026-09-05",
        corpus="internal-120",
        environment="offline",
    )


@pytest.mark.asyncio
async def test_runner_seal_verify_report_full_path(database: DatabaseFixture) -> None:
    _, sessions, context, store = database

    async with tenant_session(sessions, context) as session:
        runner = EvalRunner(
            session,
            context,
            store,
            _scripted_executor(),
            scorer=ExactMatchScorer(output_field="answer", reference_field="answer"),
            references=_reference_lookup(),
        )
        created = await runner.create(_create_command())
        outcomes = await runner.execute_pending(created.eval_run_id)
        await runner.pause(created.eval_run_id)
        sealed = await runner.seal(
            created.eval_run_id,
            migration_revision=MIGRATION_REVISION,
            test_report={"status": "passed", "scope": "s9-t2"},
        )
        artifact = await runner.verify_sealed(created.eval_run_id)

    assert isinstance(sealed, SealedEvalRun)
    assert sealed.terminal_units == 4
    # scorer 只对 COMPLETED 生效：s-1 通过、s-2 不通过；refused/error 不伪造质量信号。
    assert [(item.unit.sample_id, item.status) for item in outcomes] == [
        ("s-1", SampleStatus.COMPLETED),
        ("s-2", SampleStatus.COMPLETED),
        ("s-3", SampleStatus.ERROR),
        ("s-4", SampleStatus.REFUSED),
    ]
    assert outcomes[0].result["passed"] is True
    assert outcomes[1].result["passed"] is False
    assert outcomes[2].result["reason"] == "executor_error"
    assert outcomes[3].result == {"reason": "policy_refusal"}

    # service seal 路径无 usage 块（向后兼容），artifact 可独立复核。
    assert artifact.usage_metrics is None
    assert [sample.sample_id for sample in artifact.samples] == [
        "s-1",
        "s-2",
        "s-3",
        "s-4",
    ]

    report, report_digest = build_eval_report(
        artifact,
        outcomes,
        seal_digest=sealed.seal_digest,
        scope=_scope(),
    )
    entry = report.quality[0]
    assert entry.n == 4
    assert entry.successes == 1
    assert entry.estimate == pytest.approx(0.25)
    assert entry.denominator.n_total == 4
    assert entry.denominator.n_completed == 2
    assert entry.denominator.n_failed == 0
    assert entry.denominator.n_refused == 1
    assert entry.denominator.n_error == 1
    assert report_digest.startswith("sha256:")


@pytest.mark.asyncio
async def test_usage_sealed_artifact_is_stable_and_reportable(database: DatabaseFixture) -> None:
    _, sessions, context, store = database
    async with tenant_session(sessions, context) as session:
        runner = EvalRunner(
            session,
            context,
            store,
            _scripted_executor(),
            scorer=ExactMatchScorer(output_field="answer", reference_field="answer"),
            references=_reference_lookup(),
        )
        created = await runner.create(_create_command())
        outcomes = await runner.execute_pending(created.eval_run_id)
        state = await runner.load_state(created.eval_run_id)
        assert isinstance(state, EvalRunState)

    # manifest id 取固定值：digest 稳定性断言要求两次构建的输入完全一致。
    dataset_manifest_id = UUID("55555555-5555-4555-8555-555555555555")
    test_report_manifest_id = UUID("66666666-6666-4666-8666-666666666666")
    _baseline_artifact, baseline_digest = build_sealed_artifact(
        run_id=created.run_id,
        eval_run_id=created.eval_run_id,
        state=state,
        dataset_digest=digest_bytes(b"dataset"),
        dataset_manifest_id=dataset_manifest_id,
        suite_digest=digest_bytes(b"suite"),
        migration_revision=MIGRATION_REVISION,
        test_report_digest=digest_bytes(b"report"),
        test_report_manifest_id=test_report_manifest_id,
    )
    usage_artifact, usage_digest = build_sealed_artifact(
        run_id=created.run_id,
        eval_run_id=created.eval_run_id,
        state=state,
        dataset_digest=digest_bytes(b"dataset"),
        dataset_manifest_id=dataset_manifest_id,
        suite_digest=digest_bytes(b"suite"),
        migration_revision=MIGRATION_REVISION,
        test_report_digest=digest_bytes(b"report"),
        test_report_manifest_id=test_report_manifest_id,
        usage=_usage(),
    )
    _repeat_artifact, repeat_digest = build_sealed_artifact(
        run_id=created.run_id,
        eval_run_id=created.eval_run_id,
        state=state,
        dataset_digest=digest_bytes(b"dataset"),
        dataset_manifest_id=dataset_manifest_id,
        suite_digest=digest_bytes(b"suite"),
        migration_revision=MIGRATION_REVISION,
        test_report_digest=digest_bytes(b"report"),
        test_report_manifest_id=test_report_manifest_id,
        usage=_usage(),
    )

    assert repeat_digest == usage_digest
    assert usage_digest != baseline_digest
    metrics = usage_artifact.canonical_mapping()["usage_metrics"]
    # 指标值必须与 compute_run_usage 复算一致（seal 不发明第二套算法）。
    assert metrics == {
        name: float(value)
        for name, value in compute_run_usage(_usage()).model_dump().items()
    }

    verified = verify_sealed_artifact(usage_artifact.canonical_mapping(), usage_digest)
    assert verified.usage_metrics is not None
    assert verified.usage_metrics["authoritative_token_share"] == pytest.approx(300 / 700)

    report, _ = build_eval_report(
        verified,
        outcomes,
        seal_digest=usage_digest,
        scope=_scope(),
    )
    assert report.quality[0].n == 4
