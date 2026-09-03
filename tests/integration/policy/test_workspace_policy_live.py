"""S2 修复轮 RED（H-2）：workspace 生命周期在真实 OPA 策略下的可运营性。

事实源：specs/s1-tenancy-policy.md §3（2026-09-03 增补，ADR-012）——workspace 创建走
`workspace_policy.configure`（org 作用域，org_owner）；创建者自动获得 workspace_admin
workspace membership（bootstrap 路径，与 org 创建自动授予 owner 对称）；workspace
membership 管理 API 使 workspace_admin 可产生。

复审反例（驱动本文件）：
- api/workspaces.py 用 `configure_workspace`（workspace_admin、要求 workspace 上下文），
  而创建时 workspace 尚不存在 → 真实 OPA 下恒 deny；此前集成测试用 FakeOPA（恒
  allow）掩盖（ADR-012 反例 4）。

与 tests/integration/policy/test_opa_client_live.py 的约定一致：本文件要求 opa 服务
已在 127.0.0.1:8181 运行；服务不可达时测试直接失败（不是跳过）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from zhiwei.api.workspaces import create_workspaces_router
from zhiwei.identity.domain import ActorContext, ActorRoleBinding
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.client import OPAClient
from zhiwei.policy.enforcement import PolicyEnforcer

pytestmark = pytest.mark.asyncio

OPA_URL = "http://127.0.0.1:8181"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _require_opa_healthy() -> None:
    try:
        resp = httpx.get(f"{OPA_URL}/health?bundles", timeout=5.0)
    except httpx.HTTPError as exc:  # pragma: no cover - 环境守卫失败路径
        raise RuntimeError(
            f"opa 服务不可达（{OPA_URL}）。先运行: "
            "docker compose -f deploy/compose/compose.test.yaml --profile identity up -d --wait opa"
        ) from exc
    assert resp.status_code == 200, f"/health?bundles 必须 200，实际 {resp.status_code}"


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
    _require_opa_healthy()
    yield


@pytest_asyncio.fixture
async def identity_sessions() -> AsyncIterator[object]:
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
async def stack(identity_sessions) -> AsyncIterator[dict]:
    engine = create_database_engine(
        os.environ.get(
            "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
        ).replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    sessions = create_session_factory(engine)
    organization_id = uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=None)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
    try:
        yield {
            "sessions": sessions,
            "identity_sessions": identity_sessions,
            "organization_id": organization_id,
        }
    finally:
        await engine.dispose()


async def _create_principal(identity_sessions, principal_id: UUID) -> None:
    async with identity_sessions.begin() as session:  # type: ignore[attr-defined]
        await session.execute(
            text(
                "INSERT INTO principals (id, kind, status, schema_version)"
                " VALUES (:id, 'user', 'active', 1)"
            ),
            {"id": principal_id},
        )


def _org_owner(stack: dict, principal_id: UUID) -> ActorContext:
    org = stack["organization_id"]
    return ActorContext(
        principal_id=principal_id,
        organization_id=org,
        role_bindings=(
            ActorRoleBinding(name="org_owner", scope="org", organization_id=org),
        ),
    )


def _app(stack: dict, actor: ActorContext) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: actor,
            sessions=stack["sessions"],
            policy_enforcer=PolicyEnforcer(OPAClient(OPA_URL)),
        )
    )
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestWorkspaceLifecycleUnderRealPolicy:
    async def test_org_owner_can_create_workspace(self, stack) -> None:
        """org_owner 创建 workspace：真实 OPA 矩阵下必须 allow（201）。

        修复前：API 使用 configure_workspace（要求 workspace 上下文的
        workspace_admin），创建时无 workspace → 恒 deny（403）。
        """
        owner = uuid4()
        await _create_principal(stack["identity_sessions"], owner)
        app = _app(stack, _org_owner(stack, owner))
        org = stack["organization_id"]
        workspace_id = uuid4()
        async with _client(app) as client:
            response = await client.post(
                f"/api/v1/organizations/{org}/workspaces",
                json={"workspace_id": str(workspace_id), "name": "owner-created"},
                headers={"Idempotency-Key": f"ws-create-{workspace_id}"},
            )
        assert response.status_code == 201, response.text

    async def test_workspace_creation_grants_creator_workspace_admin(
        self, stack
    ) -> None:
        """创建者自动获得 workspace_admin membership（bootstrap 路径）。

        没有该授权，manage_workspace_members（workspace_admin）永远无人持有，
        workspace 生命周期不可运营（s1 spec §3 增补）。
        """
        owner = uuid4()
        await _create_principal(stack["identity_sessions"], owner)
        app = _app(stack, _org_owner(stack, owner))
        org = stack["organization_id"]
        workspace_id = uuid4()
        async with _client(app) as client:
            response = await client.post(
                f"/api/v1/organizations/{org}/workspaces",
                json={"workspace_id": str(workspace_id), "name": "bootstrap"},
                headers={"Idempotency-Key": f"ws-create-{workspace_id}"},
            )
            assert response.status_code == 201, response.text

        ws_context = TenantContext(organization_id=org, workspace_id=workspace_id)
        async with tenant_session(stack["sessions"], ws_context) as session:
            roles = await session.execute(
                text(
                    "SELECT role_bindings FROM workspace_memberships"
                    " WHERE principal_id = :pid AND workspace_id = :ws"
                ),
                {"pid": owner, "ws": workspace_id},
            )
            row = roles.scalar_one_or_none()
        assert row is not None, "creator must hold a workspace membership after creation"
        assert "workspace_admin" in (row or []), row

    async def test_workspace_admin_can_grant_workspace_membership(
        self, stack
    ) -> None:
        """workspace_admin 经管理 API 授予 workspace membership（agent_builder）。"""
        owner, builder = uuid4(), uuid4()
        await _create_principal(stack["identity_sessions"], owner)
        await _create_principal(stack["identity_sessions"], builder)
        org = stack["organization_id"]

        # journey 前置：owner 创建 workspace（自动获得 workspace_admin）
        owner_app = _app(stack, _org_owner(stack, owner))
        workspace_id = uuid4()
        async with _client(owner_app) as client:
            response = await client.post(
                f"/api/v1/organizations/{org}/workspaces",
                json={"workspace_id": str(workspace_id), "name": "grant-journey"},
                headers={"Idempotency-Key": f"ws-create-{workspace_id}"},
            )
            assert response.status_code == 201, response.text

        # workspace_admin（创建者）授予 builder workspace membership
        admin_context = ActorContext(
            principal_id=owner,
            organization_id=org,
            workspace_id=workspace_id,
            role_bindings=(
                ActorRoleBinding(name="org_owner", scope="org", organization_id=org),
                ActorRoleBinding(
                    name="workspace_admin",
                    scope="workspace",
                    organization_id=org,
                    workspace_id=workspace_id,
                ),
            ),
        )
        admin_app = _app(stack, admin_context)
        async with _client(admin_app) as client:
            response = await client.post(
                f"/api/v1/workspaces/{workspace_id}/memberships",
                json={"principal_id": str(builder), "role_bindings": ["agent_builder"]},
                headers={"Idempotency-Key": f"ws-member-{builder}"},
            )
        assert response.status_code == 201, response.text

        # 授权事实落库
        ws_context = TenantContext(organization_id=org, workspace_id=workspace_id)
        async with tenant_session(stack["sessions"], ws_context) as session:
            roles = await session.execute(
                text(
                    "SELECT role_bindings FROM workspace_memberships"
                    " WHERE principal_id = :pid AND workspace_id = :ws"
                ),
                {"pid": builder, "ws": workspace_id},
            )
            row = roles.scalar_one_or_none()
        assert row is not None and "agent_builder" in row, row

    async def test_org_member_cannot_create_workspace(self, stack) -> None:
        """普通 member 不能创建 workspace（矩阵语义回归：只有 org_owner 配置）。"""
        member = uuid4()
        await _create_principal(stack["identity_sessions"], member)
        org = stack["organization_id"]
        actor = ActorContext(
            principal_id=member,
            organization_id=org,
            role_bindings=(
                ActorRoleBinding(name="member", scope="org", organization_id=org),
            ),
        )
        app = _app(stack, actor)
        workspace_id = uuid4()
        async with _client(app) as client:
            response = await client.post(
                f"/api/v1/organizations/{org}/workspaces",
                json={"workspace_id": str(workspace_id), "name": "member-denied"},
                headers={"Idempotency-Key": f"ws-create-{workspace_id}"},
            )
        assert response.status_code == 403, response.text

    async def _owner_created_workspace(self, stack) -> tuple[UUID, UUID]:
        """journey 前置：owner 创建 workspace 并返回 (owner, workspace_id)。"""
        owner = uuid4()
        await _create_principal(stack["identity_sessions"], owner)
        app = _app(stack, _org_owner(stack, owner))
        org = stack["organization_id"]
        workspace_id = uuid4()
        async with _client(app) as client:
            response = await client.post(
                f"/api/v1/organizations/{org}/workspaces",
                json={"workspace_id": str(workspace_id), "name": "read-authz"},
                headers={"Idempotency-Key": f"ws-create-{workspace_id}"},
            )
            assert response.status_code == 201, response.text
        return owner, workspace_id

    async def test_membership_listing_read_authorization(self, stack) -> None:
        """membership 列表读路径授权（ADR-012 决策 4；s1 spec §5 读路径 IDOR）。

        - 普通 member（无任何 workspace 角色）→ 403：member 只能读自身，
          不得枚举全量成员+角色绑定（D-1 反例：修复前 200 全量）
        - workspace_admin 绑定在另一 workspace → 403（workspace 作用域不匹配）
        - org_owner（org 上下文）→ 200
        - security_admin（org 上下文）→ 200
        """
        org = stack["organization_id"]
        owner, workspace_id = await self._owner_created_workspace(stack)

        member = uuid4()
        await _create_principal(stack["identity_sessions"], member)
        other_ws_admin = uuid4()
        await _create_principal(stack["identity_sessions"], other_ws_admin)
        security_admin = uuid4()
        await _create_principal(stack["identity_sessions"], security_admin)

        member_actor = ActorContext(
            principal_id=member,
            organization_id=org,
            role_bindings=(
                ActorRoleBinding(name="member", scope="org", organization_id=org),
            ),
        )
        other_ws_admin_actor = ActorContext(
            principal_id=other_ws_admin,
            organization_id=org,
            role_bindings=(
                ActorRoleBinding(
                    name="workspace_admin",
                    scope="workspace",
                    organization_id=org,
                    workspace_id=uuid4(),  # 绑定在别的 workspace
                ),
            ),
        )
        security_admin_actor = ActorContext(
            principal_id=security_admin,
            organization_id=org,
            role_bindings=(
                ActorRoleBinding(name="security_admin", scope="org", organization_id=org),
            ),
        )

        async with _client(_app(stack, member_actor)) as client:
            response = await client.get(f"/api/v1/workspaces/{workspace_id}/memberships")
        assert response.status_code == 403, response.text

        async with _client(_app(stack, other_ws_admin_actor)) as client:
            response = await client.get(f"/api/v1/workspaces/{workspace_id}/memberships")
        assert response.status_code == 403, response.text

        # org_owner（创建者，org 上下文）与 security_admin 可读
        async with _client(_app(stack, _org_owner(stack, owner))) as client:
            response = await client.get(f"/api/v1/workspaces/{workspace_id}/memberships")
        assert response.status_code == 200, response.text
        listing = response.json()
        assert any(str(entry["principal_id"]) == str(owner) for entry in listing), listing

        async with _client(_app(stack, security_admin_actor)) as client:
            response = await client.get(f"/api/v1/workspaces/{workspace_id}/memberships")
        assert response.status_code == 200, response.text

    async def test_plain_member_cannot_grant_workspace_membership(self, stack) -> None:
        """非 admin 授予 membership 被真实策略拒绝（manage_workspace_members 反例）。"""
        org = stack["organization_id"]
        _owner, workspace_id = await self._owner_created_workspace(stack)

        member, victim = uuid4(), uuid4()
        await _create_principal(stack["identity_sessions"], member)
        await _create_principal(stack["identity_sessions"], victim)
        member_actor = ActorContext(
            principal_id=member,
            organization_id=org,
            role_bindings=(
                ActorRoleBinding(name="member", scope="org", organization_id=org),
            ),
        )
        async with _client(_app(stack, member_actor)) as client:
            response = await client.post(
                f"/api/v1/workspaces/{workspace_id}/memberships",
                json={"principal_id": str(victim), "role_bindings": ["agent_builder"]},
                headers={"Idempotency-Key": f"member-grant-{victim}"},
            )
        assert response.status_code == 403, response.text


class TestWorkspacePolicyDecisionSemantics:
    """决策级回归：冻结矩阵语义的机读快照（真实 OPA bundle）。"""

    async def test_decision_configure_by_org_owner_without_workspace_context(
        self, stack
    ) -> None:
        """workspace_policy.configure + org_owner + workspace_id=null → allow。"""
        from zhiwei.policy.roles import Action

        enforcer = PolicyEnforcer(OPAClient(OPA_URL))
        org = stack["organization_id"]
        decision = await enforcer.authorize(
            {
                "organization_id": str(org),
                "workspace_id": None,
                "actor": {
                    "principal_id": str(uuid4()),
                    "kind": "user",
                    "roles": [
                        {
                            "name": "org_owner",
                            "scope": "org",
                            "organization_id": str(org),
                            "workspace_id": None,
                        }
                    ],
                },
                "resource": {"type": "workspace_policy", "id": str(uuid4()), "version": "v1"},
                "action": Action.CONFIGURE.value,
                "purpose": "general",
                "classification": None,
                "risk": None,
                "delegation": [],
                "resource_context": {},
                "context": {
                    "now": "2026-09-03T00:00:00Z",
                    "classification_ceiling": None,
                    "requires_delegation": False,
                },
            }
        )
        assert decision.allow is True, decision.reason

    async def test_decision_configure_workspace_without_workspace_context_denied(
        self, stack
    ) -> None:
        """workspace_policy.configure_workspace + workspace_id=null → deny（死锁墓碑）。

        这正是修复前 API 的调用形态：唯一允许角色 workspace_admin 是 workspace
        作用域，无 workspace 上下文时永不可命中。冻结该语义防止回退。
        """
        from zhiwei.policy.roles import Action

        enforcer = PolicyEnforcer(OPAClient(OPA_URL))
        org = stack["organization_id"]
        decision = await enforcer.authorize(
            {
                "organization_id": str(org),
                "workspace_id": None,
                "actor": {
                    "principal_id": str(uuid4()),
                    "kind": "user",
                    "roles": [
                        {
                            "name": "workspace_admin",
                            "scope": "org",
                            "organization_id": str(org),
                            "workspace_id": None,
                        }
                    ],
                },
                "resource": {"type": "workspace_policy", "id": str(uuid4()), "version": "v1"},
                "action": Action.CONFIGURE_WORKSPACE.value,
                "purpose": "general",
                "classification": None,
                "risk": None,
                "delegation": [],
                "resource_context": {},
                "context": {
                    "now": "2026-09-03T00:00:00Z",
                    "classification_ceiling": None,
                    "requires_delegation": False,
                },
            }
        )
        assert decision.allow is False
