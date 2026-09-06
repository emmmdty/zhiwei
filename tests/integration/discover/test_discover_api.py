"""S10-T4c integration：Discover triage / Case / gated action / Resolution 契约
（S8 收口补齐——docs/handoffs/s8-discover-case-action-e2e-exception.md 解锁条件）。

事实源：specs/s8-discover-actions.md §4/§6（Workbench journey：Feed/Triage →
Case → Approval/ActionReceipt → HumanResolution；启发式 score 不称 probability）、
S8 冻结域（src/zhiwei/discover/*：ActionManager SoD 复用 S2 ApprovalRequestManager）。

- Discover router（api/discover.py）：GET /api/v1/discover/feed 只读投影
  （score 机器字段逐字，无 probability 语义）；POST .../hypotheses/{id}/triage
  状态机迁移（fail closed：非法迁移 409、owner 写入）；POST
  .../hypotheses/{id}/cases 创建 S8 DiscoverCase（同 hypothesis 重复创建 409，
  刷新/重试不复制）；POST .../cases/{id}/actions 高风险动作提交即落
  pending_approval 并以 409 逐字拒绝执行（server-driven 门禁，不默认执行），
  审批经 ActionManager → S2 ApprovalRequestManager（SoD：requester 本人批准
  409）；POST .../cases/{id}/resolutions 记录 HumanResolution（case 终态后
  再记录 409）；
- mutation 全部走生产 policy 纵切（RUN_CASE_ARTIFACT × manage_visible_cases，
  policy 先于业务事务）；读路径 PEP（RUN_CASE_ARTIFACT × read）；
- 持久化（0018_discover）：discover_hypotheses/cases/actions/resolutions +
  hypothesis_events 台账，FORCE RLS，与 ORM 约束镜像。
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
from sqlalchemy import text

from zhiwei.discover.hypotheses import EvidenceTag, HypothesisKind, RiskHypothesis
from zhiwei.discover.signals import SignalSeverity
from zhiwei.identity.domain import ActorContext

pytestmark = pytest.mark.asyncio

ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

# api/discover.py 的 409 拒绝 detail（与 e2e discover-case-action.spec.ts 逐字一致）
APPROVAL_REFUSAL = "action requires human approval before execution"


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
    from zhiwei.persistence.database import create_database_engine, create_session_factory

    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant(sessions) -> Any:
    from zhiwei.persistence.repositories import TenantRepository
    from zhiwei.persistence.tenant import TenantContext, tenant_session

    context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        await repository.create_workspace(context.workspace_id, name="discover-api")
    return context


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


def _actor(context: Any) -> ActorContext:
    return ActorContext(
        principal_id=uuid4(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


def _discover_app(context: Any, sessions, policy_enforcer, actor_holder: dict[str, Any]) -> FastAPI:
    """每 app 独立 router 实例（ActionManager SoD 决策态随 router 存活）；
    actor_holder 让单个 app 在请求间切换 actor（SoD 二人反例的驱动点）。"""
    from zhiwei.api.discover import create_discover_router

    app = FastAPI()
    app.include_router(
        create_discover_router(
            actor_dependency=lambda: actor_holder["value"],
            sessions=sessions,
            policy_enforcer=policy_enforcer,
        )
    )
    return app


async def _seed_hypothesis(
    sessions,
    context: Any,
    *,
    status: str = "ready_for_triage",
    owner: str = "",
    score: float | None = 0.87,
    title: str = "Vendor X spend anomaly",
    supporting: int = 2,
    contradicting: int = 1,
) -> UUID:
    """经持久仓储直接注入 pipeline ingest 产物（Signal→Hypothesis 的落点）。

    Trigger→StartRun→detector 的 pipeline 执行不属本契约面（D0–D6 数据面断言
    由 eval suite 承担）——workbench 契约从 hypothesis 行开始。
    """
    from datetime import UTC, datetime

    from zhiwei.discover.pg_repository import PgDiscoverRepository

    from zhiwei.discover.hypotheses import HypothesisStatus
    from zhiwei.persistence.tenant import tenant_session
    now = datetime.now(UTC)
    evidence = (
        tuple(
            EvidenceTag(
                tag_id=uuid4(),
                kind=HypothesisKind.SUPPORTING,
                description=f"supporting evidence {index}",
                created_at=now,
            )
            for index in range(supporting)
        )
        + tuple(
            EvidenceTag(
                tag_id=uuid4(),
                kind=HypothesisKind.CONTRADICTING,
                description=f"contradicting evidence {index}",
                created_at=now,
            )
            for index in range(contradicting)
        )
    )
    hypothesis = RiskHypothesis(
        id=uuid4(),
        signal_id=uuid4(),
        program_version_id=uuid4(),
        detector_pack_id=uuid4(),
        detector_pack_version=1,
        kind=HypothesisKind.SUPPORTING,
        title=title,
        description="Vendor X spending increased 300% month over month",
        affected_entities=("vendor:vendor-x",),
        evidence_tags=evidence,
        status=HypothesisStatus(status),
        owner=owner,
        suggested_validation_actions=("Ask for corroborating evidence",),
        score=score,
        created_at=now,
        updated_at=now,
    )
    async with tenant_session(sessions, context) as session:
        repository = PgDiscoverRepository(session, context)
        await repository.ingest_hypothesis(
            hypothesis,
            severity=SignalSeverity.HIGH,
            dedup_key="fingerprint:vendor-x-spend:v1",
        )
    return hypothesis.id


async def _hypothesis_status(sessions, context: Any, hypothesis_id: UUID) -> str:
    from zhiwei.persistence.tenant import tenant_session

    async with tenant_session(sessions, context) as session:
        row = await session.execute(
            text(
                "SELECT status FROM discover_hypotheses WHERE id = :id"
            ),
            {"id": hypothesis_id},
        )
        return str(row.scalar_one())


class TestDiscoverFeed:
    async def test_feed_projects_hypothesis_rows_with_machine_fields(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/discover/feed")
            assert response.status_code == 200, response.text
            feed = response.json()
            assert len(feed) == 1
            row = feed[0]
            assert row["id"] == str(hypothesis_id)
            # 机器字段逐字：status/score/severity 都以域名词呈现（score 是启发式
            # 分值，绝不标注 probability——投影层无该语义字段）
            assert row["status"] == "ready_for_triage"
            assert row["score"] == 0.87
            assert row["severity"] == "high"
            assert row["supporting_count"] == 2
            assert row["contradicting_count"] == 1
            assert row["missing_count"] == 0
            assert row["dedup_key"] == "fingerprint:vendor-x-spend:v1"
            assert row["case_id"] is None
            assert row["freshness_hours"] >= 0
            assert "probab" not in json.dumps(feed).lower()

    async def test_feed_read_denied_is_403(self, tenant, sessions, policy_enforcer) -> None:
        await _seed_hypothesis(sessions, tenant)
        fake = FakeOPA()
        fake.deny = True
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        client_opa = OPAClient(
            "http://opa.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )
        actor_holder = {"value": _actor(tenant)}
        denied_app = _discover_app(tenant, sessions, PolicyEnforcer(client_opa), actor_holder)
        transport = ASGITransport(app=denied_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/discover/feed")
            assert response.status_code == 403, response.text
        await client_opa.aclose()

    async def test_feed_is_tenant_isolated(self, tenant, sessions, policy_enforcer) -> None:
        from zhiwei.persistence.repositories import TenantRepository
        from zhiwei.persistence.tenant import TenantContext, tenant_session

        other = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        async with tenant_session(sessions, other) as session:
            repository = TenantRepository(session, other)
            await repository.create_organization(other.organization_id, status="active")
            await repository.create_workspace(other.workspace_id, name="discover-other")
        await _seed_hypothesis(sessions, other)

        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/discover/feed")
            assert response.status_code == 200, response.text
            assert response.json() == []


class TestTriageTransitions:
    async def test_claim_transition_writes_status_owner_and_ledger(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        actor = _actor(tenant)
        actor_holder = {"value": actor}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/discover/hypotheses/{hypothesis_id}/triage",
                json={"status": "in_triage", "owner": str(actor.principal_id)},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "in_triage"
            assert body["owner"] == str(actor.principal_id)

        assert await _hypothesis_status(sessions, tenant, hypothesis_id) == "in_triage"
        # 台账：转移落 discover_hypothesis_events（轨迹不改写 detector output）
        from zhiwei.persistence.tenant import tenant_session

        async with tenant_session(sessions, tenant) as session:
            events = await session.execute(
                text(
                    "SELECT action, from_status, to_status FROM discover_hypothesis_events"
                    " WHERE hypothesis_id = :id"
                ),
                {"id": hypothesis_id},
            )
            rows = events.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "triage"
        assert rows[0][1] == "ready_for_triage"
        assert rows[0][2] == "in_triage"

    async def test_illegal_transition_is_409_fail_closed(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # ready_for_triage → accepted 跳过 triage 队列：状态机拒绝
            response = await client.post(
                f"/api/v1/discover/hypotheses/{hypothesis_id}/triage",
                json={"status": "accepted"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 409, response.text
        assert await _hypothesis_status(sessions, tenant, hypothesis_id) == "ready_for_triage"

    async def test_unknown_hypothesis_is_404(self, tenant, sessions, policy_enforcer) -> None:
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/discover/hypotheses/{uuid4()}/triage",
                json={"status": "in_triage"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 404, response.text

    async def test_triage_denied_by_policy_is_403_with_zero_writes(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        fake = FakeOPA()
        fake.deny = True
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        client_opa = OPAClient(
            "http://opa.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, PolicyEnforcer(client_opa), actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/discover/hypotheses/{hypothesis_id}/triage",
                json={"status": "in_triage"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 403, response.text
        assert await _hypothesis_status(sessions, tenant, hypothesis_id) == "ready_for_triage"
        await client_opa.aclose()


class TestCaseCreation:
    async def test_create_case_defaults_title_from_hypothesis(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        actor = _actor(tenant)
        actor_holder = {"value": actor}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                json={"description": "needs review"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["hypothesis_id"] == str(hypothesis_id)
            assert body["title"] == "Vendor X spend anomaly"
            assert body["status"] == "open"
            assert body["created_by"] == str(actor.principal_id)

            # 刷新/重试不复制：同 hypothesis 的重复创建 409（唯一键兜底）
            replay = await client.post(
                f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                json={"description": "needs review"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert replay.status_code == 409, replay.text

            # feed 投影出现 case 关联
            feed = (await client.get("/api/v1/discover/feed")).json()
            assert feed[0]["case_id"] == body["id"]

    async def test_unknown_hypothesis_case_is_404(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/discover/hypotheses/{uuid4()}/cases",
                json={},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 404, response.text

    async def test_case_creation_denied_by_policy_is_403(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        fake = FakeOPA()
        fake.deny = True
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        client_opa = OPAClient(
            "http://opa.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, PolicyEnforcer(client_opa), actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                json={},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 403, response.text
            feed = await client.get("/api/v1/discover/feed")
            assert feed.json()[0]["case_id"] is None
        await client_opa.aclose()


class TestGatedActionSubmission:
    async def test_submission_pends_approval_and_refuses_execution_verbatim(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        actor = _actor(tenant)
        actor_holder = {"value": actor}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            case_id = (
                await client.post(
                    f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                    json={},
                    headers={"Idempotency-Key": str(uuid4())},
                )
            ).json()["id"]

            submitted = await client.post(
                f"/api/v1/discover/cases/{case_id}/actions",
                json={
                    "action_type": "modify",
                    "tool_name": "vendor-payment-adjust",
                    "rationale": "Adjust vendor X payment terms",
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            # server-driven 门禁：request 已落账（pending_approval），执行被逐字拒绝
            assert submitted.status_code == 409, submitted.text
            assert submitted.json()["detail"] == APPROVAL_REFUSAL

            detail = (await client.get(f"/api/v1/discover/cases/{case_id}")).json()
            assert len(detail["actions"]) == 1
            action = detail["actions"][0]
            assert action["status"] == "pending_approval"
            assert action["action_type"] == "modify"
            assert action["requested_by"] == str(actor.principal_id)
            assert action["s2_decision_id"] is not None
            assert action["approved_by"] is None

            # 刷新/重试不复制：同内容重复提交 409（digest 唯一键兜底）
            replay = await client.post(
                f"/api/v1/discover/cases/{case_id}/actions",
                json={
                    "action_type": "modify",
                    "tool_name": "vendor-payment-adjust",
                    "rationale": "Adjust vendor X payment terms",
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert replay.status_code == 409, replay.text
            detail = (await client.get(f"/api/v1/discover/cases/{case_id}")).json()
            assert len(detail["actions"]) == 1

    async def test_unknown_case_action_is_404(self, tenant, sessions, policy_enforcer) -> None:
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/discover/cases/{uuid4()}/actions",
                json={
                    "action_type": "query",
                    "tool_name": "lookup",
                    "rationale": "check balance",
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 404, response.text

    async def test_action_submission_denied_by_policy_is_403(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        fake = FakeOPA()
        fake.deny = True
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        client_opa = OPAClient(
            "http://opa.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, PolicyEnforcer(client_opa), actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            case_id = (
                await _seed_case_via_allow_app(tenant, sessions, hypothesis_id)
            )
            response = await client.post(
                f"/api/v1/discover/cases/{case_id}/actions",
                json={
                    "action_type": "modify",
                    "tool_name": "vendor-payment-adjust",
                    "rationale": "Adjust vendor X payment terms",
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 403, response.text
        await client_opa.aclose()


class TestApproval:
    async def test_self_approval_is_refused_and_other_principal_approves(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        requester = _actor(tenant)
        approver = _actor(tenant)
        actor_holder = {"value": requester}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            case_id = (
                await client.post(
                    f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                    json={},
                    headers={"Idempotency-Key": str(uuid4())},
                )
            ).json()["id"]
            await client.post(
                f"/api/v1/discover/cases/{case_id}/actions",
                json={
                    "action_type": "delete",
                    "tool_name": "vendor-record-purge",
                    "rationale": "Purge duplicated vendor record",
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            action_id = (
                (await client.get(f"/api/v1/discover/cases/{case_id}")).json()["actions"][0]["id"]
            )

            # SoD（S2 ApprovalRequestManager）：requester 本人批准 → 409
            self_approved = await client.post(
                f"/api/v1/discover/actions/{action_id}/approve",
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert self_approved.status_code == 409, self_approved.text
            assert "different human principal" in self_approved.json()["detail"]

            # 他人批准 → approved（approved_by 消费 S2 决定）
            actor_holder["value"] = approver
            approved = await client.post(
                f"/api/v1/discover/actions/{action_id}/approve",
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert approved.status_code == 200, approved.text
            body = approved.json()
            assert body["status"] == "approved"
            assert body["approved_by"] == str(approver.principal_id)
            assert body["approval_timestamp"] is not None

            # 状态机：重复批准 409（刷新/重试不复制审批）
            actor_holder["value"] = approver
            replay = await client.post(
                f"/api/v1/discover/actions/{action_id}/approve",
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert replay.status_code == 409, replay.text

    async def test_unknown_action_is_404(self, tenant, sessions, policy_enforcer) -> None:
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/discover/actions/{uuid4()}/approve",
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 404, response.text


class TestHumanResolution:
    async def test_resolution_recorded_and_case_resolves(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        actor = _actor(tenant)
        actor_holder = {"value": actor}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            case_id = (
                await client.post(
                    f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                    json={},
                    headers={"Idempotency-Key": str(uuid4())},
                )
            ).json()["id"]

            recorded = await client.post(
                f"/api/v1/discover/cases/{case_id}/resolutions",
                json={
                    "kind": "accepted",
                    "rationale": "Confirmed with vendor ledger",
                    "notes": "evidence attached to case",
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert recorded.status_code == 201, recorded.text
            body = recorded.json()
            assert body["kind"] == "accepted"
            assert body["hypothesis_id"] == str(hypothesis_id)
            assert body["approved_by"] == str(actor.principal_id)
            assert body["resolved_by"] == str(actor.principal_id)

            detail = (await client.get(f"/api/v1/discover/cases/{case_id}")).json()
            assert detail["status"] == "resolved"
            assert len(detail["resolutions"]) == 1
            assert detail["resolutions"][0]["id"] == body["id"]

            # 刷新/重试不复制：已终态 case 的重复 resolution 409
            replay = await client.post(
                f"/api/v1/discover/cases/{case_id}/resolutions",
                json={"kind": "dismissed", "rationale": "changed mind"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert replay.status_code == 409, replay.text

    async def test_empty_rationale_is_422(self, tenant, sessions, policy_enforcer) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            case_id = (
                await client.post(
                    f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                    json={},
                    headers={"Idempotency-Key": str(uuid4())},
                )
            ).json()["id"]
            response = await client.post(
                f"/api/v1/discover/cases/{case_id}/resolutions",
                json={"kind": "accepted", "rationale": ""},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 422, response.text

    async def test_unknown_case_resolution_is_404(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/discover/cases/{uuid4()}/resolutions",
                json={"kind": "accepted", "rationale": "orphan"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 404, response.text


class TestCaseDetailRead:
    async def test_case_detail_is_tenant_isolated(
        self, tenant, sessions, policy_enforcer
    ) -> None:
        hypothesis_id = await _seed_hypothesis(sessions, tenant)
        actor_holder = {"value": _actor(tenant)}
        app = _discover_app(tenant, sessions, policy_enforcer, actor_holder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            case_id = (
                await client.post(
                    f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                    json={},
                    headers={"Idempotency-Key": str(uuid4())},
                )
            ).json()["id"]
            detail = await client.get(f"/api/v1/discover/cases/{case_id}")
            assert detail.status_code == 200
            unknown = await client.get(f"/api/v1/discover/cases/{uuid4()}")
            # 未知与跨租户 case 同语义 404（防枚举）
            assert unknown.status_code == 404


async def _seed_case_via_allow_app(
    tenant: Any, sessions, hypothesis_id: UUID
) -> UUID:
    """用 allow 形态的独立 app 创建 case（deny 形态测试的前置数据）。"""
    actor_holder = {"value": _actor(tenant)}
    fake = FakeOPA()
    from zhiwei.policy.client import OPAClient
    from zhiwei.policy.enforcement import PolicyEnforcer

    client_opa = OPAClient(
        "http://opa.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    app = _discover_app(tenant, sessions, PolicyEnforcer(client_opa), actor_holder)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        case_id = (
            await client.post(
                f"/api/v1/discover/hypotheses/{hypothesis_id}/cases",
                json={},
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).json()["id"]
    await client_opa.aclose()
    return UUID(case_id)
