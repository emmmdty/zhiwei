"""S2 安全：跨 Organization 并发 Run 的租户隔离与终态完整性。

事实源：specs/s2-agent-runtime.md §6（「10 个并发跨两 Organization Run：无事件、
approval、artifact、stream 串租户且全有 terminal state」）。

真实 PG（RLS 强制）+ 真实 Temporal dev server + 生产命令路径。断言：
1. 全部 run 达到 run 级终态；
2. 事件/canonical projection 在错误租户上下文下不可见（跨租户零泄漏）；
3. 每个租户只看到自己的 run 集合。
"""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.runtime.persistence import RuntimeEventStore
from zhiwei.runtime.reducer import RunState
from zhiwei.runtime.run_commands import RunCommandService
from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE, build_agent_worker
from zhiwei.workers.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxDispatcherConfig,
    SessionOutboxRepository,
)
from zhiwei.workers.temporal_sender import TemporalWorkflowSender

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_PER_ORG = 5


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


class _FixtureHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "Fixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"task_id": input.task_id})


def _graph(run_index: int) -> TaskGraph:
    """每个 run 的图带独立节点 id（防串_run 断言的伪造阴性）。"""
    suffix = f"r{run_index}"
    return TaskGraph(
        nodes={
            f"intake_{suffix}": TaskGraphNode(
                task_id=f"intake_{suffix}", task_type="Fixture",
                required_capability="fixture",
            ),
            f"analyze_{suffix}": TaskGraphNode(
                task_id=f"analyze_{suffix}", task_type="Fixture",
                dependencies=(f"intake_{suffix}",), required_capability="fixture",
            ),
        },
        edges={f"analyze_{suffix}": [f"intake_{suffix}"]},
    )


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(_app_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local()
    try:
        yield env
    finally:
        await env.shutdown()


async def _make_tenant(
    sessions: async_sessionmaker[AsyncSession], tag: str
) -> TenantContext:
    organization_id, workspace_id = uuid_module.uuid4(), uuid_module.uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name=f"iso-{tag}")
    return context


async def _submit(sessions, context: TenantContext, run_id, graph: TaskGraph) -> None:
    async with tenant_session(sessions, context) as session:
        service = RunCommandService(session, context)
        await service.submit_start_run(
            run_id=run_id,
            graph=graph.model_dump(mode="json"),
            task_queue=DEFAULT_TASK_QUEUE,
        )


async def _dispatch_until_drained(
    sessions, context: TenantContext, client
) -> None:
    from sqlalchemy import select

    from zhiwei.persistence.models import OutboxMessage
    from zhiwei.runtime.run_commands import RUNTIME_COMMAND_TOPIC

    dispatcher = OutboxDispatcher(
        SessionOutboxRepository(sessions, context),
        __import__("zhiwei.runtime.outbox_handlers", fromlist=["OutboxSignalHandler"]).OutboxSignalHandler(
            TemporalWorkflowSender(client)
        ),
        OutboxDispatcherConfig(
            worker_id=f"iso-{context.organization_id}",
            poll_interval=timedelta(milliseconds=20),
            batch_limit=20,
            max_attempts=5,
            base_delay=timedelta(milliseconds=20),
        ),
    )
    for _ in range(300):
        await dispatcher.poll_once()
        async with tenant_session(sessions, context) as session:
            pending = (
                await session.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.organization_id == context.organization_id,
                        OutboxMessage.workspace_id == context.workspace_id,
                        OutboxMessage.topic == RUNTIME_COMMAND_TOPIC,
                        OutboxMessage.status.in_(("pending", "processing")),
                    )
                )
            ).all()
            if not pending:
                return
        await asyncio.sleep(0.05)
    raise AssertionError("commands did not drain")


async def _wait_terminal(sessions, context: TenantContext, run_id) -> RunState:
    for _ in range(300):
        async with tenant_session(sessions, context) as session:
            state = await RuntimeEventStore(session, context).reduce_state(run_id)
            if state.is_terminal:
                return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"run {run_id} did not reach terminal")


class TestConcurrentCrossTenantRuns:
    async def test_ten_concurrent_runs_two_orgs_no_leak(self, engine, env) -> None:
        sessions = create_session_factory(engine)
        org_a = await _make_tenant(sessions, "a")
        org_b = await _make_tenant(sessions, "b")

        registry = TaskHandlerRegistry()
        registry.register(_FixtureHandler())
        worker = build_agent_worker(
            env.client,
            task_queue=DEFAULT_TASK_QUEUE,
            session_factory=sessions,
            handler_registry=registry,
            max_concurrent_activities=32,
        )

        runs_by_org: dict[int, list] = {0: [], 1: []}
        tenants = (org_a, org_b)
        for i in range(RUNS_PER_ORG):
            for tenant_index, context in enumerate(tenants):
                run_id = uuid_module.uuid4()
                runs_by_org[tenant_index].append(run_id)
                await _submit(sessions, context, run_id, _graph(i))

        async with worker:
            await asyncio.gather(
                _dispatch_until_drained(sessions, org_a, env.client),
                _dispatch_until_drained(sessions, org_b, env.client),
            )
            states = await asyncio.gather(*[
                _wait_terminal(sessions, context, run_id)
                for tenant_index, context in enumerate(tenants)
                for run_id in runs_by_org[tenant_index]
            ])

        # 1. 全部终态且成功
        for state in states:
            assert state.status == "completed", state.status
            assert all(t.status == "completed" for t in state.tasks.values())

        # 2. 跨租户零泄漏：错误租户上下文读对方 run 的事件为空
        for run_id in runs_by_org[0]:
            async with tenant_session(sessions, org_b) as session:
                events = await RuntimeEventStore(session, org_b).load_events(run_id)
                assert events == [], f"org B must not see org A run {run_id}"
        for run_id in runs_by_org[1]:
            async with tenant_session(sessions, org_a) as session:
                events = await RuntimeEventStore(session, org_a).load_events(run_id)
                assert events == [], f"org A must not see org B run {run_id}"

        # 3. 每租户只包含自己的节点（run 图独立，事件投影无串染）
        for tenant_index, context in enumerate(tenants):
            for i, run_id in enumerate(runs_by_org[tenant_index]):
                async with tenant_session(sessions, context) as session:
                    state = await RuntimeEventStore(session, context).reduce_state(run_id)
                suffix = f"r{i}"
                assert set(state.tasks) == {f"intake_{suffix}", f"analyze_{suffix}"}
