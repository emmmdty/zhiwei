"""S9 R2-B RED: security-v1 suite 在真实 PostgreSQL 上的 create → execute → seal 全链路。

每个注册单位必须是安全 PASS（生产路径正确拒绝）；负例对照翻转一个 fixture 输入为
应放行的形态，断言该单位正确判 fail（判分器可区分，非常数通过）。密封仍可进行：
failed 是终态——密封语义与安全语义正交，密封件如实记录失败单位。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import EvalMode, SampleStatus
from zhiwei.evals.executors.security import SecurityGateExecutor
from zhiwei.evals.runs import CreateEvalRunCommand, EvalFoundationService
from zhiwei.evals.security_suites import SECURITY_V1, resolve_security_suite
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
MIGRATION_REVISION = "0015_release_claims"

DatabaseFixture = tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    TenantContext,
    PosixObjectStore,
]


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
        await repository.create_workspace(workspace_id, name="S9-R2B")
    try:
        yield engine, sessions, context, PosixObjectStore(tmp_path / "objects")
    finally:
        await engine.dispose()


def _dataset_payload() -> dict[str, object]:
    suite = resolve_security_suite(SECURITY_V1)
    return {
        "suite": suite.name,
        "executor": suite.executor_kind,
        "production_path": suite.production_path,
        "registered_units": [
            {"sample_id": unit.sample_id, "unit_id": unit.unit_id}
            for unit in suite.registered_units
        ],
    }


async def _seal_suite(
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
    store: PosixObjectStore,
    *,
    fixture_overrides: dict[str, dict[str, object]] | None,
) -> tuple[dict[str, SampleStatus], str | None]:
    suite = resolve_security_suite(SECURITY_V1)
    executor = SecurityGateExecutor(suite, fixture_overrides=fixture_overrides)
    outcomes = [await executor.execute(unit) for unit in suite.registered_units]
    async with tenant_session(sessions, context) as session:
        service = EvalFoundationService(session, context, store)
        created = await service.create(
            CreateEvalRunCommand(
                mode=EvalMode.OFFLINE,
                registered_units=suite.registered_units,
                dataset_payload=_dataset_payload(),
                code_digest=digest_bytes(b"code"),
                config_digest=digest_bytes(b"config"),
                schema_digest=digest_bytes(b"schema"),
            )
        )
        for outcome in outcomes:
            await service.record_outcome(created.eval_run_id, outcome)
        sealed = await service.seal(
            created.eval_run_id,
            migration_revision=MIGRATION_REVISION,
            test_report={
                "status": "passed"
                if all(o.status is SampleStatus.COMPLETED for o in outcomes)
                else "failed",
                "scope": SECURITY_V1,
                "command": f"zhiwei eval run --suite {SECURITY_V1} --mode offline --seal",
            },
        )
        await service.verify_sealed(created.eval_run_id)
        return {o.unit.sample_id: o.status for o in outcomes}, sealed.seal_digest


@pytest.mark.asyncio
async def test_security_suite_seals_with_all_units_passing(
    database: DatabaseFixture,
) -> None:
    _, sessions, context, store = database
    statuses, seal_digest = await _seal_suite(
        sessions, context, store, fixture_overrides=None
    )
    suite = resolve_security_suite(SECURITY_V1)
    assert set(statuses) == {u.sample_id for u in suite.registered_units}
    # 每个 unit 都是安全 PASS：生产路径按预期拒绝/围栏（pass = 正确 refused/contained）。
    assert all(status is SampleStatus.COMPLETED for status in statuses.values())
    assert seal_digest is not None and seal_digest.startswith("sha256:")


@pytest.mark.asyncio
async def test_negative_control_unit_fails_but_seal_still_records_it(
    database: DatabaseFixture,
) -> None:
    """负例对照：翻转 poisoning fixture 为良性形态 → 该单位正确判 fail，其余照常 PASS。"""
    _, sessions, context, store = database
    statuses, seal_digest = await _seal_suite(
        sessions,
        context,
        store,
        fixture_overrides={
            "memory-poisoning/tool-instruction-refused": {
                "canonical_value": "editor theme dark"
            }
        },
    )
    flipped = statuses["memory-poisoning/tool-instruction-refused"]
    assert flipped is SampleStatus.FAILED
    for sample_id, status in statuses.items():
        if sample_id != "memory-poisoning/tool-instruction-refused":
            assert status is SampleStatus.COMPLETED, sample_id
    # failed 是终态：密封仍成立（密封记录失败，不伪造成功）。
    assert seal_digest is not None and seal_digest.startswith("sha256:")
