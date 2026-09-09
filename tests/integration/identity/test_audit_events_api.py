"""F-R6-09 RED：GET /api/v1/audit-events（org.read_audit cell + RLS + 游标分页）。

finding 证据：api/ 无审计列表端点（events.py 是 run SSE）；docs/PERMISSIONS.md
的 org.read_audit cell 仅被 observability costs/failures 复用——Auditor 角色的
核心 journey（S1 §4）在产品内不可达，审计数据只能 DB 直连。

契约（按 plan T-P2a.5）：读 cell org.read_audit（PEP 前置，deny → 403 policy
denied）；audit_events 的 FORCE RLS 保证跨 org 不可见（org 级列表，无 ws 谓
词）；游标分页 keyset (created_at DESC, id DESC)——cursor 缺省首页、翻页不重
不漏、耗尽 next_cursor=None；非法 cursor → 422 invalid cursor。响应只含审计
元数据列（不含 payload 正文——审计行本无正文，payload_digest 指纹含在内）。
"""

from __future__ import annotations

import hashlib
import uuid as uuid_module
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fixtures.policy_fake import FakePolicyEnforcer
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.integration.telemetry.test_costs_persistence import _insert_tenant_rows
from zhiwei.api.audit import create_audit_router
from zhiwei.identity.audit import AuditRecord, append_audit
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import AuditEvent
from zhiwei.persistence.tenant import TenantContext, tenant_session

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_SQLALCHEMY_URL = ("postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test").replace(
    "postgresql://", "postgresql+asyncpg://", 1
)


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    url = APP_SQLALCHEMY_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


def _actor(organization_id: UUID | None, workspace_id: UUID | None = None) -> ActorContext:
    return ActorContext(
        principal_id=uuid4(), organization_id=organization_id, workspace_id=workspace_id
    )


def _router_app(
    actor: ActorContext, policy_enforcer=None
) -> tuple[FastAPI, async_sessionmaker[AsyncSession], AsyncEngine]:
    sessions = create_session_factory(create_database_engine(APP_SQLALCHEMY_URL))
    app = FastAPI()
    app.include_router(
        create_audit_router(
            actor_dependency=lambda: actor,
            sessions=sessions,
            policy_enforcer=policy_enforcer or FakePolicyEnforcer(allow=True),
        )
    )
    return app, sessions, sessions.kw["bind"]


def _record(
    organization_id: UUID,
    workspace_id: UUID | None,
    *,
    action: str = "test.action",
    result: str = "denied",
) -> AuditRecord:
    allowed = result == "allowed"
    return AuditRecord(
        organization_id=organization_id,
        workspace_id=workspace_id,
        action=action,
        resource_type="test_resource",
        resource_id=uuid4(),
        resource_version=0,
        actor_ref=f"user:{uuid4()}",
        effective_identity_ref=f"user:{uuid4()}",
        decision_id="test-decision" if allowed else None,
        policy_revision="test-rev" if allowed else None,
        decision_reason="seed",
        result=result,  # type: ignore[arg-type]
        request_id=uuid_module.uuid4().hex,
        trace_id=uuid_module.uuid4().hex,
        payload_digest="sha256:" + hashlib.sha256(uuid_module.uuid4().bytes).hexdigest(),
    )


async def _seed_event(
    sessions: async_sessionmaker[AsyncSession],
    organization_id: UUID,
    workspace_id: UUID | None,
    *,
    action: str = "test.action",
    result: str = "denied",
) -> UUID:
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        event = await append_audit(session, context, _record(organization_id, workspace_id, action=action, result=result))
        return event.id


class TestAuditEventsAPI:
    async def test_actor_without_org_context_is_refused(self) -> None:
        app, _sessions, _engine = _router_app(_actor(None))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/audit-events")
        assert response.status_code == 403
        assert response.json() == {"detail": "outside tenant scope"}

    async def test_list_denied_without_read_audit_cell(self) -> None:
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        app, _sessions, _engine = _router_app(
            _actor(organization_id), FakePolicyEnforcer(allow=False)
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/audit-events")
        assert response.status_code == 403
        assert response.json() == {"detail": "policy denied"}

    async def test_list_returns_org_events_newest_first(self) -> None:
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        other_org, other_ws = uuid4(), uuid4()
        await _insert_tenant_rows(other_org, other_ws)
        app, sessions, _engine = _router_app(_actor(organization_id, workspace_id))
        own_ids = [
            await _seed_event(sessions, organization_id, workspace_id, action="own.a"),
            await _seed_event(sessions, organization_id, workspace_id, action="own.b"),
            await _seed_event(sessions, organization_id, None, action="own.orglevel"),
        ]
        await _seed_event(sessions, other_org, other_ws, action="foreign.a")
        await _seed_event(sessions, other_org, None, action="foreign.b")
        # 同 org 其他 workspace 的审计行：actor 未携带该 ws 上下文 → RLS 不可见
        other_ws_same_org = uuid4()
        import asyncpg as _asyncpg

        from tests.integration.telemetry.test_costs_persistence import ADMIN_DSN as _ADMIN_DSN

        conn = await _asyncpg.connect(_ADMIN_DSN)
        try:
            await conn.execute(
                "INSERT INTO workspaces (id, organization_id, name, schema_version) "
                "VALUES ($1, $2, $3, 1)",
                other_ws_same_org,
                organization_id,
                f"audit-otherws-{other_ws_same_org}",
            )
        finally:
            await conn.close()
        await _seed_event(
            sessions, organization_id, other_ws_same_org, action="own.otherws"
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/audit-events")
        assert response.status_code == 200, response.text
        body = response.json()
        events = body["events"]
        assert {event["id"] for event in events} == {str(i) for i in own_ids}
        stamps = [event["created_at"] for event in events]
        assert stamps == sorted(stamps, reverse=True), stamps
        for event in events:
            assert event["organization_id"] == str(organization_id)
            for field in (
                "action",
                "resource_type",
                "resource_id",
                "actor_ref",
                "result",
                "decision_reason",
                "request_id",
                "trace_id",
                "created_at",
            ):
                assert field in event, field

    async def test_cursor_pagination_no_overlap_no_gap(self) -> None:
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        app, sessions, _engine = _router_app(_actor(organization_id, workspace_id))
        seeded = {
            await _seed_event(sessions, organization_id, workspace_id, action=f"page.{i}")
            for i in range(5)
        }
        collected: list[dict] = []
        cursor: str | None = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _page in range(10):
                params = {"limit": "2"}
                if cursor is not None:
                    params["cursor"] = cursor
                response = await client.get("/api/v1/audit-events", params=params)
                assert response.status_code == 200, response.text
                body = response.json()
                collected.extend(body["events"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break
        assert cursor is None, "pagination did not exhaust"
        assert {event["id"] for event in collected} == {str(i) for i in seeded}
        assert len(collected) == 5
        stamps = [event["created_at"] for event in collected]
        # 非增即可（微秒碰撞时 id 决胜，不假设严格降序）
        assert stamps == sorted(stamps, reverse=True), stamps

    async def test_cursor_tie_break_on_identical_created_at(self) -> None:
        """同 created_at 平局：keyset 的 id 决胜分支（时钟粒度碰撞/批量补写时
        唯一保证续页不重不漏的分支）。构造方式：直插固定 created_at 的审计行
        ——audit_events 对 app 角色只追加（UPDATE 拒绝），append_audit 的
        utc_now 是逐行 Python 时钟无法自然构造平局；链位列为直插 fabricate
        （本测试只锚定列表/分页语义，不锚定链完整性）。"""
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        app, sessions, _engine = _router_app(_actor(organization_id, workspace_id))
        fixed_stamp = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
        context = TenantContext(
            organization_id=organization_id, workspace_id=workspace_id
        )
        seeded: list[UUID] = []
        async with tenant_session(sessions, context) as session:
            previous_digest: str | None = None
            for index in range(4):
                # 链式 prev→digest：uq_audit_events_scope_previous 对 scope 内
                # prev 值唯一（NULL 亦不重复），直插行必须成链
                event_digest = "sha256:" + uuid_module.uuid4().hex + uuid_module.uuid4().hex
                row = AuditEvent(
                    id=uuid4(),
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    action=f"tie.{index}",
                    resource_type="test_resource",
                    resource_id=uuid4(),
                    actor_ref=f"user:{uuid4()}",
                    payload_digest="sha256:" + "b" * 64,
                    previous_event_digest=previous_digest,
                    event_digest=event_digest,
                    schema_version=1,
                    created_at=fixed_stamp,
                    audit_schema_version=2,
                    effective_identity_ref=f"user:{uuid4()}",
                    resource_version=0,
                    decision_id=None,
                    policy_revision=None,
                    decision_reason="tie-seed",
                    result="denied",
                    request_id=uuid_module.uuid4().hex,
                    trace_id=uuid_module.uuid4().hex,
                )
                previous_digest = event_digest
                session.add(row)
                seeded.append(row.id)
        collected: list[dict] = []
        cursor: str | None = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _page in range(10):
                params: dict[str, str] = {"limit": "2"}
                if cursor is not None:
                    params["cursor"] = cursor
                response = await client.get("/api/v1/audit-events", params=params)
                assert response.status_code == 200, response.text
                body = response.json()
                collected.extend(body["events"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break
        assert cursor is None
        assert {event["id"] for event in collected} == {str(i) for i in seeded}
        # 平局页内不重复、页间不重（created_at < after 单独实现会在平局处死循环/漏行）
        ids = [event["id"] for event in collected]
        assert len(ids) == len(set(ids))
        assert all(
            datetime.fromisoformat(event["created_at"]) == fixed_stamp for event in collected
        )

    async def test_naive_timestamp_cursor_is_422(self) -> None:
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        app, _sessions, _engine = _router_app(_actor(organization_id, workspace_id))
        naive = f"2026-09-06T12:00:00|{uuid4()}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/audit-events", params={"cursor": naive})
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid cursor"}

    async def test_limit_bounds_are_422(self) -> None:
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        app, _sessions, _engine = _router_app(_actor(organization_id, workspace_id))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            low = await client.get("/api/v1/audit-events", params={"limit": "0"})
            high = await client.get("/api/v1/audit-events", params={"limit": "201"})
        assert low.status_code == 422
        assert high.status_code == 422

    async def test_invalid_cursor_is_422(self) -> None:
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        app, _sessions, _engine = _router_app(_actor(organization_id, workspace_id))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/audit-events", params={"cursor": "not-a-cursor"}
            )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid cursor"}
