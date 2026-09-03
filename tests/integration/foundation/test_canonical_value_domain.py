"""S2 修复轮批次 C RED（S0 不变量，ADR-12 反例）：canonical 值域与 JSONB 兼容。

specs/s0 §4（2026-09-03 增补）：写入 canonical_events 的值经 JSONB 往返后必须
仍能复算出逐字节一致的 canonical JSON；写入侧对不可 round-trip 的值 fail
closed——|float| ≥ 1e16 被 jsonb 归一为整数字面量，读回 int 超出 JCS 安全
整数域，链验证必败（落库后毒化整个 run 的事件链）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.events import TaskCompleted

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
    from sqlalchemy import text

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
        await repository.create_workspace(workspace_id, name="value-domain")
        await session.execute(
            text(
                "INSERT INTO runs (id, organization_id, workspace_id, status, schema_version)"
                " VALUES (:id, :org, :ws, 'created', 1)"
            ),
            {"id": run_id, "org": organization_id, "ws": workspace_id},
        )
    try:
        yield sessions, context, run_id
    finally:
        await engine.dispose()


def _graph() -> TaskGraph:
    return TaskGraph(
        nodes={
            "t1": TaskGraphNode(
                task_id="t1",
                task_type="Fixture",
                dependencies=(),
                parallel_safe=False,
                required_capability="fixture",
            )
        },
        edges={},
    )


class TestCanonicalValueDomain:
    async def test_non_roundtrippable_float_rejected_at_write(self, tenant) -> None:
        """|float| ≥ 1e16 落库后不可复算——写入侧必须拒绝（不毒化链）。"""
        from zhiwei.runtime.events import RunCreated

        sessions, context, run_id = tenant
        from uuid import UUID

        parsed = UUID(run_id)
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            await store.append(
                RunCreated(run_id=parsed, timestamp=utc_now(), graph=_graph()),
                actor_ref="test:value-domain",
                idempotency_key=f"run-created:{run_id}",
            )
            with pytest.raises(Exception, match=r"float|round"):
                await store.append(
                    TaskCompleted(
                        run_id=parsed,
                        timestamp=utc_now(),
                        task_id="t1",
                        output_values={"budget": 1e21},
                    ),
                    actor_ref="test:value-domain",
                    idempotency_key=f"terminal:{run_id}:t1:1",
                )

    async def test_safe_float_roundtrips(self, tenant) -> None:
        """安全域内的 float（JCS 安全整数/短小数）正常往返。"""
        from zhiwei.runtime.events import RunCreated

        sessions, context, run_id = tenant
        from uuid import UUID

        parsed = UUID(run_id)
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            await store.append(
                RunCreated(run_id=parsed, timestamp=utc_now(), graph=_graph()),
                actor_ref="test:value-domain",
                idempotency_key=f"run-created:{run_id}",
            )
            await store.append(
                TaskCompleted(
                    run_id=parsed,
                    timestamp=utc_now(),
                    task_id="t1",
                    output_values={"ratio": 0.25, "count": 42.0},
                ),
                actor_ref="test:value-domain",
                idempotency_key=f"terminal:{run_id}:t1:1",
            )
        async with tenant_session(sessions, context) as session:
            events = await RuntimeEventStore(session, context).load_events(parsed)
        completed = [e for e in events if type(e).__name__ == "TaskCompleted"]
        assert completed and completed[0].output_values["ratio"] == 0.25
