"""S0-T6 RED: a real PostgreSQL empty Run/EvalRun seals to a verified artifact."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from zhiwei.cli.main import app
from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.runs import (
    CreateEvalRunCommand,
    EvalFoundationService,
    EvalStateError,
    SealEmptyCommand,
)
from zhiwei.evals.sealing import EvalSealRefused, verify_sealed_artifact
from zhiwei.object_store.manifests import ArtifactVerificationError
from zhiwei.object_store.ports import ObjectNamespace
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.events import event_data_from_row, verify_event_chain
from zhiwei.persistence.models import (
    ArtifactManifest,
    AuditEvent,
    CanonicalEvent,
    CanonicalProjection,
    DatasetVersion,
    EvalRun,
    EvalSample,
    EvalSuiteVersion,
    OutboxMessage,
    Run,
)
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
CLI_RUNNER = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


def _cli_env(object_root: Path) -> dict[str, str | None]:
    return {
        "ZHIWEI_PROFILE": "test",
        "ZHIWEI_RELEASE_MODE": "fixture_only",
        "ZHIWEI_DATABASE_URL": APP_URL,
        "ZHIWEI_OBJECT_STORE_ROOT": str(object_root),
        "OPENAI_API_KEY": None,
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "OPENAI_MODEL": None,
    }


@pytest.fixture
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    refused: list[object] = []

    def guard_connect(self: socket.socket, address: object) -> object:
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == 55432:
            return original_connect(self, address)
        refused.append(address)
        raise AssertionError(f"eval CLI attempted external network access: {address!r}")

    def guard_connect_ex(self: socket.socket, address: object) -> int:
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == 55432:
            return original_connect_ex(self, address)
        refused.append(address)
        raise AssertionError(f"eval CLI attempted external network access: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)
    return refused


def test_seal_empty_cli_check_executes_and_verifies_the_real_flow(
    tmp_path: Path, no_external_network: list[object]
) -> None:
    result = CLI_RUNNER.invoke(
        app,
        ["eval", "seal-empty", "--check"],
        env=_cli_env(tmp_path / "objects"),
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "fixture"
    assert payload["run_status"] == "succeeded"
    assert payload["eval_run_status"] == "sealed"
    assert payload["registered_units"] == 0
    assert payload["verified"] is True
    for field in ("run_id", "eval_run_id", "manifest_id", "seal_digest"):
        assert payload[field]
    assert no_external_network == []


def test_legacy_cli_run_and_seal_execute_registered_units_without_live_model(
    tmp_path: Path,
    no_external_network: list[object],
) -> None:
    env = _cli_env(tmp_path / "objects")
    run_result = CLI_RUNNER.invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "legacy-assets",
            "--executor",
            "legacy",
            "--mode",
            "offline",
        ],
        env=env,
    )
    assert run_result.exit_code == 0, run_result.output
    assert TRACEBACK_MARKER not in run_result.output
    run_payload = json.loads(run_result.stdout)
    assert run_payload["mode"] == "offline"
    assert run_payload["executor"] == "legacy"
    assert run_payload["registered_units"] > 0
    assert run_payload["terminal_units"] == run_payload["registered_units"]

    seal_result = CLI_RUNNER.invoke(
        app,
        [
            "eval",
            "seal",
            run_payload["eval_run_id"],
            "--organization-id",
            run_payload["organization_id"],
            "--workspace-id",
            run_payload["workspace_id"],
        ],
        env=env,
    )
    assert seal_result.exit_code == 0, seal_result.output
    assert TRACEBACK_MARKER not in seal_result.output
    seal_payload = json.loads(seal_result.stdout)
    assert seal_payload["eval_run_status"] == "sealed"
    assert seal_payload["verified"] is True
    assert no_external_network == []


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[
    tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        PosixObjectStore,
    ]
]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="T6")
    try:
        yield engine, sessions, context, PosixObjectStore(tmp_path / "objects")
    finally:
        await engine.dispose()


def _namespace(context: TenantContext) -> ObjectNamespace:
    assert context.workspace_id is not None
    return ObjectNamespace(
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


def _seal_empty_command() -> SealEmptyCommand:
    return SealEmptyCommand(
        mode=EvalMode.FIXTURE,
        code_digest=digest_bytes(b"code revision"),
        config_digest=digest_bytes(b"test config"),
        schema_digest=digest_bytes(b"0001 schema"),
        migration_revision="0001_foundation",
        test_report={
            "status": "passed",
            "scope": "focused",
            "command": "pytest tests/integration/foundation/test_empty_run.py",
        },
    )


@pytest.mark.asyncio
async def test_empty_run_persists_and_seals_a_recomputable_artifact(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, store = database
    async with tenant_session(sessions, context) as session:
        result = await EvalFoundationService(session, context, store).seal_empty(
            _seal_empty_command()
        )

    async with tenant_session(sessions, context) as session:
        run = await session.get(Run, result.run_id)
        eval_run = await session.get(EvalRun, result.eval_run_id)
        dataset = await session.get(DatasetVersion, result.dataset_version_id)
        suite = await session.get(EvalSuiteVersion, result.eval_suite_version_id)
        seal_manifest = await session.get(ArtifactManifest, result.manifest_id)
        dataset_manifest = await session.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.owner_resource_type == "dataset_version",
                ArtifactManifest.owner_resource_id == result.dataset_version_id,
            )
        )
        test_report_manifest = await session.get(
            ArtifactManifest, result.test_report_manifest_id
        )
        samples = (
            await session.scalars(
                select(EvalSample).where(EvalSample.eval_run_id == result.eval_run_id)
            )
        ).all()
        events = (
            await session.scalars(
                select(CanonicalEvent).where(CanonicalEvent.run_id == result.run_id)
            )
        ).all()
        projection = await session.get(CanonicalProjection, result.run_id)
        audits = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.resource_type == "run",
                    AuditEvent.resource_id == result.run_id,
                )
            )
        ).all()
        outbox = (
            await session.scalars(
                select(OutboxMessage).where(OutboxMessage.organization_id == context.organization_id)
            )
        ).all()

    assert run is not None and run.status == "succeeded"
    assert eval_run is not None and eval_run.status == "sealed" and eval_run.sealed_at is not None
    assert dataset is not None and dataset.status == "frozen" and dataset.manifest_id is not None
    assert suite is not None and suite.status == "frozen"
    assert dataset_manifest is not None and dataset.manifest_id == dataset_manifest.id
    assert dataset_manifest.owner_resource_type == "dataset_version"
    assert dataset_manifest.owner_resource_id == result.dataset_version_id
    assert dataset_manifest.artifact_schema_id == "eval.dataset"
    assert dataset_manifest.content_digest == dataset.content_digest
    assert test_report_manifest is not None
    assert test_report_manifest.owner_resource_type == "eval_test_report"
    assert test_report_manifest.owner_resource_id == result.eval_run_id
    assert test_report_manifest.artifact_schema_id == "gate.test-report"
    assert seal_manifest is not None
    assert seal_manifest.owner_resource_type == "eval_run"
    assert seal_manifest.owner_resource_id == result.eval_run_id
    assert seal_manifest.artifact_schema_id == "eval.sealed-run"
    assert seal_manifest.content_digest == result.seal_digest
    assert samples == []
    assert [event.event_type for event in events] == ["eval.run.sealed"]
    assert events[0].payload["eval_run_id"] == str(result.eval_run_id)
    assert events[0].payload["manifest_id"] == str(result.manifest_id)
    assert events[0].payload["seal_digest"] == result.seal_digest
    verify_event_chain([event_data_from_row(event) for event in events])
    assert projection is not None
    assert projection.head_event_digest == events[0].event_digest
    assert projection.sequence_no == 1
    assert len(audits) == len(outbox) == 1
    assert audits[0].payload_digest == events[0].event_digest
    assert outbox[0].event_key == str(events[0].id)
    assert outbox[0].payload["event_digest"] == events[0].event_digest

    dataset_bytes = b"".join(
        store.read_immutable(_namespace(context), dataset_manifest.object_key)
    )
    assert digest_bytes(dataset_bytes) == dataset.content_digest
    dataset_payload = json.loads(dataset_bytes)
    assert canonical_json(dataset_payload) == dataset_bytes
    assert dataset_payload["registered_units"] == []

    test_report_bytes = b"".join(
        store.read_immutable(_namespace(context), test_report_manifest.object_key)
    )
    assert digest_bytes(test_report_bytes) == test_report_manifest.content_digest
    test_report_payload = json.loads(test_report_bytes)
    assert canonical_json(test_report_payload) == test_report_bytes
    assert test_report_payload == _seal_empty_command().test_report

    seal_bytes = b"".join(
        store.read_immutable(_namespace(context), seal_manifest.object_key)
    )
    assert digest_bytes(seal_bytes) == result.seal_digest
    payload = json.loads(seal_bytes)
    assert canonical_json(payload) == seal_bytes
    verified = verify_sealed_artifact(payload, result.seal_digest)
    assert verified.run_id == result.run_id
    assert verified.eval_run_id == result.eval_run_id
    assert verified.registered_units == ()
    assert verified.samples == ()
    assert verified.code_digest == eval_run.code_digest
    assert verified.config_digest == eval_run.config_digest
    assert verified.schema_digest == eval_run.schema_digest
    assert verified.migration_revision == "0001_foundation"
    assert verified.dataset_manifest_id == dataset_manifest.id
    assert verified.test_report_manifest_id == test_report_manifest.id
    assert verified.test_report_digest == test_report_manifest.content_digest


@pytest.mark.parametrize("corrupt_target", ["dataset", "test-report", "seal"])
@pytest.mark.asyncio
async def test_sealed_eval_verification_fails_for_corrupt_object(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        PosixObjectStore,
    ],
    corrupt_target: str,
) -> None:
    _, sessions, context, store = database
    async with tenant_session(sessions, context) as session:
        result = await EvalFoundationService(session, context, store).seal_empty(
            _seal_empty_command()
        )
    target_id = {
        "dataset": result.dataset_manifest_id,
        "test-report": result.test_report_manifest_id,
        "seal": result.manifest_id,
    }[corrupt_target]
    async with tenant_session(sessions, context) as session:
        manifest = await session.get(ArtifactManifest, target_id)
    assert manifest is not None
    store.debug_replace_immutable(_namespace(context), manifest.object_key, b"tampered")

    with pytest.raises(ArtifactVerificationError):
        async with tenant_session(sessions, context) as session:
            await EvalFoundationService(session, context, store).verify_sealed(
                result.eval_run_id
            )


@pytest.mark.asyncio
async def test_partial_run_cannot_seal_and_resume_keeps_the_frozen_registry(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, store = database
    units = (
        RegisteredUnit(sample_id="sample-1", unit_id="unit-1"),
        RegisteredUnit(sample_id="sample-2", unit_id="unit-2"),
    )
    create = CreateEvalRunCommand(
        mode=EvalMode.FIXTURE,
        registered_units=units,
        dataset_payload={"samples": ["sample-1", "sample-2"]},
        code_digest=digest_bytes(b"code"),
        config_digest=digest_bytes(b"config"),
        schema_digest=digest_bytes(b"schema"),
    )
    async with tenant_session(sessions, context) as session:
        created = await EvalFoundationService(session, context, store).create(create)

    with pytest.raises(EvalStateError, match="registered"):
        async with tenant_session(sessions, context) as session:
            await EvalFoundationService(session, context, store).record_outcome(
                created.eval_run_id,
                SampleOutcome(
                    unit=RegisteredUnit(sample_id="unknown", unit_id="unknown"),
                    status=SampleStatus.COMPLETED,
                    result={"ok": False},
                ),
            )
    async with tenant_session(sessions, context) as session:
        service = EvalFoundationService(session, context, store)
        await service.record_outcome(
            created.eval_run_id,
            SampleOutcome(
                unit=units[0],
                status=SampleStatus.COMPLETED,
                result={"ok": True},
            ),
        )
        await service.pause(created.eval_run_id)

    with pytest.raises(EvalStateError, match="already"):
        async with tenant_session(sessions, context) as session:
            await EvalFoundationService(session, context, store).record_outcome(
                created.eval_run_id,
                SampleOutcome(
                    unit=units[0],
                    status=SampleStatus.COMPLETED,
                    result={"ok": "different"},
                ),
            )

    with pytest.raises(EvalSealRefused, match="terminal"):
        async with tenant_session(sessions, context) as session:
            await EvalFoundationService(session, context, store).seal(
                created.eval_run_id,
                migration_revision="0001_foundation",
                test_report={"status": "partial", "scope": "focused"},
            )

    async with tenant_session(sessions, context) as session:
        eval_run = await session.get(EvalRun, created.eval_run_id)
        before_resume = (
            await session.scalars(
                select(EvalSample)
                .where(EvalSample.eval_run_id == created.eval_run_id)
                .order_by(EvalSample.sample_id, EvalSample.unit_id)
            )
        ).all()
        eval_manifests = (
            await session.scalars(
            select(ArtifactManifest).where(
                ArtifactManifest.owner_resource_id == created.eval_run_id,
            )
            )
        ).all()
        projection = await session.get(CanonicalProjection, created.run_id)
        events = (
            await session.scalars(
                select(CanonicalEvent).where(CanonicalEvent.run_id == created.run_id)
            )
        ).all()
        audits = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.resource_type == "run",
                    AuditEvent.resource_id == created.run_id,
                )
            )
        ).all()
        messages = (
            await session.scalars(
                select(OutboxMessage).where(
                    OutboxMessage.organization_id == context.organization_id
                )
            )
        ).all()
    assert eval_run is not None and eval_run.status == "partial"
    assert [row.status for row in before_resume] == ["completed", "registered"]
    assert eval_manifests == []
    assert projection is None
    assert events == audits == messages == []

    async with tenant_session(sessions, context) as session:
        await EvalFoundationService(session, context, store).resume(created.eval_run_id)
    async with tenant_session(sessions, context) as session:
        after_resume = (
            await session.scalars(
                select(EvalSample)
                .where(EvalSample.eval_run_id == created.eval_run_id)
                .order_by(EvalSample.sample_id, EvalSample.unit_id)
            )
        ).all()
    assert [(row.sample_id, row.unit_id, row.status) for row in after_resume] == [
        ("sample-1", "unit-1", "completed"),
        ("sample-2", "unit-2", "registered"),
    ]


@pytest.mark.asyncio
async def test_record_outcome_and_seal_serialize_on_the_eval_run_lock(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, store = database
    unit = RegisteredUnit(sample_id="concurrent-sample", unit_id="concurrent-unit")
    async with tenant_session(sessions, context) as session:
        created = await EvalFoundationService(session, context, store).create(
            CreateEvalRunCommand(
                mode=EvalMode.FIXTURE,
                registered_units=(unit,),
                dataset_payload={"samples": [unit.sample_id]},
                code_digest=digest_bytes(b"concurrent code"),
                config_digest=digest_bytes(b"concurrent config"),
                schema_digest=digest_bytes(b"concurrent schema"),
            )
        )

    async def record() -> None:
        async with tenant_session(sessions, context) as session:
            await EvalFoundationService(session, context, store).record_outcome(
                created.eval_run_id,
                SampleOutcome(
                    unit=unit,
                    status=SampleStatus.FAILED,
                    result={"reason": "fixture failure"},
                ),
            )

    async def seal() -> UUID:
        async with tenant_session(sessions, context) as session:
            sealed = await EvalFoundationService(session, context, store).seal(
                created.eval_run_id,
                migration_revision="0001_foundation",
                test_report={"status": "passed", "scope": "concurrency"},
            )
            return sealed.manifest_id

    async with tenant_session(sessions, context) as blocker:
        await blocker.execute(
            select(EvalRun)
            .where(EvalRun.id == created.eval_run_id)
            .with_for_update()
        )
        record_task = asyncio.create_task(record())
        await asyncio.sleep(0.05)
        seal_task = asyncio.create_task(seal())
        await asyncio.sleep(0.05)
        assert not record_task.done()
        assert not seal_task.done()

    record_result, seal_result = await asyncio.gather(
        record_task, seal_task, return_exceptions=True
    )
    assert record_result is None
    if isinstance(seal_result, EvalSealRefused):
        async with tenant_session(sessions, context) as session:
            retried = await EvalFoundationService(session, context, store).seal(
                created.eval_run_id,
                migration_revision="0001_foundation",
                test_report={"status": "passed", "scope": "concurrency"},
            )
            manifest_id = retried.manifest_id
    else:
        assert isinstance(seal_result, UUID)
        manifest_id = seal_result
    async with tenant_session(sessions, context) as session:
        eval_run = await session.get(EvalRun, created.eval_run_id)
        sample = await session.scalar(
            select(EvalSample).where(EvalSample.eval_run_id == created.eval_run_id)
        )
        manifest = await session.get(ArtifactManifest, manifest_id)
    assert eval_run is not None and eval_run.status == "sealed"
    assert sample is not None and sample.status == "failed"
    assert manifest is not None and manifest.owner_resource_id == created.eval_run_id


@pytest.mark.asyncio
async def test_seal_refuses_when_dataset_object_tampered_after_freeze(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        PosixObjectStore,
    ],
) -> None:
    """S2 修复轮批次 C RED（S0 不变量，ADR-12 复审）：seal 前复验 dataset 对象。

    specs/s0 §4：artifact digest mismatch / missing object 不得 seal。_seal 此前
    只信 dataset_version.content_digest（DB 信任链），不读对象字节——create 与
    seal 之间对象被篡改/删除时仍会 sealed。
    """
    _, sessions, context, store = database
    units = (RegisteredUnit(sample_id="s1", unit_id="u1"),)
    create = CreateEvalRunCommand(
        mode=EvalMode.FIXTURE,
        registered_units=units,
        dataset_payload={"samples": ["s1"]},
        code_digest=digest_bytes(b"code"),
        config_digest=digest_bytes(b"config"),
        schema_digest=digest_bytes(b"schema"),
    )
    async with tenant_session(sessions, context) as session:
        created = await EvalFoundationService(session, context, store).create(create)
        await EvalFoundationService(session, context, store).record_outcome(
            created.eval_run_id,
            SampleOutcome(
                unit=units[0],
                status=SampleStatus.COMPLETED,
                result={"ok": True},
            ),
        )

    # create 与 seal 之间篡改已冻结的 dataset 对象
    async with tenant_session(sessions, context) as session:
        manifest = await session.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.owner_resource_type == "dataset_version",
                ArtifactManifest.owner_resource_id == created.dataset_version_id,
            )
        )
    assert manifest is not None
    store.debug_replace_immutable(_namespace(context), manifest.object_key, b"tampered")

    with pytest.raises(ArtifactVerificationError):
        async with tenant_session(sessions, context) as session:
            await EvalFoundationService(session, context, store).seal(
                created.eval_run_id,
                migration_revision="0011_approval_requests",
                test_report={"status": "ok"},
            )

    # seal 被拒后 eval run 不得处于 sealed 态
    async with tenant_session(sessions, context) as session:
        eval_run = await session.get(EvalRun, created.eval_run_id)
    assert eval_run is not None and eval_run.status != "sealed"
