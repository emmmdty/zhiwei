"""S10 fix-A RED：pack 模板 run 经生产 Run API 创建并真实执行（R1 REJECT D4）。

事实源：POST /api/v1/runs {template, workspace_id} 是 run 创建的生产入口
（src/zhiwei/api/runs.py + RunCommandService）；web 侧 T1 绑定（renderers/
registry.ts registerRunBinding）按 templateId 解析 App——run 投影必须携带
template（D2），pack 模板 run 必须以 fixture 绑定真实执行到终态（D4）。

覆盖：
- ask-v1 / change-brief（有 fixture 绑定的注册 pack 模板）→ 201 → 生产工作流
  执行到 completed；详情/列表投影携带 template（caller-declared 绑定，
  创建期持久化）与 mode=fixture（fixture 资格诚实标注）；
- discover-v1（注册但无 fixture 绑定）→ 422 machine reason，run 拒绝于创建期；
- 未知模板 → 既有 unknown fixture template 422 语义不变；
- worker 侧按 pack 队列组合 pack handler 注册表（TaskHandlerRegistry 公共机制），
  与 T6 eval 环境同款——没有 pack 专属 Core handler/DB 列/API route。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import text

from zhiwei.api.runs import create_runs_router
from zhiwei.evals.pack_templates import (
    PackTemplatePlanSource,
    pack_template_handler_registry,
    pack_template_queue,
)
from zhiwei.identity.domain import ActorContext
from zhiwei.identity.sessions import MembershipScopeError
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.runtime.planner import FixturePlanner
from zhiwei.workers.agent_worker import build_agent_worker

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    import os
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[3]
    dsn = os.environ.get(
        "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
    )
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", dsn)
    config.attributes["database_url"] = dsn
    command.upgrade(config, "head")


@pytest_asyncio.fixture(scope="module")
async def temporal_env() -> AsyncIterator[object]:
    from temporalio.testing import WorkflowEnvironment

    env = await WorkflowEnvironment.start_local()
    try:
        yield env
    finally:
        await env.shutdown()


@pytest_asyncio.fixture
async def stack(temporal_env) -> AsyncIterator[dict]:
    import os

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
        await repository.create_workspace(workspace_id, name="pack-template-runs")

    # worker 侧组合：pack 绑定队列 + pack handler 注册表（公共 TaskHandlerRegistry
    # 机制）。每个注册模板独立队列——单一默认队列无法同时服务两个 pack（handler
    # 语义互斥）；这正是 pack_templates 把队列作为绑定数据的原因。
    workers = []
    for template_id in ("ask-v1", "change-brief"):
        registry: TaskHandlerRegistry = pack_template_handler_registry(template_id)
        workers.append(
            build_agent_worker(
                temporal_env.client,  # type: ignore[attr-defined]
                task_queue=pack_template_queue(template_id),
                session_factory=sessions,
                handler_registry=registry,
            )
        )
    try:
        yield {
            "sessions": sessions,
            "context": context,
            "env": temporal_env,
            "workers": workers,
        }
    finally:
        await engine.dispose()


def _actor(context: TenantContext, principal_id: UUID) -> ActorContext:
    return ActorContext(
        principal_id=principal_id,
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


async def _grant_workspace_membership(
    identity_sessions: Any, sessions: Any, context: TenantContext, principal_id: UUID
) -> None:
    async with identity_sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, kind, status, schema_version)"
                " VALUES (:id, 'user', 'active', 1)"
            ),
            {"id": principal_id},
        )
    async with tenant_session(sessions, context) as session:
        await session.execute(
            text(
                "INSERT INTO workspace_memberships"
                " (principal_id, organization_id, workspace_id, role_bindings)"
                " VALUES (:pid, :org, :ws, '[]'::jsonb)"
            ),
            {"pid": principal_id, "org": context.organization_id, "ws": context.workspace_id},
        )


def _identity_sessions() -> tuple[Any, Any]:
    import os

    dsn = os.environ.get(
        "ZHIWEI_TEST_IDENTITY_DSN", "postgresql://zhiwei_identity@127.0.0.1:55432/zhiwei_test"
    )
    engine = create_database_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    return engine, create_session_factory(engine)


def _runs_app(stack: dict, actor: ActorContext) -> FastAPI:
    sessions = stack["sessions"]

    def sessions_factory(actor_: Any, workspace_id: Any) -> Any:
        return sessions

    async def authorize(actor_: ActorContext, workspace_id: UUID) -> None:
        assert actor_.organization_id is not None
        context = TenantContext(
            organization_id=actor_.organization_id, workspace_id=workspace_id
        )
        async with tenant_session(sessions, context) as session:
            row = await session.execute(
                text(
                    "SELECT 1 FROM workspace_memberships"
                    " WHERE principal_id = :pid AND organization_id = :org"
                    " AND workspace_id = :ws"
                ),
                {
                    "pid": actor_.principal_id,
                    "org": actor_.organization_id,
                    "ws": workspace_id,
                },
            )
            if row.scalar_one_or_none() is None:
                raise MembershipScopeError("no membership")

    app = FastAPI()
    app.include_router(
        create_runs_router(
            actor_dependency=lambda: actor,
            sessions_factory=sessions_factory,
            temporal_target=_target(stack),
            # 生产组合（app.py 同款）：pack 计划源经组合根注入 planner port
            planner=FixturePlanner(pack_plans=PackTemplatePlanSource()),
            workspace_authorizer=authorize,
        )
    )
    return app


def _target(stack: dict) -> str:
    client = stack["env"].client
    return str(client.config()["service_client"].config.target_host)


async def _wait_terminal(client: AsyncClient, run_id: str) -> dict:
    deadline = asyncio.get_event_loop().time() + 60
    detail = None
    while asyncio.get_event_loop().time() < deadline:
        response = await client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        detail = response.json()
        if detail["status"] in {"completed", "failed", "cancelled"}:
            return detail
        await asyncio.sleep(0.2)
    raise AssertionError(f"pack run did not reach terminal state: {detail}")


@pytest.mark.usefixtures("stack")
class TestPackTemplateRunOrigination:
    @pytest_asyncio.fixture
    async def client(self, stack: dict) -> AsyncIterator[AsyncClient]:
        creator = uuid4()
        identity_engine, identity_sessions = _identity_sessions()
        try:
            await _grant_workspace_membership(
                identity_sessions, stack["sessions"], stack["context"], creator
            )
        finally:
            await identity_engine.dispose()
        app = _runs_app(stack, _actor(stack["context"], creator))
        async with stack["workers"][0], stack["workers"][1], AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client

    async def test_ask_v1_run_executes_and_projects_template(
        self, client: AsyncClient, stack: dict
    ) -> None:
        created = await client.post(
            "/api/v1/runs",
            json={
                "template": "ask-v1",
                "workspace_id": str(stack["context"].workspace_id),
            },
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["run_id"]

        detail = await _wait_terminal(client, run_id)
        assert detail["status"] == "completed", detail
        # D2：run 投影携带 caller-declared 绑定（创建期持久化，刷新可恢复）
        assert detail["template"] == "ask-v1"
        # fixture 资格诚实标注：pack 模板 run 是 fixture 绑定执行（无 live source）
        assert detail["mode"] == "fixture"
        assert detail["tasks"], "ask pack 拓扑必须真实执行（任务非空）"

        listing = (await client.get("/api/v1/runs")).json()
        projected = [r for r in listing if r["run_id"] == run_id]
        assert projected and projected[0]["template"] == "ask-v1"

        events = (await client.get(f"/api/v1/runs/{run_id}/events")).json()
        types = [e["event_type"] for e in events]
        assert "RunCreated" in types and "RunCompleted" in types

    async def test_change_brief_run_executes_and_projects_template(
        self, client: AsyncClient, stack: dict
    ) -> None:
        created = await client.post(
            "/api/v1/runs",
            json={
                "template": "change-brief",
                "workspace_id": str(stack["context"].workspace_id),
            },
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["run_id"]

        detail = await _wait_terminal(client, run_id)
        assert detail["status"] == "completed", detail
        assert detail["template"] == "change-brief"
        assert detail["mode"] == "fixture"
        # T6 pack 拓扑：fixture unit 前缀隔离的任务真实执行
        assert any(task_id.startswith("mixed-refs/") for task_id in detail["tasks"])

        listing = (await client.get("/api/v1/runs")).json()
        projected = [r for r in listing if r["run_id"] == run_id]
        assert projected and projected[0]["template"] == "change-brief"

    async def test_discover_v1_without_fixture_bindings_is_refused_at_creation(
        self, client: AsyncClient, stack: dict
    ) -> None:
        created = await client.post(
            "/api/v1/runs",
            json={
                "template": "discover-v1",
                "workspace_id": str(stack["context"].workspace_id),
            },
        )
        # fail closed：注册但无 fixture 绑定的 pack 模板在创建期以 machine reason
        # 拒绝——绝不让 run 带着无 handler 的图进入 worker 侧崩溃。
        assert created.status_code == 422, created.text
        assert "discover-v1" in created.json()["detail"]
        assert "fixture" in created.json()["detail"]

    async def test_unknown_template_keeps_existing_machine_reason(
        self, client: AsyncClient, stack: dict
    ) -> None:
        created = await client.post(
            "/api/v1/runs",
            json={
                "template": "no-such-template",
                "workspace_id": str(stack["context"].workspace_id),
            },
        )
        assert created.status_code == 422, created.text
        assert "no-such-template" in created.json()["detail"]
