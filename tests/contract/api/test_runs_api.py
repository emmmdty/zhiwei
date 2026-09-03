"""S2-T7 契约：Run API——REST 投影绑定 PG 真相（真实 PG + Temporal）。

事实源：specs/s2-agent-runtime.md §5（刷新/断网经 REST 恢复；sandbox run）+ 2026-09-03
复审增补（ADR-012）：审批 requester 必须为真实 human principal（SoD 经 REST 反例）、
POST /runs 的 body workspace_id 必须经成员校验（客户端声明不是授权事实）。

原契约修订说明（S2 修复轮 RED）：
- 原 test_approval_journey_via_rest 允许「创建者本人批准自己的审批」——其 docstring
  自述「requester 记为 'agent-runtime'，决策人是 API actor」，即把 H-1（SoD 生产失效）
  固化成了测试契约。按 specs/s2 §4（2026-09-03 增补）修订：requester 必须穿透为创建者
  principal，创建者本人决策必须被拒。
- 原 POST /runs 测试不做 workspace 成员校验——修订：actor 必须持有目标 workspace 的
  membership 行（组织内无成员资格 403；跨 org workspace 404 防枚举）。

覆盖：
- POST /runs：Planner port → RunCommandService → 内联 dispatch → run 真实执行；
- GET /runs/{id} 与 /events：PG reduce 投影（进程内无缓存）；
- 审批旅程：GET /approvals 出 pending（requester = 创建者 principal）→ SoD：本人决策
  409 → 他人 POST decision → run 真实完成；
- 跨租户 404。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from zhiwei.api.runs import create_runs_router
from zhiwei.identity.domain import ActorContext
from zhiwei.identity.sessions import MembershipScopeError
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
async def identity_sessions() -> AsyncIterator[Any]:
    """identity 引擎会话（principals 的 INSERT 自 0003 起为 zhiwei_identity 专属）。"""
    import os

    dsn = os.environ.get(
        "ZHIWEI_TEST_IDENTITY_DSN", "postgresql://zhiwei_identity@127.0.0.1:55432/zhiwei_test"
    )
    engine = create_database_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    sessions = create_session_factory(engine)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def stack(temporal_env, identity_sessions) -> AsyncIterator[dict]:
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
            "identity_sessions": identity_sessions,
            "context": context,
            "worker": worker,
            "env": temporal_env,
            "task_queue": task_queue,
        }
    finally:
        await engine.dispose()


async def _grant_workspace_membership(
    identity_sessions, sessions, context: TenantContext, principal_id: UUID
) -> None:
    """给 actor 落 principal + workspace membership 行（授权事实，非客户端声明）。

    principals 的 INSERT 是 identity 引擎专属（0003 收回 app 直写）；workspace_memberships
    受 FORCE RLS，必须在 tenant context 内经 app 会话写。
    """
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


def _table_backed_authorizer(sessions) -> Any:
    """与 app.py 的 resolve_context 同语义的成员校验（表级直查，S1 语义已另行覆盖）。"""

    async def authorize(actor: ActorContext, workspace_id: UUID) -> None:
        assert actor.organization_id is not None  # 端点已保证 org context
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=workspace_id
        )
        async with tenant_session(sessions, context) as session:
            row = await session.execute(
                text(
                    "SELECT 1 FROM workspace_memberships"
                    " WHERE principal_id = :pid AND organization_id = :org"
                    " AND workspace_id = :ws"
                ),
                {
                    "pid": actor.principal_id,
                    "org": actor.organization_id,
                    "ws": workspace_id,
                },
            )
            if row.scalar_one_or_none() is None:
                raise MembershipScopeError(
                    "principal has no workspace membership in the declared organization"
                )

    return authorize


def _actor(context: TenantContext, principal_id: UUID) -> ActorContext:
    return ActorContext(
        principal_id=principal_id,
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


def _runs_app(stack: dict, actor: ActorContext, *, queue: str | None = None) -> FastAPI:
    sessions = stack["sessions"]

    def sessions_factory(actor, workspace_id):
        return sessions

    app = FastAPI()
    app.include_router(
        create_runs_router(
            actor_dependency=lambda: actor,
            sessions_factory=sessions_factory,
            temporal_target=_target(stack),
            task_queue=queue or stack["task_queue"],
            workspace_authorizer=_table_backed_authorizer(sessions),
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
        creator = uuid4()
        await _grant_workspace_membership(stack["identity_sessions"], stack["sessions"], stack["context"], creator)
        app = _runs_app(stack, _actor(stack["context"], creator))
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

    async def test_create_run_rejects_actor_without_workspace_membership(
        self, stack
    ) -> None:
        """body workspace_id 是授权事实的声明，不是授权事实本身：无成员资格 403。

        S2 修复轮 RED（H-6）：org 内 workspace 存在，但 actor 没有该 workspace 的
        membership——POST /runs 必须拒绝（此前仅校验 workspace 属于本 org）。
        """
        outsider = uuid4()  # 有 org context，但无 workspace membership 行
        app = _runs_app(stack, _actor(stack["context"], outsider))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/runs",
                json={
                    "template": "single-fixture",
                    "workspace_id": str(stack["context"].workspace_id),
                },
            )
            assert created.status_code == 403, created.text

    async def test_create_run_cross_org_workspace_is_404(self, stack) -> None:
        """跨 org workspace 统一 404（防枚举：与不存在的 workspace 不可区分）。"""
        sessions = stack["sessions"]
        other = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        assert other.organization_id is not None and other.workspace_id is not None
        async with tenant_session(sessions, other) as session:
            repository = TenantRepository(session, other)
            await repository.create_organization(other.organization_id, status="active")
            await repository.create_workspace(other.workspace_id, name="other-org-ws")

        creator = uuid4()
        await _grant_workspace_membership(stack["identity_sessions"], sessions, stack["context"], creator)
        app = _runs_app(stack, _actor(stack["context"], creator))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/runs",
                json={
                    "template": "single-fixture",
                    "workspace_id": str(other.workspace_id),
                },
            )
            assert created.status_code == 404, created.text


class TestApprovalSoD:
    """审批 SoD 经 REST 决策路径（spec §6【I】：requester 本人 approve 被拒）。"""

    async def test_requester_cannot_approve_own_request(
        self, stack
    ) -> None:
        creator = uuid4()
        await _grant_workspace_membership(stack["identity_sessions"], stack["sessions"], stack["context"], creator)
        context = stack["context"]
        app = _runs_app(stack, _actor(context, creator))
        transport = ASGITransport(app=app)
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

            # requester 必须穿透为创建者 principal（不是常量 agent-runtime）
            assert approvals[0]["requester"] == str(creator), approvals[0]

            # 创建者本人批准自己的审批：SoD 拒绝（409 冲突语义）
            self_decision = await client.post(
                f"/api/v1/runs/{run_id}/approvals/{request_id}/decision",
                json={"decision": "approved", "reason": "self approve"},
            )
            assert self_decision.status_code == 409, self_decision.text
            assert "different human principal" in self_decision.json()["detail"]

    async def test_other_principal_can_approve_and_run_completes(
        self, stack
    ) -> None:
        creator, approver = uuid4(), uuid4()
        sessions = stack["sessions"]
        context = stack["context"]
        await _grant_workspace_membership(stack["identity_sessions"], sessions, context, creator)
        await _grant_workspace_membership(stack["identity_sessions"], sessions, context, approver)
        creator_app = _runs_app(stack, _actor(context, creator))
        approver_app = _runs_app(stack, _actor(context, approver))
        async with stack["worker"], AsyncClient(
            transport=ASGITransport(app=creator_app), base_url="http://test"
        ) as creating, AsyncClient(
            transport=ASGITransport(app=approver_app), base_url="http://test"
        ) as deciding:
            created = await creating.post(
                "/api/v1/runs",
                json={"template": "approval-chain", "workspace_id": str(context.workspace_id)},
            )
            assert created.status_code == 201, created.text
            run_id = created.json()["run_id"]

            deadline = asyncio.get_event_loop().time() + 30
            approvals = []
            while asyncio.get_event_loop().time() < deadline:
                listing = await deciding.get(f"/api/v1/runs/{run_id}/approvals")
                approvals = listing.json()
                if approvals:
                    break
                await asyncio.sleep(0.2)
            assert approvals, "pending approval never surfaced via REST"
            assert approvals[0]["requester"] == str(creator), approvals[0]
            request_id = approvals[0]["request_id"]

            decision = await deciding.post(
                f"/api/v1/runs/{run_id}/approvals/{request_id}/decision",
                json={"decision": "approved", "reason": "ok"},
            )
            assert decision.status_code == 200, decision.text

            deadline = asyncio.get_event_loop().time() + 30
            detail = None
            while asyncio.get_event_loop().time() < deadline:
                detail = (await deciding.get(f"/api/v1/runs/{run_id}")).json()
                if detail["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.2)
            assert detail is not None and detail["status"] == "completed", detail


class TestValidationAndTenancy:
    async def test_invalid_decision_is_422(self, stack) -> None:
        app = _runs_app(stack, _actor(stack["context"], uuid4()))
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
        app = _runs_app(
            stack,
            ActorContext(
                principal_id=uuid4(),
                organization_id=other.organization_id,
                workspace_id=other.workspace_id,
            ),
        )
        # 他人租户下同 id run 不存在（租户 A 的 run 不对租户 B 可见）
        run_id = uuid4()
        async with tenant_session(stack["sessions"], context) as session:
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
