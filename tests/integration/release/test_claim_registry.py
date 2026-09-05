"""S9-T4 集成：Claim Registry 由真实密封 EvalRun 驱动升级（真实 PG + object store）。

- 升级证据不经调用方转述：服务从 object store 独立复算密封件，把复算 digest
  作为 verified_seal_digest 喂给域层状态机；
- offline 密封件只能把 claim 升到 offline_verified；live_verified 需要 live/shadow；
- 错 eval_run_id（不存在/跨租户）一律拒绝——RLS 让跨租户密封件「不存在」；
- 状态升级落库：新会话读回 status/evidence 与升级结果一致。
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
from zhiwei.agents.claims import (
    ClaimAlreadyRegistered,
    ClaimNotFound,
    ClaimRegistryService,
    ClaimScope,
    ClaimStatus,
    ClaimUpgradeDenied,
)

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.runs import (
    CreateEvalRunCommand,
    EvalFoundationService,
    EvalRunNotFound,
    SealedEvalRun,
)
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_URL = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)
MIGRATION_REVISION = "0014_cost_ledger"

UNITS = (
    RegisteredUnit(sample_id="s-1", unit_id="u-1"),
    RegisteredUnit(sample_id="s-2", unit_id="u-1"),
)

DatabaseFixture = tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    TenantContext,
    PosixObjectStore,
]


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1))
    config.attributes["database_url"] = ADMIN_DSN
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
        await repository.create_workspace(workspace_id, name="S9-T4-claims")
    try:
        yield engine, sessions, context, PosixObjectStore(tmp_path / "objects")
    finally:
        await engine.dispose()


def _scope() -> ClaimScope:
    return ClaimScope(
        mode="offline",
        model="reference-fixture",
        version="1",
        date="2026-09-05",
        corpus="factqa-v1",
        environment="offline-fixture",
    )


async def _create_and_seal_eval_run(
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
    store: PosixObjectStore,
) -> SealedEvalRun:
    async with tenant_session(sessions, context) as session:
        service = EvalFoundationService(session, context, store)
        created = await service.create(
            CreateEvalRunCommand(
                mode=EvalMode.OFFLINE,
                registered_units=UNITS,
                dataset_payload={"samples": [unit.sample_id for unit in UNITS]},
                code_digest=digest_bytes(b"code"),
                config_digest=digest_bytes(b"config"),
                schema_digest=digest_bytes(b"schema"),
            )
        )
        for unit in UNITS:
            await service.record_outcome(
                created.eval_run_id,
                SampleOutcome(unit=unit, status=SampleStatus.COMPLETED, result={"answer": "42"}),
            )
        return await service.seal(
            created.eval_run_id,
            migration_revision=MIGRATION_REVISION,
            test_report={"status": "passed", "scope": "s9-t4"},
        )


class TestClaimUpgradeFromRealSeal:
    @pytest.mark.asyncio
    async def test_offline_verified_upgrade_binds_the_recomputed_seal(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context, store = database
        sealed = await _create_and_seal_eval_run(sessions, context, store)
        async with tenant_session(sessions, context) as session:
            registry = ClaimRegistryService(session, context, store)
            await registry.register(
                claim_id="factqa-v1.accuracy",
                statement="FactQA accuracy {{accuracy}}（{{environment}}）",
                scope=_scope(),
            )
            upgraded = await registry.upgrade(
                "factqa-v1.accuracy",
                target=ClaimStatus.OFFLINE_VERIFIED,
                eval_run_id=sealed.eval_run_id,
            )
        assert upgraded.status is ClaimStatus.OFFLINE_VERIFIED
        assert upgraded.evidence is not None
        assert upgraded.evidence.seal_digest == sealed.seal_digest
        assert upgraded.evidence.eval_run_id == sealed.eval_run_id
        assert upgraded.evidence.artifact_manifest_id == sealed.manifest_id
        assert upgraded.evidence.mode == "offline"

        # 升级落库：全新会话读回的证据与升级结果一致（不是进程内缓存）
        async with tenant_session(sessions, context) as session:
            stored = await ClaimRegistryService(session, context, store).get("factqa-v1.accuracy")
        assert stored.status is ClaimStatus.OFFLINE_VERIFIED
        assert stored.evidence == upgraded.evidence

    @pytest.mark.asyncio
    async def test_manual_implemented_upgrade_then_live_refusal(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context, store = database
        sealed = await _create_and_seal_eval_run(sessions, context, store)
        async with tenant_session(sessions, context) as session:
            registry = ClaimRegistryService(session, context, store)
            await registry.register(
                claim_id="factqa-v1.latency",
                statement="p95 {{latency_ms}} ms",
                scope=_scope(),
            )
            manual = await registry.upgrade(
                "factqa-v1.latency", target=ClaimStatus.IMPLEMENTED, eval_run_id=None
            )
            assert manual.status is ClaimStatus.IMPLEMENTED
            with pytest.raises(ClaimUpgradeDenied):
                # fixture/live 口径混写防线：offline 密封件不能支撑 live_verified
                await registry.upgrade(
                    "factqa-v1.latency",
                    target=ClaimStatus.LIVE_VERIFIED,
                    eval_run_id=sealed.eval_run_id,
                )
        async with tenant_session(sessions, context) as session:
            stored = await ClaimRegistryService(session, context, store).get("factqa-v1.latency")
        assert stored.status is ClaimStatus.IMPLEMENTED

    @pytest.mark.asyncio
    async def test_unknown_eval_run_refuses_upgrade(self, database: DatabaseFixture) -> None:
        _, sessions, context, store = database
        async with tenant_session(sessions, context) as session:
            registry = ClaimRegistryService(session, context, store)
            await registry.register(
                claim_id="factqa-v1.coverage",
                statement="coverage {{coverage}}",
                scope=_scope(),
            )
            await registry.upgrade(
                "factqa-v1.coverage", target=ClaimStatus.IMPLEMENTED, eval_run_id=None
            )
            with pytest.raises(EvalRunNotFound):
                await registry.upgrade(
                    "factqa-v1.coverage",
                    target=ClaimStatus.OFFLINE_VERIFIED,
                    eval_run_id=uuid4(),
                )

    @pytest.mark.asyncio
    async def test_cross_tenant_eval_run_refuses_upgrade(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context, store = database
        other = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        async with tenant_session(sessions, other) as session:
            repository = TenantRepository(session, other)
            await repository.create_organization(other.organization_id, status="active")
            await repository.create_workspace(other.workspace_id, name="S9-T4-other")
        # 其他租户的真实密封件对本租户 claim 不可见（RLS）——升级必须拒绝
        foreign = await _create_and_seal_eval_run(sessions, other, store)
        async with tenant_session(sessions, context) as session:
            registry = ClaimRegistryService(session, context, store)
            await registry.register(
                claim_id="factqa-v1.exact",
                statement="exact {{exact}}",
                scope=_scope(),
            )
            await registry.upgrade(
                "factqa-v1.exact", target=ClaimStatus.IMPLEMENTED, eval_run_id=None
            )
            with pytest.raises(EvalRunNotFound):
                await registry.upgrade(
                    "factqa-v1.exact",
                    target=ClaimStatus.OFFLINE_VERIFIED,
                    eval_run_id=foreign.eval_run_id,
                )


class TestClaimRegistryPersistence:
    @pytest.mark.asyncio
    async def test_register_roundtrip_and_duplicate_refusal(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context, store = database
        async with tenant_session(sessions, context) as session:
            registry = ClaimRegistryService(session, context, store)
            registered = await registry.register(
                claim_id="factqa-v1.accuracy",
                statement="FactQA accuracy {{accuracy}}",
                scope=_scope(),
            )
            assert registered.status is ClaimStatus.PLANNED
            assert registered.evidence is None
            with pytest.raises(ClaimAlreadyRegistered):
                await registry.register(
                    claim_id="factqa-v1.accuracy",
                    statement="FactQA accuracy {{accuracy}}",
                    scope=_scope(),
                )
        async with tenant_session(sessions, context) as session:
            stored = await ClaimRegistryService(session, context, store).get("factqa-v1.accuracy")
            assert stored.scope == _scope()
            listing = await ClaimRegistryService(session, context, store).list()
        assert [item.claim_id for item in listing] == ["factqa-v1.accuracy"]
        with pytest.raises(ClaimNotFound):
            async with tenant_session(sessions, context) as session:
                await ClaimRegistryService(session, context, store).get("unknown.claim")
