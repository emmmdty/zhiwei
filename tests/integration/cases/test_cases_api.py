"""S10-T4b integration：Case surface 与 run evidence 投影契约（S6 收口补齐）。

事实源：specs/s6-evidence-ask.md §4/§5、docs/handoffs/s6-ask-evidence-e2e-exception.md
（解锁条件：Case/Evidence 真实 API + 前端消费面）、S6-T3 冻结 Case 聚合
（src/zhiwei/cases/domain.py 生命周期状态机）。

- Case router（api/cases.py）：POST /api/v1/runs/{run_id}/cases 仅接受终态 run
  （PG canonical reduce 判定；非终态 409、跨租户/未知 404 防枚举）；GET
  /api/v1/cases、GET /api/v1/cases/{id}、GET /api/v1/runs/{run_id}/cases 全部
  租户隔离；mutation 走生产 policy 纵切（RUN_CASE_ARTIFACT × manage_visible_cases），
  denied → 403 且业务零写入；创建落 case.created 生命周期台账（0017 case_events）；
- Evidence router（api/evidence.py）：GET /api/v1/runs/{run_id}/evidence 只投影
  canonical 事件已携带的 claim/verify/answer 形态——不发明 canonical 之外的字段；
- 持久化（0017_cases）：cases 行 + case_events 台账，FORCE RLS，与 ORM 约束镜像。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from zhiwei.api.cases import create_cases_router
from zhiwei.api.evidence import create_evidence_router

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.identity.domain import ActorContext
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
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)


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
        await repository.create_workspace(workspace_id, name="cases-api")
    return context


class FakeOPA:
    """本地假 OPA：响应形状对齐 client 校验（decision_id/result/provenance）。"""

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.deny = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.inputs.append(json.loads(request.read())["input"])
        allow = not self.deny
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


@pytest_asyncio.fixture
async def policy_enforcer() -> AsyncIterator[Any]:
    from zhiwei.policy.client import OPAClient
    from zhiwei.policy.enforcement import PolicyEnforcer

    fake = FakeOPA()
    client = OPAClient(
        "http://opa.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    try:
        yield PolicyEnforcer(client)
    finally:
        await client.aclose()


def _actor(context: TenantContext) -> ActorContext:
    return ActorContext(
        principal_id=uuid4(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


def _cases_app(context: TenantContext, sessions, policy_enforcer) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_cases_router(
            actor_dependency=lambda: _actor(context),
            sessions=sessions,
            policy_enforcer=policy_enforcer,
        )
    )
    return app


def _evidence_app(context: TenantContext, sessions) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_evidence_router(
            actor_dependency=lambda: _actor(context),
            sessions=sessions,
        )
    )
    return app


def _single_task_graph() -> TaskGraph:
    node = TaskGraphNode(task_id="t1", task_type="Fixture", required_capability="fixture")
    return TaskGraph(nodes={"t1": node}, edges={})


async def _seed_run(
    sessions,
    context: TenantContext,
    run_id: UUID,
    *,
    terminal: bool,
    output_values: dict[str, Any] | None = None,
) -> None:
    """Run 行 + canonical 事件链（不经 Temporal：事件即真相，reduce 判定终态）。"""
    from zhiwei.contracts.time import utc_now

    now = utc_now()
    async with tenant_session(sessions, context) as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO runs (id, organization_id, workspace_id, status, schema_version)"
                " VALUES (:id, :org, :ws, 'running', 1)"
            ),
            {"id": run_id, "org": context.organization_id, "ws": context.workspace_id},
        )
    async with tenant_session(sessions, context) as session:
        store = RuntimeEventStore(session, context)
        attempt = uuid4()
        events: list[Any] = [
            RunCreated(run_id=run_id, timestamp=now, graph=_single_task_graph()),
            RunStarted(run_id=run_id, timestamp=now),
            TaskScheduled(run_id=run_id, timestamp=now, task_id="t1"),
            TaskStarted(run_id=run_id, timestamp=now, task_id="t1", attempt_id=attempt),
            TaskCompleted(
                run_id=run_id, timestamp=now, task_id="t1",
                output_values=output_values or {},
            ),
        ]
        if terminal:
            events.append(RunCompleted(run_id=run_id, timestamp=now))
        for index, event in enumerate(events):
            await store.append(
                event, actor_ref="cases-api-test", idempotency_key=f"{run_id}:{index}"
            )


class TestCaseCreationFromRun:
    async def test_create_case_from_completed_run(self, tenant, sessions, policy_enforcer) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=True)
        transport = ASGITransport(app=_cases_app(tenant, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                f"/api/v1/runs/{run_id}/cases",
                json={"title": "Cross-source finding", "description": "needs triage"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["run_id"] == str(run_id)
            assert body["title"] == "Cross-source finding"
            assert body["description"] == "needs triage"
            assert body["status"] == "created"
            assert body["organization_id"] == str(tenant.organization_id)
            assert body["workspace_id"] == str(tenant.workspace_id)

            # 台账：case.created 生命周期事件落 case_events（同一事务）
            async with tenant_session(sessions, tenant) as session:
                from sqlalchemy import text

                row = await session.execute(
                    text(
                        "SELECT event_type FROM case_events WHERE case_id = :case_id"
                    ),
                    {"case_id": UUID(body["id"])},
                )
                assert row.scalar_one() == "case.created"

    async def test_create_case_from_non_terminal_run_is_409(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=False)
        transport = ASGITransport(app=_cases_app(tenant, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                f"/api/v1/runs/{run_id}/cases",
                json={"title": "too early"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert created.status_code == 409, created.text

    async def test_create_case_from_unknown_run_is_404(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        transport = ASGITransport(app=_cases_app(tenant, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                f"/api/v1/runs/{uuid4()}/cases",
                json={"title": "ghost"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert created.status_code == 404, created.text

    async def test_create_case_cross_tenant_run_is_404(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=True)
        other = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        transport = ASGITransport(app=_cases_app(other, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                f"/api/v1/runs/{run_id}/cases",
                json={"title": "cross tenant"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert created.status_code == 404, created.text

    async def test_policy_denied_case_creation_is_403_and_writes_nothing(
        self, tenant, sessions
    ) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=True)
        # deny 开关：OPA 拒绝 → gate 403，业务事务不得开始（cases 零写入）
        fake = FakeOPA()
        fake.deny = True
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        denying = PolicyEnforcer(
            OPAClient(
                "http://opa.test",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
            )
        )
        transport = ASGITransport(app=_cases_app(tenant, sessions, denying))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                f"/api/v1/runs/{run_id}/cases",
                json={"title": "denied"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert created.status_code == 403, created.text
            assert "policy denied" in created.json()["detail"]
        async with tenant_session(sessions, tenant) as session:
            from sqlalchemy import text

            count = await session.scalar(text("SELECT count(*) FROM cases"))
            assert count == 0

    async def test_create_case_requires_idempotency_key(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=True)
        transport = ASGITransport(app=_cases_app(tenant, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                f"/api/v1/runs/{run_id}/cases",
                json={"title": "no key"},
            )
            assert created.status_code == 422, created.text


class TestCaseReadSurface:
    async def _create_case(self, tenant, sessions, policy_enforcer, run_id: UUID) -> dict:
        transport = ASGITransport(app=_cases_app(tenant, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                f"/api/v1/runs/{run_id}/cases",
                json={"title": "triage me"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert created.status_code == 201, created.text
            return created.json()

    async def test_list_get_and_run_scoped_listing(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=True)
        created = await self._create_case(tenant, sessions, policy_enforcer, run_id)
        transport = ASGITransport(app=_cases_app(tenant, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            listing = await client.get("/api/v1/cases")
            assert listing.status_code == 200
            assert [c["id"] for c in listing.json()] == [created["id"]]

            detail = await client.get(f"/api/v1/cases/{created['id']}")
            assert detail.status_code == 200
            assert detail.json()["run_id"] == str(run_id)
            assert detail.json()["status"] == "created"

            scoped = await client.get(f"/api/v1/runs/{run_id}/cases")
            assert scoped.status_code == 200
            assert [c["id"] for c in scoped.json()] == [created["id"]]

    async def test_unknown_case_is_404(self, tenant, sessions, policy_enforcer) -> None:
        transport = ASGITransport(app=_cases_app(tenant, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            detail = await client.get(f"/api/v1/cases/{uuid4()}")
            assert detail.status_code == 404

    async def test_cross_tenant_case_is_404(self, tenant, sessions, policy_enforcer) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=True)
        created = await self._create_case(tenant, sessions, policy_enforcer, run_id)
        other = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        transport = ASGITransport(app=_cases_app(other, sessions, policy_enforcer))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            detail = await client.get(f"/api/v1/cases/{created['id']}")
            assert detail.status_code == 404
            listing = await client.get("/api/v1/cases")
            assert listing.json() == []


class TestRunEvidenceProjection:
    async def test_evidence_projects_canonical_outputs(self, tenant, sessions) -> None:
        run_id = uuid4()
        await _seed_run(
            sessions,
            tenant,
            run_id,
            terminal=True,
            output_values={
                "claims": ["cross-source/claim"],
                "conflicts": [],
                "unknowns": [],
                "verification": {"verification_ok": True, "exit_code": 0, "check_count": 1},
                "verified_claims": ["claim"],
                "failed_claims": [],
                "answer": {"status": "completed", "claims": ["cross-source/claim"]},
            },
        )
        transport = ASGITransport(app=_evidence_app(tenant, sessions))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{run_id}/evidence")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["run_id"] == str(run_id)
            assert body["run_status"] == "completed"
            assert body["answer_status"] == "completed"
            assert body["verification"] == {
                "verification_ok": True, "exit_code": 0, "check_count": 1,
            }
            assert [c["claim_ref"] for c in body["claims"]] == ["cross-source/claim"]
            # verify 节点的 verified_claims 用通用标记，不与 claim_ref 绑定——
            # 逐 claim verified 如实为 null（不虚构绑定）
            assert body["claims"][0]["verified"] is None
            assert body["verified_claims"] == ["claim"]
            assert body["failed_claims"] == []
            assert body["unknowns"] == []
            assert body["conflicts"] == []

    async def test_evidence_abstain_projection(self, tenant, sessions) -> None:
        run_id = uuid4()
        await _seed_run(
            sessions,
            tenant,
            run_id,
            terminal=True,
            output_values={
                "claims": [],
                "unknowns": ["数据中不存在该条目"],
                "verification": {"verification_ok": True, "exit_code": 0, "check_count": 0},
                "answer": {"status": "abstained", "claims": []},
            },
        )
        transport = ASGITransport(app=_evidence_app(tenant, sessions))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{run_id}/evidence")
            assert response.status_code == 200
            body = response.json()
            assert body["answer_status"] == "abstained"
            assert body["claims"] == []
            assert body["unknowns"] == ["数据中不存在该条目"]

    async def test_evidence_non_terminal_run_is_projected_not_rejected(
        self, tenant, sessions
    ) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=False, output_values={"findings": []})
        transport = ASGITransport(app=_evidence_app(tenant, sessions))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{run_id}/evidence")
            assert response.status_code == 200
            assert response.json()["run_status"] == "running"

    async def test_evidence_unknown_run_is_404(self, tenant, sessions) -> None:
        transport = ASGITransport(app=_evidence_app(tenant, sessions))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{uuid4()}/evidence")
            assert response.status_code == 404

    async def test_evidence_cross_tenant_run_is_404(self, tenant, sessions) -> None:
        run_id = uuid4()
        await _seed_run(sessions, tenant, run_id, terminal=True)
        other = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        transport = ASGITransport(app=_evidence_app(other, sessions))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{run_id}/evidence")
            assert response.status_code == 404
