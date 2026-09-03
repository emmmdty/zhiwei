"""S2-T7 契约：Run API——REST 投影绑定 PG 真相（真实 PG + Temporal）。

事实源：specs/s2-agent-runtime.md §5（刷新/断网经 REST 恢复；sandbox run）。

覆盖：
- POST /runs：Planner port → RunCommandService → 内联 dispatch → run 真实执行；
- GET /runs/{id} 与 /events：PG reduce 投影（进程内无缓存）；
- 审批旅程：GET /approvals 出 pending → POST decision（SoD：决策人 ≠ requester
  经 ApprovalRequestStore 守护）→ run 真实完成；
- 跨租户 404。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from zhiwei.api.runs import create_runs_router
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.workers.agent_worker import build_agent_worker

pytestmark = pytest.mark.asyncio

TEMPORAL_TARGET = "127.0.0.1:7233"  # 由 WorkflowEnvironment.start_local() 动态填充


class _FixtureHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "Fixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"task_id": input.task_id})


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
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
    yield


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
    engine = create_database_engine(
        __import__("os")
        .environ.get(
            "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
        )
        .replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="runs-api")

    registry = TaskHandlerRegistry()
    registry.register(_FixtureHandler())
    # 每 stack 独立 task queue：同 queue 的串行重建与 server 端注销存在
    # 注册时序竞争（Registration of multiple workers ... not allowed）
    task_queue = f"runs-api-{uuid4()}"
    worker = build_agent_worker(
        temporal_env.client,  # type: ignore[attr-defined]
        task_queue=task_queue,
        session_factory=sessions,
        handler_registry=registry,
    )
    try:
        yield {
            "sessions": sessions,
            "context": context,
            "worker": worker,
            "env": temporal_env,
            "task_queue": task_queue,
        }
    finally:
        await engine.dispose()


def _actor(context: TenantContext) -> ActorContext:
    return ActorContext(
        principal_id=uuid4(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


def _app(stack: dict) -> FastAPI:
    sessions = stack["sessions"]
    context = stack["context"]

    def sessions_factory(actor, workspace_id):
        return sessions

    app = FastAPI()
    app.include_router(
        create_runs_router(
            actor_dependency=lambda: _actor(context),
            sessions_factory=sessions_factory,
            temporal_target=str(stack["env"].client.service_config.target_host)
            if hasattr(stack["env"].client, "service_config")
            else "127.0.0.1:7233",
        )
    )
    return app


def _target(stack: dict) -> str:
    """从 WorkflowEnvironment 提取前端地址（私有能力，测试专用）。"""
    client = stack["env"].client
    return str(client.config()["service_client"].config.target_host)


class TestRunJourney:
    async def test_create_run_executes_and_rest_projection_recovers(
        self, stack
    ) -> None:
        app = _app(stack)
        # dispatch_inline 需要 temporal target；直接用 env client 的 sender
        # （工厂签名兼容：runs router 内部 Client.connect(temporal_target)）
        stack["temporal_target"] = _target(stack)
        app = FastAPI()
        app.include_router(
            create_runs_router(
                actor_dependency=lambda: _actor(stack["context"]),
                sessions_factory=lambda actor, ws: stack["sessions"],
                temporal_target=stack["temporal_target"],
                task_queue=stack["task_queue"],
            )
        )
        context = stack["context"]
        transport = ASGITransport(app=app)
        async with stack["worker"], AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
                created = await client.post(
                    "/api/v1/runs",
                    json={"template": "single-fixture", "workspace_id": str(context.workspace_id)},
                )
                assert created.status_code == 201, created.text
                run_id = created.json()["run_id"]

                # run 真实执行 → REST 投影恢复终态（无进程内缓存）
                deadline = asyncio.get_event_loop().time() + 30
                detail = None
                while asyncio.get_event_loop().time() < deadline:
                    response = await client.get(f"/api/v1/runs/{run_id}")
                    assert response.status_code == 200
                    detail = response.json()
                    if detail["status"] in {"completed", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(0.2)
                assert detail is not None and detail["status"] == "completed", detail

                events_response = await client.get(f"/api/v1/runs/{run_id}/events")
                assert events_response.status_code == 200
                events = events_response.json()
                types = [e["event_type"] for e in events]
                assert "RunCreated" in types and "RunCompleted" in types

    async def test_approval_journey_via_rest(self, stack) -> None:
        """single-fixture 之外：approval-chain 模板 + REST 决策 → run 完成。"""
        context = stack["context"]
        stack["temporal_target"] = _target(stack)
        app = FastAPI()
        app.include_router(
            create_runs_router(
                actor_dependency=lambda: _actor(context),
                sessions_factory=lambda actor, ws: stack["sessions"],
                temporal_target=stack["temporal_target"],
                task_queue=stack["task_queue"],
            )
        )
        transport = ASGITransport(app=app)
        # 决策人 ≠ requester：requester 记为 "agent-runtime"，决策人是 API actor
        async with stack["worker"], AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
                created = await client.post(
                    "/api/v1/runs",
                    json={"template": "approval-chain", "workspace_id": str(context.workspace_id)},
                )
                assert created.status_code == 201, created.text
                run_id = created.json()["run_id"]

                # 等 pending 审批请求出现
                deadline = asyncio.get_event_loop().time() + 30
                approvals = []
                while asyncio.get_event_loop().time() < deadline:
                    listing = await client.get(f"/api/v1/runs/{run_id}/approvals")
                    approvals = listing.json()
                    if approvals:
                        break
                    await asyncio.sleep(0.2)
                assert approvals, "pending approval never surfaced via REST"
                request_id = approvals[0]["request_id"]

                decision = await client.post(
                    f"/api/v1/runs/{run_id}/approvals/{request_id}/decision",
                    json={"decision": "approved", "reason": "ok"},
                )
                assert decision.status_code == 200, decision.text

                deadline = asyncio.get_event_loop().time() + 30
                detail = None
                while asyncio.get_event_loop().time() < deadline:
                    detail = (await client.get(f"/api/v1/runs/{run_id}")).json()
                    if detail["status"] in {"completed", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(0.2)
                assert detail is not None and detail["status"] == "completed", detail

    async def test_invalid_decision_is_422(self, stack) -> None:
        stack["temporal_target"] = _target(stack)
        context = stack["context"]
        app = FastAPI()
        app.include_router(
            create_runs_router(
                actor_dependency=lambda: _actor(context),
                sessions_factory=lambda actor, ws: stack["sessions"],
                temporal_target=stack["temporal_target"],
                task_queue=stack["task_queue"],
            )
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/runs/{uuid4()}/approvals/{uuid4()}/decision",
                json={"decision": "maybe"},
            )
            assert response.status_code == 422

    async def test_cross_tenant_get_run_is_404(self, stack) -> None:
        context = stack["context"]
        other = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        app = FastAPI()
        app.include_router(
            create_runs_router(
                actor_dependency=lambda: ActorContext(
                    principal_id=uuid4(),
                    organization_id=other.organization_id,
                    workspace_id=other.workspace_id,
                ),
                sessions_factory=lambda actor, ws: stack["sessions"],
                temporal_target=_target(stack),
            )
        )
        # 他人租户下同 id run 不存在（租户 A 的 run 不对租户 B 可见）
        run_id = uuid4()
        async with tenant_session(stack["sessions"], context) as session:
            from sqlalchemy import text

            await session.execute(
                text(
                    "INSERT INTO runs (id, organization_id, workspace_id, status, schema_version)"
                    " VALUES (:id, :org, :ws, 'created', 1)"
                ),
                {"id": run_id, "org": context.organization_id, "ws": context.workspace_id},
            )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{run_id}")
            assert response.status_code == 404
