"""live-synthesis Run 供给的集成面验证（S11 followup-3）。

首次 live 触发（eval_run e9b19e43）根因：executor 以合成 run_id 调模型，
canonical 落账经 CanonicalUnitOfWork.append_event 要求 run_id 在租户作用域有
真实 Run 行——没有即 RunNotFound，4 个可答单位全 ERROR。

本测试在真实 PG 上锁定修复链路：生产命令路径供给（_provision_live_synthesis_run，
Run 行 + outbox StartRun 同事务）→ canonical 落账被承认（first use 留痕 +
wire manifest，即首次触发失败的同一落账点）。不调用 live 模型（AGENTS.md：
live 只由 operator 显式触发）；endpoint 用 discard 地址，无外部网络。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from zhiwei.cli.evals import _provision_live_synthesis_run
from zhiwei.context.manifests import ContextManifest
from zhiwei.models.contracts import ClassificationCeiling, NetworkZone, TrustTier
from zhiwei.models.first_use import EndpointFirstUseDeclaration
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.model_first_use import CanonicalEndpointFirstUseSink
from zhiwei.persistence.model_manifests import CanonicalManifestSink
from zhiwei.persistence.models import OutboxMessage, Run
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

REPO_ROOT = Path(__file__).resolve().parents[3]

ADMIN_DSN = "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
APP_URL = "postgresql+asyncpg://zhiwei_app@127.0.0.1:55432/zhiwei_test"


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN)
    config.attributes["database_url"] = ADMIN_DSN
    command.upgrade(config, "head")
    yield


@pytest.mark.asyncio
async def test_provisioned_run_admits_canonical_ledger_writes() -> None:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
    assert context.workspace_id is not None
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        await repository.create_workspace(context.workspace_id, name="live-synthesis-v1")

    # 生产命令路径供给：可答单位的 Run 行真实存在
    run_id = await _provision_live_synthesis_run(sessions, context, "live-ans-001")
    async with tenant_session(sessions, context) as session:
        run_row = await session.get(Run, run_id)
        assert run_row is not None
        assert run_row.organization_id == context.organization_id
        assert run_row.status == "created"
        commands = (
            await session.scalars(
                select(OutboxMessage).where(OutboxMessage.id.is_not(None)).limit(10)
            )
        ).all()
        assert any(
            cmd.topic == "runtime.command" and cmd.payload["run_id"] == str(run_id)
            for cmd in commands
        ), "StartRun 命令必须与 Run 行同事务落账"

    # canonical 落账被承认——首次触发失败的同一落账点（first use 留痕）
    first_use_sink = CanonicalEndpointFirstUseSink(sessions, context)
    declaration = EndpointFirstUseDeclaration(
        base_url="http://127.0.0.1:9/v1",
        trust_tier=TrustTier.UNVERIFIED,
        network_zone=NetworkZone.UNKNOWN,
        classification_ceiling=ClassificationCeiling.PUBLIC,
        declared_by="test:integration",
    )
    assert await first_use_sink.record_first_use(declaration, run_id=run_id) is True

    # wire manifest 落账（CaptureTransport 捕获后的 sink 入口）
    manifest = ContextManifest(
        manifest_id="m-live-provisioning-1",
        body_sha256="sha256:" + "0" * 64,
        body_len=3,
        url="http://127.0.0.1:9/v1/chat/completions",
        method="POST",
        captured_at="2026-09-09T00:00:00+00:00",
        sequence_no=0,
    )
    manifest_sink = CanonicalManifestSink(sessions, context)
    assert await manifest_sink.record_wire_manifest(manifest, run_id=run_id) is True

    # 重复落账幂等（manifest_id 进幂等键，run 作用域内防重）
    assert await manifest_sink.record_wire_manifest(manifest, run_id=run_id) is False


@pytest.mark.asyncio
async def test_provision_is_per_tenant_not_globally_deterministic() -> None:
    """同 sample 在新租户供给新 run id——runs.id 是全局 PK，固定派生值会跨 run 碰撞。"""
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    ids: list[object] = []
    for _ in range(2):
        context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        assert context.workspace_id is not None
        async with tenant_session(sessions, context) as session:
            repository = TenantRepository(session, context)
            await repository.create_organization(context.organization_id, status="active")
            await repository.create_workspace(context.workspace_id, name="live-synthesis-v1")
        ids.append(await _provision_live_synthesis_run(sessions, context, "live-ans-001"))
    assert ids[0] != ids[1]
