"""F-R5-05（T-P4.4/B4b）：trigger watermark 持久化 + webhook 防重放。

finding 证据：DiscoveryTriggerService 的 watermark 为进程内 dict（重启丢失
→ 重复触发 run）；webhook 校验仅 possession proof（secret digest），无
nonce/时间窗——重复投递重放无防护。本批交付：PG 持久 store（0023 迁移）+
服务 seam（source_delta_changed 异步持久化）+ webhook delivery 窗口与
一次性 claim（fail closed）。webhook HTTP 端点本身属 S11 compose（与
F-R6-04 webhook 入口同口径）——本批交付端点必须调用的机制。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from zhiwei.discover.programs import ProgramManager
from zhiwei.discover.triggers import ScheduleTrigger, SourceDeltaTrigger, WebhookTrigger
from zhiwei.persistence.run_commands import RunCommandService
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.persistence.trigger_watermarks import PgTriggerStateStore
from zhiwei.runtime.triggers.discovery import DiscoveryTriggerService, TriggerFireError

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
ADMIN_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

_NOW = datetime(2026, 9, 4, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


def _active_program() -> tuple:
    manager = ProgramManager()
    program = manager.create_program(
        name="trigger state watch",
        created_by="alice",
        risk_charter="monitor trigger state persistence",
        service_identity="svc:trigger-state",
    )
    version = manager.get_version(program.current_version_id)
    schedule = ScheduleTrigger(cron_expression="0 6 * * *")
    webhook = WebhookTrigger(
        path="discover/trigger-state",
        secret_digest="sha256hex:" + "b" * 64,
    )
    delta = SourceDeltaTrigger(
        source_id=uuid4(), watermark_field="month", min_change_threshold=0.0
    )
    return manager, program, version, (schedule, webhook, delta)


async def _seed_tenant(sessions, context) -> None:
    from zhiwei.persistence.models import Workspace
    from zhiwei.persistence.repositories import TenantRepository

    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        session.add(
            Workspace(
                id=context.workspace_id,
                organization_id=context.organization_id,
                name="trigger-state",
                schema_version=1,
            )
        )


def test_watermark_persists_across_service_instances() -> None:
    """重启（新 service 实例）后 watermark 仍在：未推进不重复触发（finding
    所指进程内 dict 缺口关闭）。"""

    async def flow() -> tuple[bool, bool, bool]:
        engine = create_async_engine(APP_URL)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        manager, program, version, triggers = _active_program()
        program = manager.activate(program.id, performed_by="alice")
        context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        await _seed_tenant(sessions, context)
        async with tenant_session(sessions, context) as session:
            store = PgTriggerStateStore(session, context)
            service_a = DiscoveryTriggerService(
                RunCommandService.__new__(RunCommandService), state_store=store
            )
            first = await service_a.source_delta_changed(
                program, version, triggers[2], observed="2025-12", now=_NOW
            )
        # 模拟重启：新 store（新 session）+ 新 service 实例
        async with tenant_session(sessions, context) as session:
            store_b = PgTriggerStateStore(session, context)
            service_b = DiscoveryTriggerService(
                RunCommandService.__new__(RunCommandService), state_store=store_b
            )
            second = await service_b.source_delta_changed(
                program, version, triggers[2], observed="2025-12", now=_NOW
            )
            third = await service_b.source_delta_changed(
                program, version, triggers[2], observed="2026-01", now=_NOW
            )
        await engine.dispose()
        return first, second, third

    first, second, third = asyncio.run(flow())
    assert first, "首次观察视为相对激活基线的 delta"
    assert not second, "重启后同 watermark 不得重复触发（持久化本体）"
    assert third, "watermark 推进必须再次触发"


def test_webhook_delivery_replay_and_stale_timestamp_rejected() -> None:
    """webhook 防重放：delivery nonce 一次性 claim + 时间窗校验（fail closed）。"""

    async def flow() -> None:
        engine = create_async_engine(APP_URL)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        manager, program, _version, triggers = _active_program()
        program = manager.activate(program.id, performed_by="alice")
        context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        await _seed_tenant(sessions, context)
        webhook = triggers[1]
        async with tenant_session(sessions, context) as session:
            store = PgTriggerStateStore(session, context)
            service = DiscoveryTriggerService(
                RunCommandService.__new__(RunCommandService), state_store=store
            )
            await service.verify_webhook_delivery(
                webhook, delivery_id="delivery-1", timestamp=_NOW, now=_NOW
            )
            with pytest.raises(TriggerFireError, match="replay"):
                await service.verify_webhook_delivery(
                    webhook, delivery_id="delivery-1", timestamp=_NOW, now=_NOW
                )
            # 不同 delivery 不受影响
            await service.verify_webhook_delivery(
                webhook, delivery_id="delivery-2", timestamp=_NOW, now=_NOW
            )
            # 时间窗外（过期重放）拒绝
            stale = _NOW - timedelta(seconds=3600)
            with pytest.raises(TriggerFireError, match="window"):
                await service.verify_webhook_delivery(
                    webhook, delivery_id="delivery-3", timestamp=stale, now=_NOW
                )
        await engine.dispose()

    asyncio.run(flow())


def test_webhook_delivery_without_state_store_fails_closed() -> None:
    """未注入 state store 的服务拒绝 webhook delivery 校验（不退化为无防重放）。"""
    manager, program, _version, triggers = _active_program()
    program = manager.activate(program.id, performed_by="alice")
    service = DiscoveryTriggerService(RunCommandService.__new__(RunCommandService))
    with pytest.raises(TriggerFireError, match="state store"):
        asyncio.run(
            service.verify_webhook_delivery(
                triggers[1], delivery_id="delivery-x", timestamp=_NOW, now=_NOW
            )
        )
