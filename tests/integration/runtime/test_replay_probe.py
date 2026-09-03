"""S2 修复轮批次 B RED（H-5）：replay-check 探针的载入独立性。

事实源：specs/s2-agent-runtime.md §6（2026-09-03 增补）——「replay-check：探针
两次载入必须在不同事务/会话（同事务双查询鉴别力不足，ADR-012 反例 7）」。

同一事务内的两次 SELECT 共享快照：并发写入发生在两次载入之间时，两次看到
的是同一旧快照——「双次载入」退化为恒真探针。独立事务/会话下，第二次载入
必须观察到载入间隙的并发写入（这正是不变量的鉴别力来源）。
"""

from __future__ import annotations

import os
import uuid as uuid_module
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from zhiwei.contracts.time import utc_now
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.events import TaskScheduled

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    from alembic import command
    from alembic.config import Config

    dsn = os.environ.get(
        "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
    )
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", dsn)
    config.attributes["database_url"] = dsn
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def tenant() -> AsyncIterator[tuple[object, TenantContext, str]]:
    from zhiwei.persistence.database import create_database_engine, create_session_factory
    from zhiwei.persistence.repositories import TenantRepository

    engine = create_database_engine(
        os.environ.get(
            "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
        ).replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    run_id = str(uuid4())
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="replay-probe")
        # 直接落 RunCreated + 一个 task 事件：探针只关心事件序列的载入一致性
        from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
        from zhiwei.runtime.events import RunCreated

        graph = TaskGraph(
            nodes={"t1": TaskGraphNode(
                task_id="t1", task_type="Fixture", dependencies=(),
                parallel_safe=False, required_capability="fixture",
            )},
            edges={},
        )
        store = RuntimeEventStore(session, context)
        await store.append(
            RunCreated(
                run_id=uuid_module.UUID(run_id),
                timestamp=utc_now(),
                graph=graph,
            ),
            actor_ref="test:replay-probe",
            idempotency_key=f"run-created:{run_id}",
        )
        await store.append(
            TaskScheduled(
                run_id=uuid_module.UUID(run_id),
                timestamp=utc_now(),
                task_id="t1",
            ),
            actor_ref="test:replay-probe",
            idempotency_key=f"scheduled:{run_id}:t1",
        )
    try:
        yield sessions, context, run_id
    finally:
        await engine.dispose()


class TestReplayProbeIndependence:
    async def test_second_load_observes_concurrent_write_between_loads(
        self, tenant
    ) -> None:
        """两次载入之间的并发写入必须被第二次载入观察到（独立快照）。

        同事务实现：两次 SELECT 同快照 → 第二次看不到钩子写入 → 探针恒真
        （RED）。独立事务实现：第二次载入看到新增事件（GREEN）。
        """
        from zhiwei.cli.runtime import load_events_twice_independently

        sessions, context, run_id = tenant
        parsed = uuid_module.UUID(run_id)

        async def _concurrent_write() -> None:
            async with tenant_session(sessions, context) as session:
                store = RuntimeEventStore(session, context)
                await store.append(
                    TaskScheduled(
                        run_id=parsed,
                        timestamp=utc_now(),
                        task_id="t2",
                    ),
                    actor_ref="test:concurrent-writer",
                    idempotency_key=f"scheduled:{run_id}:t2",
                )

        events_a, events_b = await load_events_twice_independently(
            sessions, context, parsed, between=_concurrent_write
        )
        assert len(events_b) == len(events_a) + 1, (
            "第二次载入必须观察到载入间隙的并发写入（独立事务/快照），"
            f"实际 a={len(events_a)} b={len(events_b)}（同事务恒真探针）"
        )
