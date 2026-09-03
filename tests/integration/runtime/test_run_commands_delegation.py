"""S2 修复轮批次 B RED（H-7 运行时侧）：delegation_chain 深度硬上界的命令层执行。

事实源：specs/s2-agent-runtime.md §3（2026-09-03 增补，ADR-008 可判定化）——
「max_delegation_depth 硬上界」；运行时硬上界是发布期环检测的纵深防御（防
发布校验被绕过或图在发布后被篡改）。命令提交侧必须 fail closed：超出硬上界
的链在 Run 行写入前拒绝。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from zhiwei.persistence.run_commands import RunCommandError, RunCommandService
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.delegation import MAX_DELEGATION_DEPTH

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
async def tenant() -> AsyncIterator[tuple[object, TenantContext]]:
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
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="delegation-cap")
    try:
        yield sessions, context
    finally:
        await engine.dispose()


def _graph_payload() -> dict[str, object]:
    from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode

    graph = TaskGraph(
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
    return graph.model_dump(mode="json")


class TestDelegationDepthCapAtCommandLayer:
    async def test_chain_beyond_hard_cap_rejected_before_run_row(self, tenant) -> None:
        sessions, context = tenant
        run_id = uuid4()
        chain = [f"link-{i}" for i in range(MAX_DELEGATION_DEPTH + 1)]
        async with tenant_session(sessions, context) as session:
            service = RunCommandService(session, context)
            with pytest.raises(RunCommandError, match="delegation"):
                await service.submit_start_run(
                    run_id=run_id,
                    graph=_graph_payload(),
                    task_queue="zhiwei-agent-runtime",
                    delegation_chain=chain,
                )

    async def test_chain_within_hard_cap_accepted(self, tenant) -> None:
        sessions, context = tenant
        run_id = uuid4()
        chain = [f"link-{i}" for i in range(MAX_DELEGATION_DEPTH)]
        async with tenant_session(sessions, context) as session:
            service = RunCommandService(session, context)
            await service.submit_start_run(
                run_id=run_id,
                graph=_graph_payload(),
                task_queue="zhiwei-agent-runtime",
                delegation_chain=chain,
            )
