"""S2-T3 集成测试夹具：真实 PG（55432）+ 真实 Temporal（in-process dev server）。

事实源：specs/s2-agent-runtime.md §6/§7 Gate。不使用任何 fake workflow/activity——
WorkflowEnvironment.start_local() 是官方进程内 Temporal dev server（真实 replay 引擎），
worker/activities 与生产完全同构。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE, build_agent_worker

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
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", _admin_url())
    config.attributes["database_url"] = _admin_url()
    command.upgrade(config, "head")
    yield


class FlakyFixtureHandler(TaskHandler):
    """Fixture handler that fails the first `fail_times` invocations.

    业务失败经 activity 正常返回 TaskFailed（不抛给 Temporal），workflow 按节点
    failure policy 重派新 attempt（新 attempt_id）；本 handler 按总调用次数注入
    瞬态失败——第 N+1 次调用成功。
    """

    def __init__(self, *, fail_times: int = 0, sleep_seconds: float = 0.0) -> None:
        self._fail_times = fail_times
        self._sleep_seconds = sleep_seconds
        self._calls = 0

    @property
    def primitive_type(self) -> str:
        return "FlakyFixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        import time

        self._calls += 1
        if self._sleep_seconds:
            time.sleep(self._sleep_seconds)
        if self._calls <= self._fail_times:
            raise RuntimeError(f"flaky failure {self._calls}/{self._fail_times}")
        return TaskOutput(output_values={"task_id": input.task_id, "attempt": self._calls})


class SlowFixtureHandler(TaskHandler):
    """Handler that sleeps to simulate long work (worker kill / timeout tests)."""

    def __init__(self, *, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds

    @property
    def primitive_type(self) -> str:
        return "SlowFixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        import time

        time.sleep(self._sleep_seconds)
        return TaskOutput(output_values={"task_id": input.task_id})


class CountingFixtureHandler(TaskHandler):
    """Deterministic fixture handler counting distinct attempts."""

    def __init__(self) -> None:
        self.attempt_ids: list[str] = []

    @property
    def primitive_type(self) -> str:
        return "Fixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        self.attempt_ids.append(str(input.attempt_id))
        return TaskOutput(output_values={"task_id": input.task_id})


def build_test_registry() -> TaskHandlerRegistry:
    registry = TaskHandlerRegistry()
    registry.register(CountingFixtureHandler())
    return registry


@pytest_asyncio.fixture(scope="module")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local()
    try:
        yield env
    finally:
        await env.shutdown()


@pytest_asyncio.fixture
async def database() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession], TenantContext]]:
    engine = create_database_engine(_app_url())
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="S2-T3")
        await session.execute(
            text("SELECT 1")
        )
    try:
        yield engine, sessions, context
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def worker_stack(
    temporal_env: WorkflowEnvironment,
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession], TenantContext],
) -> AsyncIterator[tuple[Worker, async_sessionmaker[AsyncSession], TenantContext, TaskHandlerRegistry]]:
    """A running worker bound to the real env + PG, with per-test org/workspace."""
    _, sessions, context = database
    registry = build_test_registry()
    worker = build_agent_worker(
        temporal_env.client,
        task_queue=DEFAULT_TASK_QUEUE,
        session_factory=sessions,
        handler_registry=registry,
        max_concurrent_activities=32,
    )
    async with worker:
        yield worker, sessions, context, registry


class EffectUnknownFixtureHandler(TaskHandler):
    """Handler whose side-effect state is unknown after an exception.

    effect_unknown 语义（spec §4 增补）：副作用已可能发生，重试会造成重复
    副作用——workflow 侧禁止自动重试。抛出的异常类型来自
    zhiwei.runtime.handlers.base（EffectUnknownError，S2 修复轮批次 C 契约）。
    """

    @property
    def primitive_type(self) -> str:
        return "EffectUnknownFixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        from zhiwei.runtime.handlers.base import EffectUnknownError

        raise EffectUnknownError(f"effect state unknown for {input.task_id}")
