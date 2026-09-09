"""F-R6-03（knowledge 面）+ F-R6-04 disable 级联 RED——Knowledge 管理 API
接 PG + PEP + 审计。

替换 api/knowledge.py 的进程内单例 `_store`（P1 止血租户谓词随之退役）：

- 全部端点落 PgSourceLedger（0021 source_objects/source_versions）；
- mutation PEP（冻结矩阵行「Knowledge Source/ACL」）：create/connect/sync/
  acl/disable → knowledge_source.manage（workspace_admin「创建/同步/授权/
  禁用」；security_admin 紧急 suspend cell 不在本管理面），同事务 allowed
  审计 + denied/failed 独立事务；
- 读路径（list/status/versions）：矩阵 read cell = {memory_steward}，语义为
  「按 ACL 读来源」；管理面 list 消费方（S10 面板）无 read cell 承载——维持
  P1 止血租户谓词语义并登记 PERMISSIONS §3.3（与 connections 同型）；
- disable 级联（F-R6-04）：lifecycle_status=disabled + ledger 全版本失权
  （SyncIntent REVOKE 同事务 apply，audit knowledge.source.revoke）；
- sync 物化语义对齐 ledger 不变量：内容寻址 digest = sha256(canonical{
  source_id, uri})——同一内容重复 sync（含 force）恒 unchanged（force 不得
  越过不可变 ledger 的重复 digest 拒绝；stub 期 force 重建同 digest 版本
  违反 spec §3「Updates create new version」，行为变更登记）；
- status 端点 ACL 判定改走 check_acl_snapshot 公共入口（消除第二套 ACL
  实现——ADN-006 deny-override/unknown 语义 + group allow 生效）。

事实源：specs/s5-knowledge.md §3/§7、docs/PERMISSIONS.md §3.1 行 4、
findings F-R6-03、F-R6-04、tests/contract/api/test_knowledge_api.py（迁移源）。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
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

from zhiwei.api.knowledge import create_knowledge_router
from zhiwei.identity.domain import ActorContext, ActorRoleBinding
from zhiwei.knowledge.contracts import SourceVersionState
from zhiwei.knowledge.pg_ledger import PgSourceLedger
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
        await repository.create_workspace(workspace_id, name="knowledge-api")
    return context


@pytest_asyncio.fixture
async def other_tenant(sessions) -> TenantContext:
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="knowledge-api-b")
    return context


@pytest_asyncio.fixture
async def policy() -> AsyncIterator[FakeOPA]:
    yield FakeOPA()


def _binding(context: TenantContext) -> ActorRoleBinding:
    """workspace 作用域 workspace_admin 绑定（knowledge_source.manage 的角色；
    作用域语义由 Rego 裁决，本文件只钉 wiring）。"""
    return ActorRoleBinding(
        name="workspace_admin",
        scope="workspace",
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


def _actor(
    context: TenantContext,
    principal: UUID | None = None,
    bindings: tuple[ActorRoleBinding, ...] = (),
) -> ActorContext:
    return ActorContext(
        principal_id=principal or uuid4(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        role_bindings=bindings,
    )


def _app(
    context: TenantContext,
    sessions: Any,
    policy: FakeOPA,
    *,
    principal: UUID | None = None,
    bindings: tuple[ActorRoleBinding, ...] = (),
) -> FastAPI:
    from zhiwei.policy.client import OPAClient
    from zhiwei.policy.enforcement import PolicyEnforcer

    client = OPAClient(
        "http://opa.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(policy.handler)),
    )
    app = FastAPI()
    app.include_router(
        create_knowledge_router(
            actor_dependency=lambda: _actor(context, principal, bindings),
            sessions=sessions,
            policy_enforcer=PolicyEnforcer(client),
        )
    )
    return app


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


_ADMIN = "workspace_admin"


def _admin_bindings(context: TenantContext) -> tuple[ActorRoleBinding, ...]:
    return (_binding(context),)


async def _seed_source(
    sessions: Any, context: TenantContext, *, with_version: bool = True
) -> tuple[UUID, UUID | None]:
    from zhiwei.knowledge.contracts import ACLSnapshot, Classification, SourceObject

    assert context.workspace_id is not None
    async with tenant_session(sessions, context) as session:
        ledger = PgSourceLedger(session, context)
        obj = SourceObject(
            id=uuid4(),
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            source_type="document",
            acl=ACLSnapshot(),
            classification=Classification.PUBLIC,
            metadata={},
        )
        await ledger.register_object(obj)
        version_id = None
        if with_version:
            import hashlib

            from zhiwei.knowledge.contracts import Locator

            digest = "sha256:" + hashlib.sha256(
                f"{obj.id}:{obj.metadata}".encode()
            ).hexdigest()
            version = await ledger.create_version(
                obj.id,
                locator=Locator(connector="document", uri=f"source://{obj.id}"),
                content_digest=digest,
                observed_at=datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
                valid_at=datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
            )
            version_id = version.id
    return obj.id, version_id


async def _versions(sessions: Any, context: TenantContext, source_id: UUID) -> list[Any]:
    async with tenant_session(sessions, context) as session:
        ledger = PgSourceLedger(session, context)
        return await ledger.list_versions(source_id)


async def _lifecycle_status(sessions: Any, context: TenantContext, source_id: UUID) -> str | None:
    async with tenant_session(sessions, context) as session:
        return await PgSourceLedger(session, context).get_lifecycle_status(source_id)


async def _audits(sessions: Any, context: TenantContext) -> list[Any]:
    from sqlalchemy import select

    async with tenant_session(sessions, context) as audit_session:
        rows = (
            await audit_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.organization_id == context.organization_id
                )
            )
        ).all()
        return list(rows)


async def _install_sync_fail_trigger() -> None:
    """source_versions BEFORE INSERT 触发器：模拟物化 flush 失败（用后必须删除）。"""
    import asyncpg

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "CREATE OR REPLACE FUNCTION zhiwei_test_sync_fail() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'intentional source version write failure "
            "(test trigger)'; END $$ "
            "LANGUAGE plpgsql"
        )
        await connection.execute(
            "CREATE TRIGGER zhiwei_test_sync_fail_trg BEFORE INSERT ON source_versions "
            "FOR EACH ROW EXECUTE FUNCTION zhiwei_test_sync_fail()"
        )
    finally:
        await connection.close()


async def _drop_sync_fail_trigger() -> None:
    import asyncpg

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "DROP TRIGGER IF EXISTS zhiwei_test_sync_fail_trg ON source_versions"
        )
        await connection.execute("DROP FUNCTION IF EXISTS zhiwei_test_sync_fail()")
    finally:
        await connection.close()


class TestSourceMutationsPep:
    async def test_add_source_persists_and_audits(
        self, tenant, sessions, policy
    ) -> None:
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "active"
        assert await _lifecycle_status(sessions, tenant, UUID(body["id"])) == "active"

        policy_input = policy.inputs[0]
        assert policy_input["action"] == "manage"
        assert policy_input["resource"]["type"] == "knowledge_source"

        audits = [
            a for a in await _audits(sessions, tenant) if a.action == "knowledge.source.create"
        ]
        assert len(audits) == 1
        assert audits[0].result == "allowed"

    async def test_mutations_denied_by_default_zero_write(
        self, tenant, sessions, policy
    ) -> None:
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            for method, path, json_body in (
                ("post", "/api/v1/knowledge/sources", {"source_type": "x", "connector": "c", "uri": "u"}),
                ("post", f"/api/v1/knowledge/sources/{uuid4()}/connect", None),
                ("post", f"/api/v1/knowledge/sources/{uuid4()}/sync", {"force": False}),
                ("put", f"/api/v1/knowledge/sources/{uuid4()}/acl", {"allowed_principals": []}),
                ("post", f"/api/v1/knowledge/sources/{uuid4()}/disable", None),
            ):
                resp = await client.request(method, path, json=json_body)
                assert resp.status_code == 403, (path, resp.text)
                assert resp.json() == {"detail": "policy denied"}
        audits = [
            a for a in await _audits(sessions, tenant) if a.resource_type == "knowledge_source"
        ]
        assert len(audits) == 5
        assert all(a.result == "denied" for a in audits)

    async def test_unknown_source_mutations_404_after_pep(
        self, tenant, sessions, policy
    ) -> None:
        """PEP 先于数据访问；未知/跨租户 source 与不存在同形 404。"""
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            for path, body in (
                (f"/api/v1/knowledge/sources/{uuid4()}/connect", None),
                (f"/api/v1/knowledge/sources/{uuid4()}/sync", {"force": False}),
                (f"/api/v1/knowledge/sources/{uuid4()}/disable", None),
            ):
                resp = await client.post(path, json=body)
                assert resp.status_code == 404, path
                assert resp.json() == {"detail": "source not found"}


class TestSourceLifecycle:
    async def test_add_connect_list_flow(self, tenant, sessions, policy) -> None:
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            assert created.status_code == 201
            source_id = created.json()["id"]

            connected = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/connect"
            )
            assert connected.status_code == 200
            assert connected.json()["status"] == "active"

            listing = await client.get("/api/v1/knowledge/sources")
            assert listing.status_code == 200
            ids = {s["id"] for s in listing.json()}
            assert ids == {source_id}

    async def test_add_source_invalid_classification_is_422(
        self, tenant, sessions, policy
    ) -> None:
        """输入校验在 PEP 之前（422 不触发 OPA、不写审计）。"""
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                    "classification": "INVALID",
                },
            )
        assert resp.status_code == 422
        assert policy.inputs == []


class TestSourceSync:
    async def test_sync_materializes_first_version(
        self, tenant, sessions, policy
    ) -> None:
        source_id, _ = await _seed_source(sessions, tenant, with_version=False)
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            synced = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync", json={"force": False}
            )
        assert synced.status_code == 200
        data = synced.json()
        assert data["sync_status"] == "completed"
        assert data["versions_created"] == 1
        versions = await _versions(sessions, tenant, source_id)
        assert len(versions) == 1
        assert versions[0].state is SourceVersionState.ACTIVE

        audits = [
            a for a in await _audits(sessions, tenant) if a.action == "knowledge.source.sync"
        ]
        assert len(audits) == 1
        assert audits[0].result == "allowed"

    async def test_repeat_sync_is_content_addressed_unchanged(
        self, tenant, sessions, policy
    ) -> None:
        """ledger 不变量对齐（行为变更登记）：同一内容重复 sync（含 force）
        恒 unchanged——force 不得越过重复 digest 拒绝（stub 期 force 重建同
        digest 版本违反 spec §3「Updates create new version」）。"""
        source_id, _ = await _seed_source(sessions, tenant, with_version=False)
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            first = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync", json={"force": False}
            )
            assert first.json()["sync_status"] == "completed"
            repeat = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync", json={"force": False}
            )
            forced = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync", json={"force": True}
            )
        assert repeat.json()["sync_status"] == "unchanged"
        assert forced.json()["sync_status"] == "unchanged"
        assert len(await _versions(sessions, tenant, source_id)) == 1

    async def test_sync_disabled_source_403_failed_audit(
        self, tenant, sessions, policy
    ) -> None:
        source_id, _ = await _seed_source(sessions, tenant)
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            disabled = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/disable"
            )
            assert disabled.status_code == 200
            synced = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync", json={"force": False}
            )
        assert synced.status_code == 403
        audits = [
            a for a in await _audits(sessions, tenant) if a.action == "knowledge.source.sync"
        ]
        assert len(audits) == 1
        assert audits[0].result == "failed"

    async def test_sync_materialization_failure_failed_audit_lifecycle_error(
        self, tenant, sessions, policy
    ) -> None:
        """物化失败路径契约（F-R6-13）：savepoint 化 + failed 审计三态闭合 +
        caller 不见内部错误。

        触发器使 create_version 的 flush 失败：失败前 mark_stale 在 savepoint
        内——回滚后既有版本保持 ACTIVE（未被错误置 stale）；lifecycle 落
        error（在可用的事务上写入，而非 pending-rollback 会话）；failed 审计
        独立事务落账；响应 error 为固定通用文案（str(exc) 内部细节不出进程
        边界，也不入 last_sync_error）。
        """
        source_id, version_id = await _seed_source(sessions, tenant, with_version=True)
        policy.allow = True
        await _install_sync_fail_trigger()
        try:
            async with _client(
                _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
            ) as client:
                resp = await client.post(
                    f"/api/v1/knowledge/sources/{source_id}/sync", json={"force": False}
                )
        finally:
            await _drop_sync_fail_trigger()
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["sync_status"] == "failed"
        assert data["versions_marked_stale"] == 0, "savepoint 回滚不得遗留 stale 转移"
        assert data["error"] == "sync materialization failed"

        versions = await _versions(sessions, tenant, source_id)
        assert len(versions) == 1
        assert versions[0].id == version_id
        assert versions[0].state is SourceVersionState.ACTIVE

        assert await _lifecycle_status(sessions, tenant, source_id) == "error"
        async with tenant_session(sessions, tenant) as session:
            last_error = await PgSourceLedger(session, tenant).get_last_sync_error(source_id)
        assert last_error == "sync materialization failed"

        audits = [
            a for a in await _audits(sessions, tenant) if a.action == "knowledge.source.sync"
        ]
        assert len(audits) == 1
        assert audits[0].result == "failed"
        assert audits[0].resource_id == source_id
        assert audits[0].resource_version == 0, "failed 审计 = mutation 未落（version 0 哨兵）"


class TestSourceDisableCascade:
    async def test_disable_revokes_all_versions(self, tenant, sessions, policy) -> None:
        """F-R6-04 级联：disable = lifecycle_status=disabled + ledger 全版本
        失权（REVOKE 同事务 apply，audit knowledge.source.revoke）。"""
        source_id, version_id = await _seed_source(sessions, tenant)
        assert version_id is not None
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            resp = await client.post(f"/api/v1/knowledge/sources/{source_id}/disable")
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"

        assert await _lifecycle_status(sessions, tenant, source_id) == "disabled"
        versions = await _versions(sessions, tenant, source_id)
        assert versions[0].state is SourceVersionState.REVOKED
        assert versions[0].tombstone is True

        actions = {a.action: a.result for a in await _audits(sessions, tenant)}
        assert actions.get("knowledge.source.disable") == "allowed"
        # 级联失权审计经 append_audit_chain（事件链行，result 语义为空），
        # 存在性即契约
        assert "knowledge.source.revoke" in actions

    async def test_reconnect_does_not_resurrect_versions(
        self, tenant, sessions, policy
    ) -> None:
        """disable 级联后的 reconnect 只恢复运营状态；已失权版本不复活
        （内容事实由 ledger 状态机承载，不可逆）。"""
        source_id, version_id = await _seed_source(sessions, tenant)
        assert version_id is not None
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_admin_bindings(tenant))
        ) as client:
            await client.post(f"/api/v1/knowledge/sources/{source_id}/disable")
            connected = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/connect"
            )
            assert connected.status_code == 200
            versions = await client.get(
                f"/api/v1/knowledge/sources/{source_id}/versions"
            )
        assert await _lifecycle_status(sessions, tenant, source_id) == "active"
        listed = versions.json()
        assert listed[0]["state"] == "revoked"
        assert listed[0]["version_seq"] == 1


class TestSourceAcl:
    async def test_update_acl_persists_and_changes_access(
        self, tenant, sessions, policy
    ) -> None:
        source_id, _ = await _seed_source(sessions, tenant)
        principal_id = uuid4()
        policy.allow = True
        app = _app(
            tenant,
            sessions,
            policy,
            principal=principal_id,
            bindings=_admin_bindings(tenant),
        )
        async with _client(app) as client:
            status_before = await client.get(
                f"/api/v1/knowledge/sources/{source_id}/status"
            )
            assert status_before.json()["acl_reason"] == "unknown"

            updated = await client.put(
                f"/api/v1/knowledge/sources/{source_id}/acl",
                json={
                    "allowed_principals": [str(principal_id)],
                    "denied_principals": [],
                    "allowed_groups": [],
                },
            )
            assert updated.status_code == 200
            assert str(principal_id) in updated.json()["acl_allowed_principals"]

            status_after = await client.get(
                f"/api/v1/knowledge/sources/{source_id}/status"
            )
        assert status_after.json()["acl_allowed"] is True
        assert status_after.json()["acl_reason"] == "allowed"

        audits = [
            a for a in await _audits(sessions, tenant) if a.action == "knowledge.source.acl_update"
        ]
        assert len(audits) == 1
        assert audits[0].result == "allowed"

    async def test_deny_principal_overrides_allow(
        self, tenant, sessions, policy
    ) -> None:
        """check_acl_snapshot 公共入口：deny-override 语义（域唯一事实）。"""
        source_id, _ = await _seed_source(sessions, tenant)
        principal_id = uuid4()
        policy.allow = True
        app = _app(
            tenant,
            sessions,
            policy,
            principal=principal_id,
            bindings=_admin_bindings(tenant),
        )
        async with _client(app) as client:
            await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/other.md",
                    "acl_allowed_principals": [str(principal_id)],
                },
            )
            await client.put(
                f"/api/v1/knowledge/sources/{source_id}/acl",
                json={
                    "allowed_principals": [str(principal_id)],
                    "denied_principals": [str(principal_id)],
                    "allowed_groups": [],
                },
            )
            status_denied = await client.get(
                f"/api/v1/knowledge/sources/{source_id}/status"
            )
        assert status_denied.json()["acl_allowed"] is False
        assert status_denied.json()["acl_reason"] == "denied_principal"

    async def test_group_allow_honored_by_shared_judgment(
        self, tenant, sessions, policy
    ) -> None:
        """行为变更锚点（check_acl_snapshot 接入）：allowed_groups 命中 →
        allowed（stub 期第二套 ACL 实现只看 principal、忽略 group）。"""
        source_id, _ = await _seed_source(sessions, tenant)
        principal_id = uuid4()
        policy.allow = True
        app = _app(
            tenant,
            sessions,
            policy,
            principal=principal_id,
            bindings=_admin_bindings(tenant),
        )
        async with _client(app) as client:
            await client.put(
                f"/api/v1/knowledge/sources/{source_id}/acl",
                json={
                    "allowed_principals": [],
                    "denied_principals": [],
                    "allowed_groups": ["workspace-members"],
                },
            )
            # group 判定需要 context.allowed_groups——status 端点构造
            # ACLContext 时以 principal 所属 groups 传入；此处经 domain 层
            # 直接验证共享判定（API 无 groups 来源，stub 消费面按 principal）
            from zhiwei.knowledge.acl import ACLContext, check_acl_snapshot

            async with tenant_session(sessions, tenant) as session:
                ledger = PgSourceLedger(session, tenant)
                obj = await ledger.get_object(source_id)
            result = check_acl_snapshot(
                obj.acl,
                ACLContext(
                    principal_id=principal_id,
                    organization_id=tenant.organization_id,
                    workspace_id=tenant.workspace_id,
                    allowed_groups=frozenset({"workspace-members"}),
                ),
            )
        assert result.allowed is True


class TestReadsAndTenancy:
    async def test_reads_are_tenant_scoped(self, tenant, other_tenant, sessions, policy) -> None:
        """golden GUARD 迁移锚点：list org+workspace 过滤；跨 org status/versions
        404 同形（读路径无管理面 read cell——PERMISSIONS §3.3 登记）。"""
        own_source, _ = await _seed_source(sessions, tenant)
        foreign_source, _ = await _seed_source(sessions, other_tenant)
        async with _client(_app(tenant, sessions, policy)) as client:
            listing = await client.get("/api/v1/knowledge/sources")
            assert listing.status_code == 200
            ids = {s["id"] for s in listing.json()}
            assert ids == {str(own_source)}

            for suffix in ("status", "versions"):
                resp = await client.get(
                    f"/api/v1/knowledge/sources/{foreign_source}/{suffix}"
                )
                assert resp.status_code == 404
                assert resp.json() == {"detail": "source not found"}

    async def test_cross_workspace_same_org_denied(
        self, tenant, sessions, policy
    ) -> None:
        """IDOR 迁移锚点（旧 TestKnowledgeCrossWorkspace）：同 org 跨 workspace
        读写一律 404 + owner 侧零写入（ws 是协作边界，不只是显示过滤）。"""
        from uuid import uuid4 as _uuid4

        from zhiwei.persistence.repositories import TenantRepository as _TenantRepo

        source_id, _ = await _seed_source(sessions, tenant)
        ws2 = TenantContext(
            organization_id=tenant.organization_id, workspace_id=_uuid4()
        )
        assert ws2.workspace_id is not None
        async with tenant_session(sessions, ws2) as session:
            repo = _TenantRepo(session, ws2)
            await repo.create_workspace(ws2.workspace_id, name="knowledge-api-ws2")
        policy.allow = True
        attacker_app = _app(ws2, sessions, policy, bindings=_admin_bindings(ws2))
        async with _client(attacker_app) as client:
            for method, path, body in (
                ("get", f"/api/v1/knowledge/sources/{source_id}/status", None),
                ("get", f"/api/v1/knowledge/sources/{source_id}/versions", None),
                ("post", f"/api/v1/knowledge/sources/{source_id}/disable", None),
                (
                    "put",
                    f"/api/v1/knowledge/sources/{source_id}/acl",
                    {
                        "allowed_principals": [str(_uuid4())],
                        "denied_principals": [],
                        "allowed_groups": [],
                    },
                ),
            ):
                resp = await client.request(method, path, json=body)
                assert resp.status_code == 404, (method, path, resp.text)
                assert resp.json() == {"detail": "source not found"}
        # owner 侧零写入（disable/acl 未触达）
        assert await _lifecycle_status(sessions, tenant, source_id) == "active"

    async def test_no_org_context_is_403(self, sessions, policy) -> None:
        """旧 contract 迁移锚点：无 org 上下文 403。"""
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        client_opa = OPAClient(
            "http://opa.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(policy.handler)),
        )
        app = FastAPI()
        app.include_router(
            create_knowledge_router(
                actor_dependency=lambda: ActorContext(principal_id=uuid4()),
                sessions=sessions,
                policy_enforcer=PolicyEnforcer(client_opa),
            )
        )
        async with _client(app) as client:
            resp = await client.get("/api/v1/knowledge/sources")
        assert resp.status_code == 403

    async def test_status_score_breakdown_after_sync(
        self, tenant, sessions, policy
    ) -> None:
        """旧 contract 迁移锚点：freshness/score_breakdown 展示契约。"""
        source_id, _ = await _seed_source(sessions, tenant)
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get(f"/api/v1/knowledge/sources/{source_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version_seq"] == 1
        assert data["content_digest"] is not None
        assert data["freshness_state"] in ("fresh", "aging", "stale", "expired")
        assert isinstance(data["acl_allowed"], bool)
        assert "acl_reason" in data
        breakdown = data["score_breakdown"]
        assert 0.0 <= breakdown["acl_score"] <= 1.0
        assert 0.0 <= breakdown["freshness_score"] <= 1.0

    async def test_no_workspace_context_is_403(
        self, sessions, policy
    ) -> None:
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        client = OPAClient(
            "http://opa.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(policy.handler)),
        )
        app = FastAPI()
        app.include_router(
            create_knowledge_router(
                actor_dependency=lambda: ActorContext(principal_id=uuid4(), organization_id=uuid4()),
                sessions=sessions,
                policy_enforcer=PolicyEnforcer(client),
            )
        )
        async with _client(app) as client:
            resp = await client.get("/api/v1/knowledge/sources")
        assert resp.status_code == 403
