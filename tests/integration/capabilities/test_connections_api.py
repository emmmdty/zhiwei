"""F-R6-03（connections 面）+ F-R3-06 own cells + P1 残留 RED——Connection API
接 PG + PEP + 审计。

替换 api/connections.py 的进程内单例 `_store`（P1 止血谓词随之退役）：

- 新增 0020_connections 持久层（PgConnectionRepository，tenant-scoped + 状态
  CAS），ToolGateway 域内注册表不动（域内消费，非 API 面）；
- mutation PEP（冻结矩阵行「Connection/secret」）：
  - create：workspace_service → connection_secret.create_workspace_connection；
    user_delegated → connection_secret.create_own（owner 证据 = 认证主体，
    request 自报 principal 不再信任）；service_account 无矩阵 cell → 结构性
    本地拒绝 + denied 审计（fail closed）；
  - actions：revoke/suspend → connection_secret.revoke（admin，P1 冻结映射）；
    own delegated 连接的 revoke → connection_secret.revoke_own（owner 证据取自
    权威记录）；
- 读路径（list/get/status）无冻结 read cell（矩阵只给 auditor
  read_status_fingerprint），维持 P1 止血的租户谓词语义并登记 PERMISSIONS；
- provider_version_id 存在性校验（P1 残留）：PEP 之后查 capability 目录，
  未知 → 422（不向未授权 caller 泄漏目录存在性）；
- 同事务 allowed 审计 + denied/failed 独立事务审计（F-R6-03）。

事实源：specs/s4-capability-hub.md §5、docs/PERMISSIONS.md §3.1 行 6、
findings F-R6-03、F-R3-06、R3 A1 验收残留（provider_version_id 校验）。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from zhiwei.api.connections import create_connections_router
from zhiwei.capabilities.connections import Connection, ConnectionStatus, SubjectMode
from zhiwei.identity.domain import ActorContext, ActorRoleBinding
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import AuditEvent
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

pytestmark = pytest.mark.asyncio

ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_URL = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)

_USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_USER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


class FakeOPA:
    """本地假 OPA（fail closed：缺省 deny，allow 须显式声明——F-R3-05 语义）。"""

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.allow = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.inputs.append(json.loads(request.read())["input"])
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
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[3]
    config = Config(str(repo_root / "alembic.ini"))
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
        await repository.create_workspace(workspace_id, name="connections-api")
    return context


@pytest_asyncio.fixture
async def other_tenant(sessions) -> TenantContext:
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="connections-api-b")
    return context


@pytest_asyncio.fixture
async def policy() -> AsyncIterator[FakeOPA]:
    yield FakeOPA()


def _binding(name: str, context: TenantContext) -> ActorRoleBinding:
    """冻结矩阵作用域：connection_secret 管理角色按各自矩阵标注（本文件只钉
    wiring 形状，角色-作用域语义由 Rego 裁决）。"""
    if name == "workspace_admin":
        return ActorRoleBinding(
            name=name,
            scope="workspace",
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
        )
    return ActorRoleBinding(name=name, scope="org", organization_id=context.organization_id)


def _actor(
    context: TenantContext,
    principal: UUID = _USER_A,
    bindings: tuple[ActorRoleBinding, ...] = (),
) -> ActorContext:
    return ActorContext(
        principal_id=principal,
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        role_bindings=bindings,
    )


def _app(
    context: TenantContext,
    sessions: Any,
    policy: FakeOPA,
    *,
    principal: UUID = _USER_A,
    bindings: tuple[ActorRoleBinding, ...] = (),
    provider_version_exists: Callable[[UUID, UUID, UUID], bool] | None = None,
) -> FastAPI:
    from zhiwei.policy.client import OPAClient
    from zhiwei.policy.enforcement import PolicyEnforcer

    client = OPAClient(
        "http://opa.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(policy.handler)),
    )
    app = FastAPI()
    app.include_router(
        create_connections_router(
            actor_dependency=lambda: _actor(context, principal, bindings),
            sessions=sessions,
            policy_enforcer=PolicyEnforcer(client),
            provider_version_exists=provider_version_exists
            or (lambda _vid, _org, _ws: True),
        )
    )
    return app


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed_connection(
    sessions: Any,
    context: TenantContext,
    *,
    subject_mode: SubjectMode = SubjectMode.WORKSPACE_SERVICE,
    principal_id: UUID | None = None,
    status: ConnectionStatus = ConnectionStatus.ACTIVE,
) -> Connection:
    from zhiwei.capabilities.pg_connections import PgConnectionRepository

    assert context.workspace_id is not None
    connection = Connection(
        id=uuid4(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        provider_version_id=uuid4(),
        subject_mode=subject_mode,
        status=status,
        principal_id=principal_id,
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with tenant_session(sessions, context) as session:
        await PgConnectionRepository(session, context).create(connection)
    return connection


async def _row(sessions: Any, context: TenantContext, connection_id: UUID) -> Any:
    from sqlalchemy import select

    from zhiwei.persistence.models import ConnectionRow

    async with tenant_session(sessions, context) as session:
        return await session.scalar(
            select(ConnectionRow).where(ConnectionRow.id == connection_id)
        )


async def _audits(sessions: Any, context: TenantContext) -> list[Any]:
    from sqlalchemy import select

    async with tenant_session(sessions, context) as session:
        rows = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.organization_id == context.organization_id
                )
            )
        ).all()
        return list(rows)


_WS_ADMIN = ("workspace_admin",)
_MEMBER = ("member",)


class TestConnectionCreate:
    async def test_create_workspace_service_persists_and_audits(
        self, tenant, sessions, policy
    ) -> None:
        """workspace_admin 创建 workspace service 连接：PG 行 + 同事务 allowed
        审计 + PolicyInput cell 形状锚点。"""
        provider_version_id = uuid4()
        policy.allow = True
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
            )
        ) as client:
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider_version_id": str(provider_version_id),
                    "subject_mode": "workspace_service",
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["subject_mode"] == "workspace_service"
        assert body["status"] == "active"
        assert body["principal_id"] is None

        row = await _row(sessions, tenant, UUID(body["id"]))
        assert row is not None
        assert row.status == "active"

        assert len(policy.inputs) == 1
        policy_input = policy.inputs[0]
        assert policy_input["action"] == "create_workspace_connection"
        assert policy_input["resource"]["type"] == "connection_secret"

        audits = [a for a in await _audits(sessions, tenant) if a.action == "connection.create"]
        assert len(audits) == 1
        assert audits[0].result == "allowed"
        assert audits[0].actor_ref == f"user:{_USER_A}"

    async def test_create_delegated_forces_own_principal(
        self, tenant, sessions, policy
    ) -> None:
        """user_delegated 走 create_own cell：owner 证据 = 认证主体；request
        自报的 principal_id 不是授权事实——他主体声明直接 403。"""
        policy.allow = True
        bindings = tuple(_binding(n, tenant) for n in _MEMBER)
        async with _client(
            _app(tenant, sessions, policy, bindings=bindings)
        ) as client:
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider_version_id": str(uuid4()),
                    "subject_mode": "user_delegated",
                    "principal_id": str(_USER_B),
                },
            )
            assert resp.status_code == 403
            ok = await client.post(
                "/api/v1/connections",
                json={
                    "provider_version_id": str(uuid4()),
                    "subject_mode": "user_delegated",
                    "principal_id": str(_USER_A),
                },
            )
        assert ok.status_code == 201
        row = await _row(sessions, tenant, UUID(ok.json()["id"]))
        assert row is not None
        assert str(row.principal_id) == str(_USER_A)

        own_inputs = [
            i for i in policy.inputs if i["action"] == "create_own"
        ]
        assert len(own_inputs) == 1
        assert own_inputs[0]["resource_context"]["owner_principal_id"] == str(_USER_A)

    async def test_create_service_account_mode_structurally_denied(
        self, tenant, sessions, policy
    ) -> None:
        """矩阵无 service_account Connection cell → 本地结构性拒绝 + denied
        审计（无 cell 可求值，不允许借道任意 cell）。"""
        policy.allow = True
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
            )
        ) as client:
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider_version_id": str(uuid4()),
                    "subject_mode": "service_account",
                },
            )
        assert resp.status_code == 403
        audits = [a for a in await _audits(sessions, tenant) if a.action == "connection.create"]
        assert len(audits) == 1
        assert audits[0].result == "denied"

    async def test_create_denied_by_default_zero_write(
        self, tenant, sessions, policy
    ) -> None:
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider_version_id": str(uuid4()),
                    "subject_mode": "workspace_service",
                },
            )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "policy denied"}
        async with tenant_session(sessions, tenant) as session:
            from sqlalchemy import func, select

            from zhiwei.persistence.models import ConnectionRow

            count = await session.scalar(select(func.count()).select_from(ConnectionRow))
        assert count == 0

    async def test_create_unknown_provider_version_422(
        self, tenant, sessions, policy
    ) -> None:
        """P1 残留（provider_version_id 存在性校验）：PEP 之后校验（不向未授权
        caller 泄漏目录存在性——policy.inputs 非空即 PEP 先行的回归锚点），
        未知 → 422 + failed 审计，零写入。"""
        policy.allow = True
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
                provider_version_exists=lambda _vid, _org, _ws: False,
            )
        ) as client:
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider_version_id": str(uuid4()),
                    "subject_mode": "workspace_service",
                },
            )
        assert resp.status_code == 422
        # 顺序锚点：PEP 求值发生在存在性校验之前
        assert len(policy.inputs) == 1
        # 零写入 + failed 审计（PEP 放行后的业务拒绝三态）
        async with tenant_session(sessions, tenant) as session:
            from sqlalchemy import func, select

            from zhiwei.persistence.models import ConnectionRow

            count = await session.scalar(select(func.count()).select_from(ConnectionRow))
        assert count == 0
        audits = [a for a in await _audits(sessions, tenant) if a.action == "connection.create"]
        assert len(audits) == 1
        assert audits[0].result == "failed"

    async def test_create_delegated_self_reported_foreign_principal_denied_audit(
        self, tenant, sessions, policy
    ) -> None:
        """对抗审查 MAJOR-1 回归锚点：自报他主体的 own 证据伪造尝试写 denied
        审计（独立事务），与 service_account 结构性拒绝同口径。"""
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=tuple(_binding(n, tenant) for n in _MEMBER))
        ) as client:
            resp = await client.post(
                "/api/v1/connections",
                json={
                    "provider_version_id": str(uuid4()),
                    "subject_mode": "user_delegated",
                    "principal_id": str(_USER_B),
                },
            )
        assert resp.status_code == 403
        assert policy.inputs == []  # 本地拒绝不经 OPA
        audits = [a for a in await _audits(sessions, tenant) if a.action == "connection.create"]
        assert len(audits) == 1
        assert audits[0].result == "denied"


class TestConnectionActions:
    async def test_revoke_by_admin_persists_and_audits(
        self, tenant, sessions, policy
    ) -> None:
        connection = await _seed_connection(sessions, tenant)
        policy.allow = True
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
            )
        ) as client:
            resp = await client.post(
                f"/api/v1/connections/{connection.id}/actions",
                json={"action": "revoke"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "revoked"
        assert body["version"] == connection.version + 1

        row = await _row(sessions, tenant, connection.id)
        assert row is not None and row.status == "revoked"

        policy_input = policy.inputs[0]
        assert policy_input["action"] == "revoke"
        assert policy_input["resource"]["type"] == "connection_secret"
        assert policy_input["resource"]["id"] == str(connection.id)

        audits = [a for a in await _audits(sessions, tenant) if a.action == "connection.revoke"]
        assert len(audits) == 1
        assert audits[0].result == "allowed"

    async def test_suspend_uses_revoke_cell(self, tenant, sessions, policy) -> None:
        """suspend 映射 connection_secret.revoke cell（P1 冻结语义延续；矩阵
        无独立 suspend cell——PERMISSIONS §3.3 登记）。"""
        connection = await _seed_connection(sessions, tenant)
        policy.allow = True
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
            )
        ) as client:
            resp = await client.post(
                f"/api/v1/connections/{connection.id}/actions",
                json={"action": "suspend"},
            )
        assert resp.status_code == 200
        assert policy.inputs[0]["action"] == "revoke"

    async def test_own_delegated_revoke_uses_revoke_own_cell(
        self, tenant, sessions, policy
    ) -> None:
        """own delegated 连接：owner revoke 走 revoke_own cell，owner 证据取自
        权威记录（非 caller 自报）。"""
        connection = await _seed_connection(
            sessions, tenant, subject_mode=SubjectMode.USER_DELEGATED, principal_id=_USER_A
        )
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=tuple(_binding(n, tenant) for n in _MEMBER))
        ) as client:
            resp = await client.post(
                f"/api/v1/connections/{connection.id}/actions",
                json={"action": "revoke"},
            )
        assert resp.status_code == 200
        policy_input = policy.inputs[0]
        assert policy_input["action"] == "revoke_own"
        assert policy_input["resource_context"]["owner_principal_id"] == str(_USER_A)

    async def test_revoke_denied_zero_write(self, tenant, sessions, policy) -> None:
        connection = await _seed_connection(sessions, tenant)
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
            )
        ) as client:
            resp = await client.post(
                f"/api/v1/connections/{connection.id}/actions",
                json={"action": "revoke"},
            )
        assert resp.status_code == 403
        row = await _row(sessions, tenant, connection.id)
        assert row is not None and row.status == "active"
        audits = [a for a in await _audits(sessions, tenant) if a.action == "connection.revoke"]
        assert len(audits) == 1
        assert audits[0].result == "denied"

    async def test_revoke_revoked_connection_409_failed_audit(
        self, tenant, sessions, policy
    ) -> None:
        connection = await _seed_connection(
            sessions, tenant, status=ConnectionStatus.REVOKED
        )
        policy.allow = True
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
            )
        ) as client:
            resp = await client.post(
                f"/api/v1/connections/{connection.id}/actions",
                json={"action": "revoke"},
            )
        assert resp.status_code == 409
        audits = [a for a in await _audits(sessions, tenant) if a.action == "connection.revoke"]
        assert len(audits) == 1
        assert audits[0].result == "failed"

    async def test_cas_conflict_409_failed_audit(
        self, tenant, sessions, policy, monkeypatch
    ) -> None:
        """CAS 失配锚点（F-R6-14）：re-read 快照过期后 CAS 落空 → 409 +
        failed 审计，行保持并发赢家状态。

        竞态窗口 = 归属读/re-read 快照与同事务 CAS UPDATE 之间（READ
        COMMITTED 下并发事务可在窗口内提交状态转移）。并发赢家经真实
        transition_status 落库；monkeypatch 把 re-read 快照钉在过期 ACTIVE——
        真实 CAS UPDATE（WHERE status='active'）匹配 0 行，走真实 raise 点与
        路由的 ConnectionTransitionConflict 转译分支（REVOKED 预检绕开 CAS，
        不覆盖本路径）。
        """
        from zhiwei.capabilities.pg_connections import PgConnectionRepository

        connection = await _seed_connection(sessions, tenant)
        async with tenant_session(sessions, tenant) as session:
            await PgConnectionRepository(session, tenant).transition_status(
                connection.id,
                expected_status=ConnectionStatus.ACTIVE,
                target_status=ConnectionStatus.SUSPENDED,
                now=datetime.now(UTC),
            )

        real_get = PgConnectionRepository.get

        async def stale_get(repo_self: Any, connection_id: UUID) -> Any:
            current = await real_get(repo_self, connection_id)
            if current is None:
                return None
            return current.model_copy(update={"status": ConnectionStatus.ACTIVE})

        monkeypatch.setattr(PgConnectionRepository, "get", stale_get)
        policy.allow = True
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
            )
        ) as client:
            resp = await client.post(
                f"/api/v1/connections/{connection.id}/actions",
                json={"action": "revoke"},
            )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "connection changed concurrently"

        row = await _row(sessions, tenant, connection.id)
        assert row is not None and row.status == "suspended"
        assert row.version == connection.version + 1, "CAS 失配不得再次递增 version"

        audits = [a for a in await _audits(sessions, tenant) if a.action == "connection.revoke"]
        assert len(audits) == 1
        assert audits[0].result == "failed"

    async def test_unknown_action_422(self, tenant, sessions, policy) -> None:
        connection = await _seed_connection(sessions, tenant)
        policy.allow = True
        async with _client(
            _app(
                tenant,
                sessions,
                policy,
                bindings=tuple(_binding(n, tenant) for n in _WS_ADMIN),
            )
        ) as client:
            resp = await client.post(
                f"/api/v1/connections/{connection.id}/actions",
                json={"action": "explode"},
            )
        assert resp.status_code == 422
        assert policy.inputs == []


class TestConnectionReadsAndTenancy:
    async def test_list_is_workspace_scoped(
        self, tenant, other_tenant, sessions, policy
    ) -> None:
        """golden GUARD 迁移锚点：list 按 org+workspace 过滤（P1 止血语义，
        读路径无冻结 read cell——PERMISSIONS §3.3 登记）。"""
        own = await _seed_connection(sessions, tenant)
        await _seed_connection(sessions, other_tenant)
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get("/api/v1/connections")
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()}
        assert ids == {str(own.id)}

    async def test_get_and_status_cross_workspace_404(
        self, tenant, other_tenant, sessions, policy
    ) -> None:
        """IDOR GUARD 迁移锚点：同 org 跨 workspace 与跨 org 读取一律 404
        （与不存在同形）。"""
        from uuid import uuid4 as _uuid4

        from zhiwei.persistence.repositories import TenantRepository as _TenantRepo

        same_org_ws2 = TenantContext(
            organization_id=tenant.organization_id, workspace_id=_uuid4()
        )
        assert same_org_ws2.workspace_id is not None
        async with tenant_session(sessions, same_org_ws2) as session:
            repo = _TenantRepo(session, same_org_ws2)
            await repo.create_workspace(same_org_ws2.workspace_id, name="connections-api-ws2")
        cross_ws = await _seed_connection(sessions, same_org_ws2)
        cross_org = await _seed_connection(sessions, other_tenant)
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            for connection in (cross_ws, cross_org):
                for suffix in ("", "/status"):
                    resp = await client.get(
                        f"/api/v1/connections/{connection.id}{suffix}"
                    )
                    assert resp.status_code == 404
                    assert resp.json() == {"detail": "connection not found"}

    async def test_cross_org_actions_404_victim_untouched(
        self, tenant, other_tenant, sessions, policy
    ) -> None:
        """IDOR 迁移锚点：跨 org mutation 与不存在同形 404，受害者零变化。"""
        connection = await _seed_connection(sessions, tenant)
        policy.allow = True
        app = _app(
            other_tenant,
            sessions,
            policy,
            principal=_USER_B,
            bindings=tuple(_binding(n, other_tenant) for n in _WS_ADMIN),
        )
        async with _client(app) as client:
            for action in ("suspend", "revoke"):
                resp = await client.post(
                    f"/api/v1/connections/{connection.id}/actions",
                    json={"action": action},
                )
                assert resp.status_code == 404
                assert resp.json() == {"detail": "connection not found"}
        row = await _row(sessions, tenant, connection.id)
        assert row is not None and row.status == "active"
