"""F-R6-01/F-R3-03 RED——Evidence 失权三通道接入生产读路径。

api/evidence.py 现状：canonical claim 的 evidence_refs 逐字透传（无当前 ACL
复检、无 evidence_access_revoked 占位）——撤权后原用户回看历史 Run 仍可见
完整 Evidence 绑定（sql/locator/digest），ADR-006「失权呈现 not silent
removal」与「fail closed」在用户可见面未生效（F-R6-01/F-R3-03）。

重接契约：

- 端点 PEP：run_case_artifact.read（与 runs 读同 cell，前置）；
- 通道判定经 PEP：knowledge_source.read_provenance 允许 → AUDITOR 通道
  （全可见，reason=audit_channel）；否则 USER 通道（逐 ref 经
  resolve_evidence_views：当前 ACL 查 PG source_objects——ADR-006 公共入口，
  占位 {ref_id, status, reason}，任何内容字段不得出现）；
- EVAL_RECOMPUTE 通道无生产入口（S6 评审确认 eval 复算仅 offline sealed
  模式内成立）——不在 API 面承载，登记于台账；
- 未解析 ref（PG 无对应 source object / 载荷不可解析）→ fail closed 占位
  （acl_unknown），不静默移除、不透传内容；
- golden 形状守卫：占位键集恒为 {ref_id, status, reason}；可见视图 = ref
  元数据 + {ref_id, status, reason}。

事实源：specs/s6-evidence-ask.md §5、ADR-006、findings F-R6-01/F-R3-03、
tests/security/evidence_access/test_evidence_access.py（域层三通道契约）。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.api.evidence import create_evidence_router
from zhiwei.identity.domain import ActorContext
from zhiwei.knowledge.contracts import ACLSnapshot, Classification, SourceObject
from zhiwei.knowledge.pg_ledger import PgSourceLedger
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.events import (
    RunCompleted,
    RunCreated,
    RunStarted,
    TaskCompleted,
    TaskScheduled,
    TaskStarted,
)

pytestmark = pytest.mark.asyncio

ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_URL = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)

_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


class FakeOPA:
    """按 action 精确授权的本地假 OPA（fail closed：未声明 action 一律 deny）。

    二元 allow/deny 假件无法区分「run 读允许 + provenance 拒绝」的通道组合
    ——本假件按 PolicyInput.action 粒度声明 allow 集，钉住通道判定 wiring。"""

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.allowed_actions: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())["input"]
        self.inputs.append(payload)
        allow = payload.get("action") in self.allowed_actions
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
        await repository.create_workspace(workspace_id, name="evidence-visibility")
    return context


@pytest_asyncio.fixture
async def policy() -> AsyncIterator[FakeOPA]:
    yield FakeOPA()


def _actor(context: TenantContext, principal: UUID) -> ActorContext:
    return ActorContext(
        principal_id=principal,
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


def _app(
    context: TenantContext,
    sessions: Any,
    policy: FakeOPA,
    principal: UUID,
) -> FastAPI:
    from zhiwei.policy.client import OPAClient
    from zhiwei.policy.enforcement import PolicyEnforcer

    client = OPAClient(
        "http://opa.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(policy.handler)),
    )
    app = FastAPI()
    app.include_router(
        create_evidence_router(
            actor_dependency=lambda: _actor(context, principal),
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


async def _seed_source(
    sessions: Any,
    context: TenantContext,
    *,
    acl: ACLSnapshot,
) -> UUID:
    assert context.workspace_id is not None
    async with tenant_session(sessions, context) as session:
        ledger = PgSourceLedger(session, context)
        obj = SourceObject(
            id=uuid4(),
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            source_type="document",
            acl=acl,
            classification=Classification.PUBLIC,
            metadata={},
        )
        await ledger.register_object(obj)
        return obj.id


def _ref_wire(source_id: UUID, *, ref_id: UUID | None = None) -> dict[str, Any]:
    return {
        "ref_id": str(ref_id or uuid4()),
        "ref_type": "QueryReplay",
        "reproducibility_level": "replayable",
        "source_id": str(source_id),
        "sql": "SELECT secret_value FROM ledger",
        "params": {},
        "classification": "PUBLIC",
        "created_at": "2025-06-15T12:00:00+00:00",
    }


async def _seed_run_with_claims(
    sessions: Any,
    context: TenantContext,
    run_id: UUID,
    claims: list[dict[str, Any]],
) -> None:
    from zhiwei.contracts.time import utc_now

    now = utc_now()
    async with tenant_session(sessions, context) as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO runs (id, organization_id, workspace_id, status, schema_version)"
                " VALUES (:id, :org, :ws, 'completed', 1)"
            ),
            {"id": run_id, "org": context.organization_id, "ws": context.workspace_id},
        )
    async with tenant_session(sessions, context) as session:
        store = RuntimeEventStore(session, context)
        attempt = uuid4()
        node = TaskGraphNode(task_id="t1", task_type="Analyze", required_capability="x")
        events: list[Any] = [
            RunCreated(
                run_id=run_id,
                timestamp=now,
                graph=TaskGraph(nodes={"t1": node}, edges={}),
            ),
            RunStarted(run_id=run_id, timestamp=now),
            TaskScheduled(run_id=run_id, timestamp=now, task_id="t1"),
            TaskStarted(run_id=run_id, timestamp=now, task_id="t1", attempt_id=attempt),
            TaskCompleted(
                run_id=run_id,
                timestamp=now + timedelta(seconds=1),
                task_id="t1",
                output_values={
                    "claims": claims,
                    "unknowns": [],
                    "verification": {"verification_ok": True, "exit_code": 0, "check_count": 1},
                    "answer": {"status": "completed", "claims": []},
                },
            ),
            RunCompleted(run_id=run_id, timestamp=now + timedelta(seconds=2)),
        ]
        for index, event in enumerate(events):
            await store.append(
                event, actor_ref="evidence-visibility-test", idempotency_key=f"{run_id}:{index}"
            )


def _claim(refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "claim_id": "cross-source/claim-1",
        "claim_type": "Fact",
        "evidence_refs": refs,
    }


class TestEvidenceVisibility:
    async def test_acl_granted_ref_is_visible_with_content(
        self, tenant, sessions, policy
    ) -> None:
        principal = uuid4()
        source_id = await _seed_source(
            sessions, tenant, acl=ACLSnapshot(allowed_principals=(str(principal),))
        )
        run_id = uuid4()
        ref_id = uuid4()
        await _seed_run_with_claims(
            sessions, tenant, run_id, [_claim([_ref_wire(source_id, ref_id=ref_id)])]
        )
        policy.allowed_actions = {"read"}
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 200, resp.text
        refs = resp.json()["claims"][0]["evidence_refs"]
        assert len(refs) == 1
        view = refs[0]
        assert view["status"] == "visible"
        assert view["ref_id"] == str(ref_id)
        assert view["sql"] == "SELECT secret_value FROM ledger"

    async def test_revoked_ref_renders_placeholder_without_content(
        self, tenant, sessions, policy
    ) -> None:
        """ADR-006 失权呈现：ACL 未授予 → 占位（{ref_id, status, reason}），
        sql/digest 等内容字段不得出现；条目不消失（不静默移除）。"""
        principal = uuid4()
        source_id = await _seed_source(
            sessions, tenant, acl=ACLSnapshot(allowed_principals=(str(uuid4()),))
        )
        run_id = uuid4()
        ref_id = uuid4()
        await _seed_run_with_claims(
            sessions, tenant, run_id, [_claim([_ref_wire(source_id, ref_id=ref_id)])]
        )
        policy.allowed_actions = {"read"}
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 200
        refs = resp.json()["claims"][0]["evidence_refs"]
        assert len(refs) == 1
        assert refs[0] == {
            "ref_id": str(ref_id),
            "status": "evidence_access_revoked",
            "reason": "not_in_acl",
        }

    async def test_unknown_source_renders_acl_unknown_placeholder(
        self, tenant, sessions, policy
    ) -> None:
        """PG 无对应 source object 的 ref（历史 run/跨租户残引）→ fail closed
        占位（acl_unknown），不透传内容。"""
        principal = uuid4()
        run_id = uuid4()
        ref_id = uuid4()
        await _seed_run_with_claims(
            sessions, tenant, run_id, [_claim([_ref_wire(uuid4(), ref_id=ref_id)])]
        )
        policy.allowed_actions = {"read"}
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 200
        refs = resp.json()["claims"][0]["evidence_refs"]
        assert refs[0] == {
            "ref_id": str(ref_id),
            "status": "evidence_access_revoked",
            "reason": "acl_unknown",
        }

    async def test_malformed_ref_fails_closed(self, tenant, sessions, policy) -> None:
        """载荷不可解析的 ref → 占位（fail closed），不透传、不静默移除。"""
        principal = uuid4()
        await _seed_source(sessions, tenant, acl=ACLSnapshot())
        run_id = uuid4()
        await _seed_run_with_claims(
            sessions,
            tenant,
            run_id,
            [_claim([{"ref_id": str(uuid4()), "garbage": True}])],
        )
        policy.allowed_actions = {"read"}
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 200
        refs = resp.json()["claims"][0]["evidence_refs"]
        assert refs[0]["status"] == "evidence_access_revoked"
        assert set(refs[0]) == {"ref_id", "status", "reason"}

    async def test_auditor_channel_via_pep_cell(
        self, tenant, sessions, policy
    ) -> None:
        """通道判定经 PEP：knowledge_source.read_provenance 允许 → AUDITOR
        通道全可见（reason=audit_channel），不查当前 ACL。"""
        principal = uuid4()
        source_id = await _seed_source(
            sessions, tenant, acl=ACLSnapshot(allowed_principals=(str(uuid4()),))
        )
        run_id = uuid4()
        ref_id = uuid4()
        await _seed_run_with_claims(
            sessions, tenant, run_id, [_claim([_ref_wire(source_id, ref_id=ref_id)])]
        )
        policy.allowed_actions = {"read", "read_provenance"}
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 200
        view = resp.json()["claims"][0]["evidence_refs"][0]
        assert view["status"] == "visible"
        assert view["reason"] == "audit_channel"
        assert view["sql"] == "SELECT secret_value FROM ledger"
        # 两次 PEP 求值：run 读 + provenance 通道
        actions = [i["action"] for i in policy.inputs]
        assert actions.count("read") == 1
        assert actions.count("read_provenance") == 1

    async def test_run_read_pep_deny_403(self, tenant, sessions, policy) -> None:
        principal = uuid4()
        run_id = uuid4()
        await _seed_run_with_claims(sessions, tenant, run_id, [_claim([])])
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 403
        assert resp.json() == {"detail": "policy denied"}

    async def test_group_allow_via_identity_group_membership(
        self, tenant, sessions, policy
    ) -> None:
        """对抗审查 Major-1 回归锚点：group 授权形态（ADR-006 granted→visible
        的另一半）——principal 所属 workspace 分组经 group_members 身份侧解析，
        命中 snapshot.allowed_groups → visible。"""
        from sqlalchemy import text

        principal = uuid4()
        source_id = await _seed_source(
            sessions,
            tenant,
            acl=ACLSnapshot(allowed_groups=("workspace-members",)),
        )
        # principals 属 identity 面（migrator 管理，app 角色无 DML）——直连播种
        import asyncpg

        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            await connection.execute(
                "INSERT INTO principals (id, kind, schema_version)"
                " VALUES ($1, 'user', 1) ON CONFLICT DO NOTHING",
                principal,
            )
        finally:
            await connection.close()
        async with tenant_session(sessions, tenant) as session:
            await session.execute(
                text(
                    "INSERT INTO groups (id, organization_id, workspace_id, name,"
                    " schema_version) VALUES (:gid, :org, :ws, 'workspace-members', 1)"
                ),
                {"gid": uuid4(), "org": tenant.organization_id, "ws": tenant.workspace_id},
            )
            await session.execute(
                text(
                    "INSERT INTO group_members (group_id, organization_id,"
                    " workspace_id, principal_id) VALUES (:gid, :org, :ws, :pid)"
                ),
                {
                    "gid": (
                        await session.execute(
                            text(
                                "SELECT id FROM groups WHERE name = 'workspace-members'"
                                " AND workspace_id = :ws"
                            ),
                            {"ws": tenant.workspace_id},
                        )
                    ).scalar_one(),
                    "org": tenant.organization_id,
                    "ws": tenant.workspace_id,
                    "pid": principal,
                },
            )
        run_id = uuid4()
        ref_id = uuid4()
        await _seed_run_with_claims(
            sessions, tenant, run_id, [_claim([_ref_wire(source_id, ref_id=ref_id)])]
        )
        policy.allowed_actions = {"read"}
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 200
        view = resp.json()["claims"][0]["evidence_refs"][0]
        assert view["status"] == "visible"
        assert view["ref_id"] == str(ref_id)

    async def test_supporting_inputs_refs_resolved(
        self, tenant, sessions, policy
    ) -> None:
        """对抗审查 Major-2 回归锚点：Inference/Recommendation 域形状把 refs
        挂在 supporting_inputs——source_id 收集与解析必须覆盖（假拒绝关闭）。"""
        principal = uuid4()
        source_id = await _seed_source(
            sessions, tenant, acl=ACLSnapshot(allowed_principals=(str(principal),))
        )
        run_id = uuid4()
        ref_id = uuid4()
        claim = {
            "claim_id": "cross-source/inference-1",
            "claim_type": "Inference",
            "supporting_inputs": [_ref_wire(source_id, ref_id=ref_id)],
        }
        await _seed_run_with_claims(sessions, tenant, run_id, [claim])
        policy.allowed_actions = {"read"}
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 200
        view = resp.json()["claims"][0]["evidence_refs"][0]
        assert view["status"] == "visible"
        assert view["ref_id"] == str(ref_id)

    async def test_cross_workspace_run_is_404(
        self, tenant, sessions, policy
    ) -> None:
        """同 org 跨 workspace 的 run：run 读 PEP（tenant 谓词）+ 事件加载
        作用域 → 404 同形。"""
        principal = uuid4()
        run_id = uuid4()
        await _seed_run_with_claims(sessions, tenant, run_id, [_claim([])])
        ws2 = TenantContext(
            organization_id=tenant.organization_id, workspace_id=uuid4()
        )
        assert ws2.workspace_id is not None
        async with tenant_session(sessions, ws2) as session:
            from zhiwei.persistence.repositories import TenantRepository

            await TenantRepository(session, ws2).create_workspace(
                ws2.workspace_id, name="evidence-ws2"
            )
        policy.allowed_actions = {"read"}
        async with _client(_app(ws2, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 404

    async def test_cross_workspace_source_is_acl_unknown(
        self, tenant, sessions, policy
    ) -> None:
        """同 org 跨 workspace 的 source object：get_current_acls 的 ws 谓词
        缺席 → fail closed 占位（acl_unknown）。"""
        principal = uuid4()
        ws2 = TenantContext(
            organization_id=tenant.organization_id, workspace_id=uuid4()
        )
        assert ws2.workspace_id is not None
        async with tenant_session(sessions, ws2) as session:
            from zhiwei.persistence.repositories import TenantRepository

            await TenantRepository(session, ws2).create_workspace(
                ws2.workspace_id, name="evidence-ws2"
            )
        foreign_ws_source = await _seed_source(sessions, ws2, acl=ACLSnapshot())
        run_id = uuid4()
        ref_id = uuid4()
        await _seed_run_with_claims(
            sessions,
            tenant,
            run_id,
            [_claim([_ref_wire(foreign_ws_source, ref_id=ref_id)])],
        )
        policy.allowed_actions = {"read"}
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 200
        view = resp.json()["claims"][0]["evidence_refs"][0]
        assert view == {
            "ref_id": str(ref_id),
            "status": "evidence_access_revoked",
            "reason": "acl_unknown",
        }

    async def test_read_denial_writes_no_audit(
        self, tenant, sessions, policy
    ) -> None:
        """读不写审计（冻结语义）的结构性断言：deny 403 + 零审计行。"""
        from sqlalchemy import func, select

        from zhiwei.persistence.models import AuditEvent

        principal = uuid4()
        run_id = uuid4()
        await _seed_run_with_claims(sessions, tenant, run_id, [_claim([])])
        async with tenant_session(sessions, tenant) as session:
            before = await session.scalar(
                select(func.count()).select_from(AuditEvent)
            )
        async with _client(_app(tenant, sessions, policy, principal)) as client:
            resp = await client.get(f"/api/v1/runs/{run_id}/evidence")
        assert resp.status_code == 403
        async with tenant_session(sessions, tenant) as session:
            after = await session.scalar(select(func.count()).select_from(AuditEvent))
        assert after == before
