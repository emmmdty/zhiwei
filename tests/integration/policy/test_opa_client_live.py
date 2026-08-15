"""S1-T3 RED：真实 OPA 容器端到端（默认运行，opa 服务必须已启动）。

与 tests/integration/identity 的约定一致：本文件要求 compose 的 opa 服务已在
127.0.0.1:8181 运行（`docker compose --profile identity up -d --wait opa`）；
服务不可达时测试直接失败（不是跳过）。容器生命周期场景（unavailable/stale/
policy-update）在 test_opa_sidecar_slow.py（@slow，真实 docker 编排）。
"""

from __future__ import annotations

import httpx
import pytest

from zhiwei.policy.client import OPAClient
from zhiwei.policy.enforcement import PolicyEnforcer

OPA_URL = "http://127.0.0.1:8181"
ORG = "00000000-0000-0000-0000-000000000001"


def _health() -> httpx.Response:
    return httpx.get(f"{OPA_URL}/health?bundles", timeout=5.0)


def _require_opa_healthy() -> None:
    try:
        resp = _health()
    except httpx.HTTPError as exc:  # pragma: no cover - 环境守卫失败路径
        raise RuntimeError(
            f"opa 服务不可达（{OPA_URL}）。先运行: "
            "docker compose -f deploy/compose/compose.test.yaml --profile identity up -d --wait opa"
        ) from exc
    assert resp.status_code == 200, f"/health?bundles 必须 200，实际 {resp.status_code}"


def _input_doc(*, role: str, resource: str, action: str, purpose: str = "general",
               classification=None, ceiling=None, resource_context=None,
               risk=None, actor_id: str = "u1") -> dict:
    return {
        "organization_id": ORG,
        "workspace_id": None,
        "actor": {"principal_id": actor_id, "kind": "user", "roles": [
            {"name": role, "scope": "org", "organization_id": ORG, "workspace_id": None},
        ]},
        "resource": {"type": resource, "id": "r1", "version": "v1"},
        "action": action,
        "purpose": purpose,
        "classification": classification,
        "risk": risk,
        "delegation": [],
        "resource_context": resource_context or {},
        "context": {"now": "2026-08-15T00:00:00Z", "classification_ceiling": ceiling,
                    "requires_delegation": False},
    }


@pytest.fixture(scope="module")
def enforcer() -> PolicyEnforcer:
    _require_opa_healthy()
    client = OPAClient(OPA_URL, cache_maxsize=64, cache_ttl_seconds=10.0)
    yield PolicyEnforcer(client)


class TestLiveDecisions:
    @pytest.mark.asyncio
    async def test_live_allow_decision_fields(self, enforcer: PolicyEnforcer) -> None:
        d = await enforcer.authorize(
            _input_doc(role="org_owner", resource="org", action="manage")
        )
        assert d.allow is True
        assert d.decision_id, "真实 OPA 决策必须带 decision_id"
        assert d.revision, "真实 OPA 决策必须带 bundle revision"
        assert d.reason, "决策必须带非空 reason"
        assert d.input_digest

    @pytest.mark.asyncio
    async def test_live_deny_decision_fields(self, enforcer: PolicyEnforcer) -> None:
        d = await enforcer.authorize(
            _input_doc(role="member", resource="org", action="manage")
        )
        assert d.allow is False
        assert d.decision_id and d.revision and d.reason

    @pytest.mark.asyncio
    async def test_live_unknown_role_denied_at_boundary(self, enforcer: PolicyEnforcer) -> None:
        d = await enforcer.authorize(
            _input_doc(role="superuser", resource="org", action="manage")
        )
        assert d.allow is False and d.reason == "policy_input_invalid"

    @pytest.mark.asyncio
    async def test_live_unknown_action_denied_at_boundary(self, enforcer: PolicyEnforcer) -> None:
        d = await enforcer.authorize(
            _input_doc(role="org_owner", resource="org", action="delete")
        )
        assert d.allow is False and d.reason == "policy_input_invalid"

    @pytest.mark.asyncio
    async def test_live_sod_review_denied(self, enforcer: PolicyEnforcer) -> None:
        d = await enforcer.authorize(_input_doc(
            role="workspace_admin",
            resource="agent_publish",
            action="review_publish",
            resource_context={"last_content_author_principal_id": "u1"},
        ))
        assert d.allow is False
        assert "sod_deny" in d.reason or "self_review" in d.reason

    @pytest.mark.asyncio
    async def test_live_health_bundles_ok(self) -> None:
        resp = _health()
        assert resp.status_code == 200


class TestLiveCache:
    @pytest.mark.asyncio
    async def test_live_cache_hit_reuses_decision_id(self, enforcer: PolicyEnforcer) -> None:
        d1 = await enforcer.authorize(
            _input_doc(role="auditor", resource="org", action="read_audit")
        )
        d2 = await enforcer.authorize(
            _input_doc(role="auditor", resource="org", action="read_audit")
        )
        assert d1.allow is True
        assert d1.decision_id == d2.decision_id, (
            "同一 input 应命中客户端缓存（同一决策对象），而不是再发请求拿新 decision_id"
        )

    @pytest.mark.asyncio
    async def test_live_different_input_not_served_from_cache(self, enforcer: PolicyEnforcer) -> None:
        d1 = await enforcer.authorize(
            _input_doc(role="auditor", resource="org", action="read_audit", purpose="general")
        )
        d2 = await enforcer.authorize(
            _input_doc(role="auditor", resource="org", action="read_audit", purpose="compliance")
        )
        assert d1.decision_id != d2.decision_id, "不同 input 不得共享缓存条目"
