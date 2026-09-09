"""memory router 的 actor 依赖接线契约（S11 followup-2 任务三实测发现）。

缺陷：api/memory.py 的 _authorize_read 以 `actor_dependency()` 裸调用取得
actor——生产组合根传入的 create_session_actor_dependency 是「async 且必需
request 形参」的 FastAPI 依赖（auth.py 冻结签名），裸调用 TypeError →
GET /api/v1/memory/records 500（产品栈实测）。

既有集成测试以零参 lambda 注入 actor（tests/integration/memory/
test_memory_center_api.py _app），bare-call 路径因此在测试中恒可活——本文件
以生产形状依赖（async + request 形参，镜像 create_session_actor_dependency
签名）钉住接线：端点必须经 Depends 消费 actor，PEP 收到的 actor 与端点
上下文同源，而非二次解析。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx2 import ASGITransport, AsyncClient

from zhiwei.api.memory import create_memory_router
from zhiwei.identity.domain import ActorContext, ActorRoleBinding, PrincipalKind
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.client import OPAClient
from zhiwei.policy.enforcement import PolicyEnforcer

pytestmark = pytest.mark.asyncio

ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_URL = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)

_PRINCIPAL = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


class FakeOPA:
    """本地假 OPA（缺省 deny，allow 显式——与 memory_center 契约同型）。"""

    def __init__(self) -> None:
        self.allow = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        allow = self.allow
        return httpx.Response(
            200,
            json={
                "decision_id": f"decision-{'allow' if allow else 'deny'}-1",
                "result": {
                    "allow": allow,
                    "reason": "allow:matrix" if allow else "deny:default_deny:no_rule_matched",
                },
                "provenance": {
                    "version": "1.19.0",
                    "bundles": {"/bundle.tar.gz": {"revision": "bundle-rev-1"}},
                },
            },
            request=request,
        )


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    from alembic import command
    from alembic import config as alembic_config

    repo_root = Path(__file__).resolve().parents[3]
    config = alembic_config.Config(str(repo_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN)
    config.attributes["database_url"] = ADMIN_DSN
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[Any]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant(sessions) -> TenantContext:
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="memory-actor-wiring")
    return context


def _production_shape_actor_dependency(context: TenantContext):
    """生产形状：async + 必需 request 形参（镜像 create_session_actor_dependency）。

    裸调用 actor_dependency() 在此形状下必然 TypeError（缺 request 且未
    await）——这正是生产栈 500 的复现条件。
    """

    async def session_actor(request: Request) -> ActorContext:
        return ActorContext(
            principal_id=_PRINCIPAL,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            kind=PrincipalKind.USER,
            # team_memory.read_authorized cell = {member}（冻结矩阵）——角色
            # 只为让 PEP 放行到 200；本契约钉的是 actor 接线，不是矩阵语义
            role_bindings=(
                ActorRoleBinding(
                    name="member",
                    scope="workspace",
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                ),
            ),
        )

    return session_actor


def _app(context: TenantContext, sessions: Any, policy: FakeOPA) -> FastAPI:
    client = OPAClient(
        "http://opa.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(policy.handler)),
    )
    app = FastAPI()
    app.include_router(
        create_memory_router(
            # Callable[..., T] 不建模 async 可调用（调用返回 Coroutine 而非 T）；
            # FastAPI Depends 的运行时契约接受两种形状——显式 cast 而非把依赖
            # 改成零参（那会复活被本测试钉住的裸调用缺陷）
            actor_dependency=cast(
                "Callable[..., ActorContext]",
                _production_shape_actor_dependency(context),
            ),
            sessions=sessions,
            policy_enforcer=PolicyEnforcer(client),
        )
    )
    return app


async def test_memory_list_authorizes_with_depends_injected_actor(
    tenant: TenantContext, sessions: Any
) -> None:
    policy = FakeOPA()
    transport = ASGITransport(app=_app(tenant, sessions, policy))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/memory/records")
    assert response.status_code == 200, response.text
    assert response.json() == []
