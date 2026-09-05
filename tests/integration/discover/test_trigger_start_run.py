"""S8 §3.1：DiscoveryProgram 的 trigger 必须经 S2 Runtime 的 StartRun 发起执行。

生产路径：trigger 组件构造 StartRun 命令，经既有 RunCommandService 落账
（Run 行 + outbox 命令同事务），不绕过 Runtime。后台 run 使用 DiscoveryProgram
的 service identity（requested_by=service identity），不继承触发者的
session/token/personal memory。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from zhiwei.discover.programs import ProgramManager, ProgramStatus
from zhiwei.discover.triggers import ScheduleTrigger, SourceDeltaTrigger, WebhookTrigger
from zhiwei.persistence.models import OutboxMessage, Run
from zhiwei.persistence.run_commands import RunCommandService
from zhiwei.persistence.tenant import TenantContext
from zhiwei.runtime.commands import CommandKind, StartRun
from zhiwei.runtime.triggers.discovery import (
    DiscoveryTriggerService,
    TriggerFireError,
)

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


def _active_program(*, service_identity: str | None = "svc:discover-numeric") -> tuple:
    manager = ProgramManager()
    program = manager.create_program(
        name="numeric risk watch",
        created_by="alice",
        risk_charter="monitor numeric risk patterns",
        service_identity=service_identity,
    )
    version = manager.get_version(program.current_version_id)
    schedule = ScheduleTrigger(cron_expression="0 6 * * *")
    webhook = WebhookTrigger(
        path="discover/numeric",
        secret_digest="sha256hex:" + "a" * 64,
    )
    delta = SourceDeltaTrigger(
        source_id=uuid4(), watermark_field="month", min_change_threshold=0.0
    )
    return manager, program, version, (schedule, webhook, delta)


def test_schedule_trigger_starts_run_via_production_command_service() -> None:
    """schedule trigger → RunCommandService.submit_start_run → Run 行 + outbox StartRun。"""

    async def flow() -> tuple[Run, StartRun]:
        engine = create_async_engine(APP_URL)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        manager, program, version, triggers = _active_program()
        program = manager.activate(program.id, performed_by="alice")
        context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        from zhiwei.persistence.tenant import tenant_session

        async with tenant_session(sessions, context) as session:
            from zhiwei.persistence.models import Workspace
            from zhiwei.persistence.repositories import TenantRepository

            repository = TenantRepository(session, context)
            await repository.create_organization(context.organization_id, status="active")
            session.add(
                Workspace(
                    id=context.workspace_id,
                    organization_id=context.organization_id,
                    name="discover-trigger",
                    schema_version=1,
                )
            )
            await session.flush()
            commands = RunCommandService(session, context)
            service = DiscoveryTriggerService(commands)
            start_run = await service.fire(
                program,
                version,
                triggers[0],
                now=_NOW,
                run_id=uuid4(),
            )
            rows = (
                (await session.execute(select(OutboxMessage).where(OutboxMessage.topic == "runtime.command")))
                .scalars()
                .all()
            )
            assert rows, "StartRun 命令必须经 outbox 落账"
            payload = rows[-1].payload
            assert payload["kind"] == CommandKind.START_RUN.value
            assert payload["requested_by"] == "svc:discover-numeric"
            run_row = await session.get(Run, start_run.run_id)
            assert run_row is not None
        await engine.dispose()
        return run_row, StartRun.model_validate(payload)

    run_row, command = asyncio.run(flow())
    assert run_row.status == "created"
    assert command.requested_by == "svc:discover-numeric"
    assert command.workflow_id == f"run-{run_row.id}"


def test_webhook_trigger_requires_matching_secret_digest() -> None:
    manager, program, version, triggers = _active_program()
    program = manager.activate(program.id, performed_by="alice")
    with pytest.raises(TriggerFireError, match="secret"):
        DiscoveryTriggerService.build_start_run(
            program, version, triggers[1], now=_NOW, run_id=uuid4(), webhook_secret="wrong"
        )


def test_trigger_fire_refuses_draft_program() -> None:
    _, program, version, triggers = _active_program()
    assert program.status == ProgramStatus.DRAFT
    with pytest.raises(TriggerFireError, match="draft"):
        DiscoveryTriggerService.build_start_run(program, version, triggers[0], now=_NOW, run_id=uuid4())


def test_trigger_fire_refuses_program_without_service_identity() -> None:
    """§3.1 fail closed：没有 service identity 的程序不得发起后台 run。"""
    manager, program, version, triggers = _active_program(service_identity=None)
    program = manager.activate(program.id, performed_by="alice")
    with pytest.raises(TriggerFireError, match="service identity"):
        DiscoveryTriggerService.build_start_run(program, version, triggers[0], now=_NOW, run_id=uuid4())


def test_source_delta_trigger_does_not_refire_on_unchanged_watermark() -> None:
    """watermark 未推进 → 不重复发起 run（重试/重复投递不复制 run）。"""
    manager, program, version, triggers = _active_program()
    program = manager.activate(program.id, performed_by="alice")
    service = DiscoveryTriggerService(RunCommandService.__new__(RunCommandService))
    assert service.source_delta_changed(program, version, triggers[2], observed="2025-12", now=_NOW)
    assert not service.source_delta_changed(program, version, triggers[2], observed="2025-12", now=_NOW)
    assert service.source_delta_changed(program, version, triggers[2], observed="2026-01", now=_NOW)
