"""F-R6-02/F-R6-03/F-R6-12/F-R3-09 RED — Memory Center API 接 PG + PEP + 审计。

替换 api/memory.py 的进程内单例 `_store`：全部端点改走 PgMemoryRepository
（0012 memory_records + lifecycle ledger），mutation 经 authorize_mutation
（denied → 独立事务审计 + 403）+ 同事务 allowed 审计；读路径经
authorize_read（team_memory.read_authorized cell）；revoke/delete 产生
ForgetManager 语义的级联事件并落台账与审计（F-R6-02 建议）。

本文件是重接后的 API 契约（由 tests/contract/memory/test_memory_center.py
的 stub 契约迁移而来，行为变更逐条登记于 findings 台账）：

- 行为收紧（矩阵语义）：confirm/correct/conflict/revoke 全部经冻结矩阵 cell
  裁决——member 不再能自确认/纠正/删除（stub 期无角色门），角色语义由
  Rego 裁决（policies/zhiwei/authz_test.rego），本文件用 FakeOPA 钉
  PolicyInput 形状（cell + 证据通道）与 wiring；
- F-R6-12：单记录读补 scope_subject 复核（他人 personal 记录与不存在同形 404）；
- delete 语义与 revoke 对齐：终态记录 409（stub 期无终态检查，恒 204）；
- conflicts：投影从 PG 记录派生（S7 §4「未解决冲突同时投影」），解析
  fail closed 409（无持久化冲突实体——DATA_MODEL 无 conflict 表，stub 的
  200-resolved 由进程内临时状态支撑，生产不可复现，登记于 R6 台账）。

事实源：specs/s7-memory.md §4/§5、ADR-006/009、docs/PERMISSIONS.md §3.1 行 10、
docs/review/findings/R6-privacy-compliance.md F-R6-02/03/12、
docs/review/findings/R3-authz-tenancy.md F-R3-09。
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

from zhiwei.api.memory import create_memory_router
from zhiwei.identity.domain import ActorContext, ActorRoleBinding, PrincipalKind
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
)
from zhiwei.memory.repositories import PgMemoryRepository
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import AuditEvent, MemoryLifecycleEventRow
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
        await repository.create_workspace(workspace_id, name="memory-center-api")
    return context


@pytest_asyncio.fixture
async def other_tenant(sessions) -> TenantContext:
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="memory-center-api-b")
    return context


@pytest_asyncio.fixture
async def policy() -> AsyncIterator[FakeOPA]:
    yield FakeOPA()


def _binding(name: str, org: UUID, ws: UUID | None = None) -> ActorRoleBinding:
    if ws is None:
        return ActorRoleBinding(name=name, scope="org", organization_id=org)
    return ActorRoleBinding(
        name=name, scope="workspace", organization_id=org, workspace_id=ws
    )


def _actor(
    context: TenantContext,
    principal: UUID = _USER_A,
    bindings: tuple[ActorRoleBinding, ...] = (),
) -> ActorContext:
    return ActorContext(
        principal_id=principal,
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        kind=PrincipalKind.USER,
        role_bindings=bindings,
    )


def _app(
    context: TenantContext,
    sessions: Any,
    policy: FakeOPA,
    *,
    principal: UUID = _USER_A,
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
        create_memory_router(
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


def _record(
    context: TenantContext,
    *,
    record_id: UUID | None = None,
    key: str = "editor.vim_mode",
    canonical_value: str = "enabled",
    scope: MemoryScope = MemoryScope.USER,
    subject_id: UUID = _USER_A,
    author: UUID = _USER_A,
    status: MemoryStatus = MemoryStatus.CANDIDATE,
    org: UUID | None = None,
    ws: UUID | None = None,
) -> MemoryRecord:
    resolved_ws = ws if ws is not None else context.workspace_id
    assert resolved_ws is not None
    return MemoryRecord(
        id=record_id or uuid4(),
        version=1,
        organization_id=org if org is not None else context.organization_id,
        workspace_id=resolved_ws,
        scope=scope,
        scope_subject_id=subject_id,
        type=MemoryType.PREFERENCE,
        subject=key,
        key=key,
        canonical_value=canonical_value,
        source_refs=(),
        observed_at=_NOW,
        confidence=0.8,
        sensitivity=SensitivityLevel.LOW,
        status=status,
        author_ref=author,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _seed(
    sessions: Any, context: TenantContext, record: MemoryRecord
) -> MemoryRecord:
    """生产写入路径播种（candidate/confirmed）；终态记录绕过写入路径直接
    落行（写入路径只产生活跃记录，终态是转移结果）。"""
    async with tenant_session(sessions, context) as session:
        repo = PgMemoryRepository(session, context)
        if record.status is MemoryStatus.CANDIDATE:
            return await repo.add_candidate(record)
        if record.status is MemoryStatus.CONFIRMED:
            return await repo.write_confirmed(record)
        from zhiwei.memory.repositories import record_to_row

        session.add(record_to_row(record))
        await session.flush()
        return record


async def _row(
    sessions: Any, context: TenantContext, record_id: UUID
) -> MemoryRecord | None:
    async with tenant_session(sessions, context) as session:
        return await PgMemoryRepository(session, context).get_by_id(record_id)


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


async def _lifecycle(sessions: Any, context: TenantContext) -> list[Any]:
    from sqlalchemy import select

    async with tenant_session(sessions, context) as session:
        rows = (
            await session.scalars(
                select(MemoryLifecycleEventRow).where(
                    MemoryLifecycleEventRow.organization_id == context.organization_id
                )
            )
        ).all()
        return list(rows)


async def _tenant_records(sessions: Any, context: TenantContext) -> list[MemoryRecord]:
    async with tenant_session(sessions, context) as session:
        return await PgMemoryRepository(session, context).list_for_tenant()


def _steward(context: TenantContext) -> tuple[ActorRoleBinding, ...]:
    """org 作用域 steward 绑定（冻结矩阵：team_memory.confirm 的角色）。"""
    return (_binding("memory_steward", context.organization_id),)


# ── 读路径：可见性 / F-R6-12 / PEP ────────────────────────────────────────


class TestMemoryReads:
    async def test_list_shows_own_and_team_hides_other_personal(
        self, tenant, other_tenant, sessions, policy
    ) -> None:
        """stub 契约迁移锚点（原 golden GUARD）：own personal + team 可见，
        他人 personal 与跨 org 隐藏。"""
        own = await _seed(sessions, tenant, _record(tenant, key="own.k"))
        team = await _seed(
            sessions,
            tenant,
            _record(tenant, key="team.k", scope=MemoryScope.TEAM, author=_USER_B),
        )
        await _seed(
            sessions,
            tenant,
            _record(tenant, key="other.k", subject_id=_USER_B, author=_USER_B),
        )
        await _seed(
            sessions,
            other_tenant,
            _record(other_tenant, key="cross.k", author=_USER_B),
        )
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get("/api/v1/memory/records")
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()}
        assert ids == {str(own.id), str(team.id)}

    async def test_list_denied_by_default(self, tenant, sessions, policy) -> None:
        """fail closed：FakeOPA 缺省 deny → 403（读路径 PEP 前置）。"""
        await _seed(sessions, tenant, _record(tenant))
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get("/api/v1/memory/records")
        assert resp.status_code == 403
        assert resp.json() == {"detail": "policy denied"}

    async def test_read_policy_input_shape(self, tenant, sessions, policy) -> None:
        """读路径 PolicyInput 形状锚点（A2a 残留②在 memory 面的落地）：
        cell = team_memory.read_authorized，actor 角色绑定透传。"""
        await _seed(sessions, tenant, _record(tenant))
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            await client.get("/api/v1/memory/records")
        assert len(policy.inputs) == 1
        policy_input = policy.inputs[0]
        assert policy_input["action"] == "read_authorized"
        assert policy_input["resource"]["type"] == "team_memory"
        assert policy_input["actor"]["principal_id"] == str(_USER_A)
        assert policy_input["actor"]["roles"][0]["name"] == "memory_steward"

    async def test_get_own_record(self, tenant, sessions, policy) -> None:
        record = await _seed(sessions, tenant, _record(tenant))
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get(f"/api/v1/memory/records/{record.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(record.id)
        assert resp.json()["canonical_value"] == "enabled"

    async def test_get_personal_record_of_other_user_404(
        self, tenant, sessions, policy
    ) -> None:
        """F-R6-12：同 org 内他人 personal 记录按 ID 不可枚举（与不存在同形）。"""
        record = await _seed(
            sessions,
            tenant,
            _record(tenant, subject_id=_USER_B, author=_USER_B),
        )
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get(f"/api/v1/memory/records/{record.id}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memory record not found"}

    async def test_get_unknown_record_404(self, tenant, sessions, policy) -> None:
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get(f"/api/v1/memory/records/{uuid4()}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memory record not found"}

    async def test_get_cross_org_record_404(
        self, tenant, other_tenant, sessions, policy
    ) -> None:
        """IDOR GUARD 迁移锚点：跨 org 记录读 404（tenant 作用域查询）。"""
        record = await _seed(
            sessions,
            other_tenant,
            _record(other_tenant, author=_USER_B),
        )
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get(f"/api/v1/memory/records/{record.id}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memory record not found"}


class TestMemoryListFilters:
    """stub 契约迁移锚点（S7 §5「按来源/类型/状态筛选」）——GREEN 补回
    （对抗审查 GAP-4：删除 stub 契约文件时遗漏的筛选断言）。"""

    async def test_filter_by_scope(self, tenant, sessions, policy) -> None:
        await _seed(sessions, tenant, _record(tenant, key="u.k"))
        await _seed(
            sessions, tenant, _record(tenant, key="t.k", scope=MemoryScope.TEAM)
        )
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get("/api/v1/memory/records?scope=team")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["scope"] == "team"

    async def test_filter_by_type(self, tenant, sessions, policy) -> None:
        await _seed(sessions, tenant, _record(tenant, key="pref.k"))
        fact = _record(tenant, key="fact.k")
        from zhiwei.memory.domain import MemoryType

        fact = fact.model_copy(update={"type": MemoryType.FACT, "subject": "fact.k"})
        await _seed(sessions, tenant, fact)
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get("/api/v1/memory/records?type=fact")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["type"] == "fact"

    async def test_filter_by_status(self, tenant, sessions, policy) -> None:
        await _seed(sessions, tenant, _record(tenant, key="cand.k"))
        await _seed(
            sessions,
            tenant,
            _record(tenant, key="conf.k", status=MemoryStatus.CONFIRMED),
        )
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get("/api/v1/memory/records?status=confirmed")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["status"] == "confirmed"

    async def test_filter_by_source(self, tenant, sessions, policy) -> None:
        from zhiwei.memory.domain import SourceRef

        with_source = _record(tenant, key="src.k")
        with_source = with_source.model_copy(
            update={
                "source_refs": (
                    SourceRef(source_id="s1", source_type="run", description=""),
                )
            }
        )
        await _seed(sessions, tenant, with_source)
        await _seed(sessions, tenant, _record(tenant, key="nosrc.k"))
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get("/api/v1/memory/records?source=run")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_team_scope_steward_confirm(self, tenant, sessions, policy) -> None:
        """stub 契约迁移锚点（旧 test_steward_can_confirm_team）：TEAM 记录
        确认成功路径。"""
        record = await _seed(
            sessions, tenant, _record(tenant, key="team.k", scope=MemoryScope.TEAM)
        )
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/confirm",
                json={"record_id": str(record.id)},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"


# ── confirm：PEP cell + 同事务审计 + 台账 ────────────────────────────────


class TestMemoryConfirm:
    async def test_confirm_persists_ledger_and_audit(self, tenant, sessions, policy) -> None:
        record = await _seed(sessions, tenant, _record(tenant))
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/confirm",
                json={"record_id": str(record.id)},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "confirmed"
        assert body["approver_ref"] == str(_USER_A)

        row = await _row(sessions, tenant, record.id)
        assert row is not None and row.status == MemoryStatus.CONFIRMED.value

        actions = [event.action for event in await _lifecycle(sessions, tenant)]
        assert "candidate.confirmed" in actions

        audits = await _audits(sessions, tenant)
        confirm = [a for a in audits if a.action == "memory.record.confirm"]
        assert len(confirm) == 1
        assert confirm[0].result == "allowed"
        assert confirm[0].resource_id == record.id
        assert confirm[0].actor_ref == f"user:{_USER_A}"
        assert confirm[0].request_id

    async def test_confirm_policy_input_shape(self, tenant, sessions, policy) -> None:
        """mutation PolicyInput 形状锚点：cell = team_memory.confirm，
        resource_id = 目标记录（PEP 先于数据访问）。"""
        record = await _seed(sessions, tenant, _record(tenant))
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            await client.post(
                f"/api/v1/memory/records/{record.id}/confirm",
                json={"record_id": str(record.id)},
            )
        assert len(policy.inputs) == 1
        policy_input = policy.inputs[0]
        assert policy_input["action"] == "confirm"
        assert policy_input["resource"]["type"] == "team_memory"
        assert policy_input["resource"]["id"] == str(record.id)

    async def test_confirm_denied_writes_denied_audit_zero_write(
        self, tenant, sessions, policy
    ) -> None:
        record = await _seed(sessions, tenant, _record(tenant))
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/confirm",
                json={"record_id": str(record.id)},
            )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "policy denied"}
        row = await _row(sessions, tenant, record.id)
        assert row is not None and row.status == MemoryStatus.CANDIDATE.value
        audits = [a for a in await _audits(sessions, tenant) if a.action == "memory.record.confirm"]
        assert len(audits) == 1
        assert audits[0].result == "denied"

    async def test_confirm_non_candidate_409_failed_audit(
        self, tenant, sessions, policy
    ) -> None:
        record = await _seed(
            sessions, tenant, _record(tenant, status=MemoryStatus.CONFIRMED)
        )
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/confirm",
                json={"record_id": str(record.id)},
            )
        assert resp.status_code == 409
        audits = [a for a in await _audits(sessions, tenant) if a.action == "memory.record.confirm"]
        assert len(audits) == 1
        assert audits[0].result == "failed"

    async def test_confirm_unknown_record_404(self, tenant, sessions, policy) -> None:
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{uuid4()}/confirm",
                json={"record_id": str(uuid4())},
            )
        assert resp.status_code == 404


# ── correct：supersede 纵切 ──────────────────────────────────────────────


class TestMemoryCorrect:
    async def test_correct_supersedes_original(self, tenant, sessions, policy) -> None:
        original = await _seed(
            sessions, tenant, _record(tenant, key="fix.k", canonical_value="old")
        )
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{original.id}/correct",
                json={
                    "record_id": str(original.id),
                    "canonical_value": "new",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["canonical_value"] == "new"
        assert body["status"] == "confirmed"
        assert body["version"] == 2

        old_row = await _row(sessions, tenant, original.id)
        assert old_row is not None
        assert old_row.status == MemoryStatus.SUPERSEDED.value
        assert old_row.superseded_by == UUID(body["id"])

        actions = sorted(event.action for event in await _lifecycle(sessions, tenant))
        assert "record.superseded" in actions
        assert "record.recorded" in actions

        audits = [a for a in await _audits(sessions, tenant) if a.action == "memory.record.correct"]
        assert len(audits) == 1
        assert audits[0].result == "allowed"


# ── revoke/delete：级联事件落台账与审计（F-R6-02）─────────────────────────


class TestMemoryRevokeAndDelete:
    async def test_revoke_emits_cascade_events_and_audits(
        self, tenant, sessions, policy
    ) -> None:
        """revoke 必须经 ForgetManager 语义产生 CascadeEvent（record/index/cache）
        并与转移同事务落 lifecycle 台账 + 审计链。"""
        record = await _seed(sessions, tenant, _record(tenant, key="revoke.k"))
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/revoke",
                json={"record_id": str(record.id), "reason": "outdated"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "revoked"
        assert body["tombstone"] is True

        row = await _row(sessions, tenant, record.id)
        assert row is not None
        assert row.status == MemoryStatus.REVOKED.value
        assert row.tombstone is True
        assert row.revoked_reason == "outdated"

        actions = [event.action for event in await _lifecycle(sessions, tenant)]
        assert "record.revoked" in actions
        assert "record.index_invalidated" in actions
        assert "record.cache_invalidated" in actions

        audit_actions = {a.action for a in await _audits(sessions, tenant)}
        assert "memory.record.revoke" in audit_actions
        assert "memory.record.index_invalidated" in audit_actions
        assert "memory.record.cache_invalidated" in audit_actions

    async def test_revoke_terminal_409_failed_audit(
        self, tenant, sessions, policy
    ) -> None:
        record = await _seed(
            sessions, tenant, _record(tenant, status=MemoryStatus.REVOKED)
        )
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/revoke",
                json={"record_id": str(record.id), "reason": "again"},
            )
        assert resp.status_code == 409
        audits = [a for a in await _audits(sessions, tenant) if a.action == "memory.record.revoke"]
        assert len(audits) == 1
        assert audits[0].result == "failed"

    async def test_revoke_denied_zero_write(self, tenant, sessions, policy) -> None:
        record = await _seed(sessions, tenant, _record(tenant, key="deny.k"))
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/revoke",
                json={"record_id": str(record.id), "reason": "nope"},
            )
        assert resp.status_code == 403
        row = await _row(sessions, tenant, record.id)
        assert row is not None and row.status == MemoryStatus.CANDIDATE.value

    async def test_delete_revokes_with_tombstone(self, tenant, sessions, policy) -> None:
        record = await _seed(sessions, tenant, _record(tenant, key="del.k"))
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/delete",
                json={"record_id": str(record.id)},
            )
        assert resp.status_code == 204
        row = await _row(sessions, tenant, record.id)
        assert row is not None
        assert row.status == MemoryStatus.REVOKED.value
        assert row.tombstone is True
        assert row.revoked_reason == "user delete"
        audits = [a for a in await _audits(sessions, tenant) if a.action == "memory.record.delete"]
        assert len(audits) == 1
        assert audits[0].result == "allowed"

    async def test_delete_terminal_record_409(self, tenant, sessions, policy) -> None:
        """行为收紧登记：delete 与 revoke 对齐终态检查（stub 期恒 204）。"""
        record = await _seed(
            sessions, tenant, _record(tenant, status=MemoryStatus.EXPIRED)
        )
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/delete",
                json={"record_id": str(record.id)},
            )
        assert resp.status_code == 409


# ── 跨租户 IDOR（迁移锚点）──────────────────────────────────────────────


class TestMemoryCrossTenant:
    async def test_cross_org_mutations_404_victim_untouched(
        self, tenant, other_tenant, sessions, policy
    ) -> None:
        """IDOR 迁移锚点：跨 org actor（org B）对 org A 记录的一切 mutation
        与不存在同形 404，受害者记录零变化（tenant 作用域查询先于副作用）。"""
        record = await _seed(sessions, tenant, _record(tenant, key="victim.k"))
        b_tenant = other_tenant
        policy.allow = True
        app = _app(
            b_tenant,
            sessions,
            policy,
            principal=_USER_B,
            bindings=_steward(b_tenant),
        )
        async with _client(app) as client:
            for path, payload in (
                ("confirm", {"record_id": str(record.id)}),
                ("correct", {"record_id": str(record.id), "canonical_value": "forged"}),
                ("revoke", {"record_id": str(record.id), "reason": "forged"}),
                ("delete", {"record_id": str(record.id)}),
            ):
                resp = await client.post(
                    f"/api/v1/memory/records/{record.id}/{path}", json=payload
                )
                assert resp.status_code == 404, (path, resp.text)
                assert resp.json() == {"detail": "memory record not found"}
        row = await _row(sessions, tenant, record.id)
        assert row is not None
        assert row.status == MemoryStatus.CANDIDATE.value
        assert row.canonical_value == "enabled"
        assert row.approver_ref is None
        assert row.tombstone is False

    async def test_correct_cross_org_inserts_no_forged_version(
        self, tenant, other_tenant, sessions, policy
    ) -> None:
        """IDOR 迁移锚点（旧 test_correct_cross_org_record_is_denied 的记录数
        断言）：跨 org correct 不得插入伪造 v2。"""
        record = await _seed(sessions, tenant, _record(tenant, key="solo.k"))
        b_tenant = other_tenant
        policy.allow = True
        app = _app(b_tenant, sessions, policy, principal=_USER_B, bindings=_steward(b_tenant))
        async with _client(app) as client:
            resp = await client.post(
                f"/api/v1/memory/records/{record.id}/correct",
                json={"record_id": str(record.id), "canonical_value": "forged"},
            )
        assert resp.status_code == 404
        rows = await _tenant_records(sessions, tenant)
        assert len(rows) == 1
        assert rows[0].id == record.id


# ── conflicts：派生投影 + 解析 fail closed ───────────────────────────────


class TestMemoryConflicts:
    async def _seed_coexisting_values(self, sessions: Any, context: TenantContext) -> None:
        """同键不同值的两条 confirmed 记录（ADR-009 写入路径不可达——绕过
        repo 直接播种，用于钉投影与 fail-closed 解析契约）。"""
        from zhiwei.memory.repositories import record_to_row

        base = _record(context, key="conflict.k", canonical_value="a", status=MemoryStatus.CONFIRMED)
        rows = [
            record_to_row(base),
            record_to_row(
                _record(
                    context,
                    record_id=uuid4(),
                    key="conflict.k",
                    canonical_value="b",
                    status=MemoryStatus.CONFIRMED,
                )
            ),
        ]
        async with tenant_session(sessions, context) as session:
            for row in rows:
                session.add(row)
            await session.flush()

    async def test_conflicts_empty_by_default(self, tenant, sessions, policy) -> None:
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.get("/api/v1/memory/conflicts")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_conflicts_project_coexisting_values(
        self, tenant, sessions, policy
    ) -> None:
        """S7 §4：同键不同值的并存记录投影为 conflict（确定性 id）。"""
        await self._seed_coexisting_values(sessions, tenant)
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            resp = await client.get("/api/v1/memory/conflicts")
        assert resp.status_code == 200
        conflicts = resp.json()
        assert len(conflicts) == 1
        assert conflicts[0]["kind"] == "value"
        assert conflicts[0]["resolved"] is False

    async def test_conflicts_ignore_terminal_and_superseded_history(
        self, tenant, sessions, policy
    ) -> None:
        """对抗审查 GAP-1 回归锚点：终态（revoked）与 superseded 历史不是
        未解决冲突——revoke 后同键重写、纠正链 A→B→C 不得投影幻影冲突
        （均为生产可达状态，走生产写入路径播种）。"""
        v1 = await _seed(sessions, tenant, _record(tenant, key="hist.k", canonical_value="v1"))
        policy.allow = True
        steward_app = _app(tenant, sessions, policy, bindings=_steward(tenant))
        async with _client(steward_app) as client:
            revoke = await client.post(
                f"/api/v1/memory/records/{v1.id}/revoke",
                json={"record_id": str(v1.id), "reason": "outdated"},
            )
            assert revoke.status_code == 200

        # revoke 后同键重写（add_candidate 只查活跃记录，插入不受阻）
        await _seed(sessions, tenant, _record(tenant, key="hist.k", canonical_value="v2"))

        # 纠正链 A→B→C：A、B 均 superseded，C confirmed 活跃
        a = await _seed(sessions, tenant, _record(tenant, key="chain.k", canonical_value="a"))
        async with _client(steward_app) as client:
            r1 = await client.post(
                f"/api/v1/memory/records/{a.id}/correct",
                json={"record_id": str(a.id), "canonical_value": "b"},
            )
            assert r1.status_code == 200
            b_id = r1.json()["id"]
            r2 = await client.post(
                f"/api/v1/memory/records/{b_id}/correct",
                json={"record_id": str(b_id), "canonical_value": "c"},
            )
            assert r2.status_code == 200

        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            listed = await client.get("/api/v1/memory/conflicts")
            assert listed.status_code == 200
            assert listed.json() == []
            stats = await client.get("/api/v1/memory/stats")
            assert stats.json()["unresolved_conflicts"] == 0

    async def test_resolve_fail_closed_409_no_persist(
        self, tenant, sessions, policy
    ) -> None:
        """解析 fail closed：无持久化冲突实体（DATA_MODEL 无 conflict 表），
        存量派生冲突一律 409——解析走 correct（supersede）路径；unknown 404。"""
        await self._seed_coexisting_values(sessions, tenant)
        policy.allow = True
        async with _client(
            _app(tenant, sessions, policy, bindings=_steward(tenant))
        ) as client:
            listed = await client.get("/api/v1/memory/conflicts")
            conflict_id = listed.json()[0]["conflict_id"]
            resp = await client.post(
                "/api/v1/memory/conflicts/resolve",
                json={"conflict_id": conflict_id},
            )
            assert resp.status_code == 409
            unknown = await client.post(
                "/api/v1/memory/conflicts/resolve",
                json={"conflict_id": str(uuid4())},
            )
            assert unknown.status_code == 404
        audits = [a for a in await _audits(sessions, tenant) if a.action == "memory.conflict.resolve"]
        assert len(audits) == 1
        assert audits[0].result == "failed"


# ── export / stats ───────────────────────────────────────────────────────


class TestMemoryExportStats:
    async def test_export_visible_records_only(self, tenant, sessions, policy) -> None:
        await _seed(sessions, tenant, _record(tenant, key="own.k"))
        await _seed(
            sessions, tenant, _record(tenant, key="team.k", scope=MemoryScope.TEAM, author=_USER_B)
        )
        await _seed(
            sessions,
            tenant,
            _record(tenant, key="other.k", subject_id=_USER_B, author=_USER_B),
        )
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.post("/api/v1/memory/export", json={})
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    async def test_export_filter_by_scope(self, tenant, sessions, policy) -> None:
        """stub 契约迁移锚点（旧 test_export_filter_by_scope）。"""
        await _seed(sessions, tenant, _record(tenant, key="e1.k"))
        await _seed(
            sessions, tenant, _record(tenant, key="e2.k", scope=MemoryScope.TEAM)
        )
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.post("/api/v1/memory/export", json={"scope": "team"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    async def test_stats_aggregates_visible_records(
        self, tenant, sessions, policy
    ) -> None:
        await _seed(sessions, tenant, _record(tenant, key="s1.k"))
        await _seed(
            sessions, tenant, _record(tenant, key="s2.k", scope=MemoryScope.TEAM, author=_USER_B)
        )
        policy.allow = True
        async with _client(_app(tenant, sessions, policy)) as client:
            resp = await client.get("/api/v1/memory/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_records"] == 2
        assert data["by_status"] == {"candidate": 2}
        assert set(data["by_scope"]) == {"user", "team"}
        assert data["by_type"] == {"preference": 2}
