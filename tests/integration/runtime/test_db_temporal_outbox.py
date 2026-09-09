"""S2-T4 集成：outbox ↔ PG ↔ Temporal 桥接的真实故障语义。

事实源：specs/s2-agent-runtime.md §4/§6（outbox：DB/Temporal 任一侧故障、
duplicate start/signal、dispatcher crash、poison message）。

真实 PG（55432）+ 真实 Temporal dev server + 真实 dispatcher/sender——无内存旁路。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid as uuid_module
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import OutboxMessage
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.run_commands import RunCommandService
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.runtime.outbox_handlers import OutboxSignalHandler
from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE, build_agent_worker
from zhiwei.workers.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxDispatcherConfig,
    SessionOutboxRepository,
)
from zhiwei.workers.temporal_sender import TemporalWorkflowSender

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[3]


def _admin_url() -> str:
    import os

    dsn = os.environ.get(
        "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
    )
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _app_url() -> str:
    import os

    dsn = os.environ.get(
        "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
    )
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", _admin_url())
    config.attributes["database_url"] = _admin_url()
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture(scope="module")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local()
    try:
        yield env
    finally:
        await env.shutdown()


class _FixtureHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "Fixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"task_id": input.task_id})


@pytest_asyncio.fixture
async def database() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession], TenantContext]]:
    engine = create_database_engine(_app_url())
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="S2-T4")
    try:
        yield engine, sessions, context
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def worker_stack(
    temporal_env: WorkflowEnvironment,
    database,
) -> AsyncIterator[tuple[Worker, async_sessionmaker[AsyncSession], TenantContext]]:
    _, sessions, context = database
    registry = TaskHandlerRegistry()
    registry.register(_FixtureHandler())
    worker = build_agent_worker(
        temporal_env.client,
        task_queue=DEFAULT_TASK_QUEUE,
        session_factory=sessions,
        handler_registry=registry,
    )
    async with worker:
        yield worker, sessions, context


def _dispatcher(
    sessions, context: TenantContext, client: Client, **config_kwargs
) -> OutboxDispatcher:
    repository = SessionOutboxRepository(sessions, context)
    sender = TemporalWorkflowSender(client)
    handler = OutboxSignalHandler(sender)
    defaults = {
        "worker_id": "test-dispatcher",
        "poll_interval": timedelta(milliseconds=10),
        "batch_limit": 10,
        "max_attempts": 3,
        "base_delay": timedelta(milliseconds=10),
        "lease_duration": timedelta(seconds=30),
    }
    defaults.update(config_kwargs)
    return OutboxDispatcher(
        repository, handler, OutboxDispatcherConfig(**defaults)
    )


def _graph() -> TaskGraph:
    return TaskGraph(
        nodes={
            "t1": TaskGraphNode(task_id="t1", task_type="Fixture", required_capability="f"),
        },
        edges={},
    )


async def _submit_start(sessions, context: TenantContext) -> str:
    run_id = uuid4()
    async with tenant_session(sessions, context) as session:
        service = RunCommandService(session, context)
        await service.submit_start_run(
            run_id=run_id,
            graph=_graph().model_dump(mode="json"),
            task_queue=DEFAULT_TASK_QUEUE,
        )
    return str(run_id)


async def _fetch_outbox_row(sessions, context: TenantContext, run_id: str) -> OutboxMessage:
    """经 tenant 事务读取（RLS 对无 GUC 的读取同样过滤行）。"""
    async with tenant_session(sessions, context) as session:
        rows = (
            await session.scalars(
                select(OutboxMessage).where(
                    OutboxMessage.organization_id == context.organization_id,
                    OutboxMessage.workspace_id == context.workspace_id,
                )
            )
        ).all()
        matches = [row for row in rows if row.payload.get("run_id") == run_id]
        assert matches, f"outbox row for run {run_id} not found"
        return matches[0]


class TestDbSuccessTemporalSuccess:
    async def test_start_command_dispatch_starts_workflow_and_run_reaches_terminal(
        self, temporal_env, worker_stack
    ) -> None:
        _, sessions, context = worker_stack
        run_id = await _submit_start(sessions, context)
        dispatcher = _dispatcher(sessions, context, temporal_env.client)
        results = await dispatcher.poll_once()
        assert [r.status for r in results] == ["delivered"]

        row = await _fetch_outbox_row(sessions, context, run_id)
        assert row.status == "delivered"

        # workflow 真实执行：等 PG 到终态
        state = None
        for _ in range(100):
            async with tenant_session(sessions, context) as session:
                state = await RuntimeEventStore(session, context).reduce_state(
                    uuid_module.UUID(run_id)
                )
                if state.is_terminal:
                    break
            await asyncio.sleep(0.2)
        assert state is not None and state.status == "completed"


class TestDuplicateDispatch:
    async def test_redelivered_start_after_crash_is_idempotent(
        self, temporal_env, worker_stack
    ) -> None:
        """dispatcher 在 start_workflow 之后、mark_delivered 之前崩溃 → 重复投递。

        租约到期后重新 claim → 再次 start → deterministic id 冲突 → 幂等 delivered；
        workflow 只执行一次。
        """
        _, sessions, context = worker_stack
        run_id = await _submit_start(sessions, context)
        repository = SessionOutboxRepository(sessions, context)
        sender = TemporalWorkflowSender(temporal_env.client)
        handler = OutboxSignalHandler(sender)

        # 第一次投递（随后「崩溃」：不 mark）；租约 1ms，随后即可被重新 claim
        claimed = await repository.claim_batch(
            worker_id="crashed",
            limit=10,
            now=datetime.now(tz=UTC),
            lease_duration=timedelta(milliseconds=1),
        )
        assert len(claimed) == 1
        result = await handler.handle(claimed[0])
        assert result.status == "delivered"
        await asyncio.sleep(0.05)  # 租约过期

        # worker 真实执行 workflow 至终态（确保第二次 start 命中已结束的 execution）
        handle = temporal_env.client.get_workflow_handle(f"run-{run_id}")
        await asyncio.wait_for(handle.result(), timeout=30)

        # 新 dispatcher 接手（租约已过期）→ 重复 start → AlreadyStarted → delivered
        dispatcher = _dispatcher(sessions, context, temporal_env.client)
        results = await dispatcher.poll_once()
        assert [r.status for r in results] == ["delivered"]
        row = await _fetch_outbox_row(sessions, context, run_id)
        assert row.status == "delivered"

        # workflow 历史只有一条 execution（未被重复启动）
        history = await handle.fetch_history()
        starts = [e for e in history.events if e.event_type == 1]
        assert len(starts) == 1  # 只有一条 WORKFLOW_EXECUTION_STARTED（未被重复启动）


class TestSignalBeforeWorker:
    async def test_cancel_without_workflow_is_retried_not_dropped(
        self, temporal_env, database
    ) -> None:
        """signal-before-worker：目标 workflow 从未 start → 有界重试，不静默丢弃。"""
        _, sessions, context = database
        orphan_run_id = uuid4()
        async with tenant_session(sessions, context) as session:
            await RunCommandService(session, context).submit_cancel_run(
                run_id=orphan_run_id, reason="operator premature"
            )
        dispatcher = _dispatcher(sessions, context, temporal_env.client)
        results = await dispatcher.poll_once()
        assert [r.status for r in results] == ["signal_before_worker"]

        row = await _fetch_outbox_row(sessions, context, str(orphan_run_id))
        assert row.status == "pending"  # 退避后重试
        assert row.attempts == 1
        assert row.last_error == "signal before worker"


class TestTemporalFailure:
    async def test_temporal_down_dead_letters_after_bounded_retries(self, database) -> None:
        """真实 Temporal 宕机（env 关闭）→ 有界重试 → dead_letter，真相保留在 PG。"""
        _, sessions, context = database
        # 独立 env：client 连上后关闭服务，模拟 Temporal 运行中不可用
        env = await WorkflowEnvironment.start_local()
        client = env.client
        await env.shutdown()

        run_id = await _submit_start(sessions, context)
        dispatcher = _dispatcher(
            sessions, context, client, max_attempts=2, base_delay=timedelta(seconds=0)
        )
        row = await _fetch_outbox_row(sessions, context, run_id)
        for _ in range(5):
            with contextlib.suppress(Exception):
                # claim/dispatch 期间连接错误不应丢失消息
                await dispatcher.poll_once()
            row = await _fetch_outbox_row(sessions, context, run_id)
            if row.status == "dead_letter":
                break
            await asyncio.sleep(0.05)
        assert row.status == "dead_letter"
        assert row.attempts >= 2
        assert row.last_error is not None


class TestPoisonMessage:
    async def test_garbage_payload_dead_letters(self, temporal_env, database) -> None:
        _, sessions, context = database
        async with tenant_session(sessions, context) as session:
            session.add(
                OutboxMessage(
                    id=uuid4(),
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    topic="runtime.command",
                    event_key="start_run",
                    payload={"garbage": True},
                    status="pending",
                    attempts=0,
                    available_at=datetime.now(tz=UTC),
                    schema_version=1,
                    created_at=datetime.now(tz=UTC),
                )
            )

        dispatcher = _dispatcher(
            sessions, context, temporal_env.client, max_attempts=1, base_delay=timedelta(0)
        )
        results = await dispatcher.poll_once()
        assert [r.status for r in results] == ["poison"]

        async with tenant_session(sessions, context) as session:
            row = await session.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.organization_id == context.organization_id,
                    OutboxMessage.event_key == "start_run",
                )
            )
            assert row is not None
            assert row.status == "dead_letter"
