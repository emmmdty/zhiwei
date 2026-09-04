"""S1-T3 RED：PEP helper（enforcement.py）编排与默认拒绝。

enforcement.py 只做 PEP 编排、默认拒绝和结果映射；不实现授权语义（Rego 唯一事实）。
契约（冻结）：authorize 永不抛异常；非法 input 在边界转为 deny（含未知枚举）；
每次调用都重新求值（不存在「复用已存决策」的旁路）；fail-closed 决策带结构化 reason；
Decision 携带 T4 audit 所需的 decision id/revision/reason/input_digest。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx2 as httpx
import pytest

from zhiwei.policy.client import OPAClient
from zhiwei.policy.enforcement import PolicyEnforcer

NOW = datetime(2026, 8, 15, 0, 0, 0, tzinfo=UTC)
ORG = "00000000-0000-0000-0000-000000000001"


def ok_response(allow: bool = True, decision_id: str = "d1", revision: str = "rev-1",
                reason: str | None = None) -> dict:
    if reason is None:
        reason = "allowed:matrix" if allow else "default_deny:no_rule_matched"
    return {
        "decision_id": decision_id,
        "result": {"allow": allow, "reason": reason},
        "provenance": {"version": "1.19.0", "bundles": {"/bundle.tar.gz": {"revision": revision}}},
    }


def enforcer_with(*responses: dict, fail: Exception | None = None,
                  status: int = 200, fail_opaque: bool = False) -> tuple[PolicyEnforcer, dict]:
    """构造 enforcer；call 记录实际发给 OPA 的请求数。"""
    call = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call["n"] += 1
        if fail is not None:
            raise fail
        if fail_opaque:
            raise RuntimeError("unexpected")
        if not responses:
            return httpx.Response(status, json={}, request=request)
        return httpx.Response(status, json=responses[min(call["n"] - 1, len(responses) - 1)],
                              request=request)

    client = OPAClient(
        "http://opa.test:8181",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        clock=lambda: NOW,
    )
    return PolicyEnforcer(client), call


U1 = "00000000-0000-0000-0000-0000000000a1"
R1 = "00000000-0000-0000-0000-0000000000b1"


def valid_input_doc(**overrides) -> dict:
    doc = {
        "organization_id": ORG,
        "workspace_id": None,
        "actor": {"principal_id": U1, "kind": "user", "roles": [
            {"name": "org_owner", "scope": "org", "organization_id": ORG, "workspace_id": None},
        ]},
        "resource": {"type": "org", "id": R1, "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": None,
        "risk": None,
        "delegation": [],
        "resource_context": {},
        "context": {"now": "2026-08-15T00:00:00Z", "classification_ceiling": None,
                    "requires_delegation": False},
    }
    doc.update(overrides)
    return doc


class TestAuthorize:
    @pytest.mark.asyncio
    async def test_authorize_returns_decision(self) -> None:
        enforcer, _ = enforcer_with(ok_response())
        d = await enforcer.authorize(valid_input_doc())
        assert d.allow is True
        assert d.decision_id == "d1" and d.revision == "rev-1"

    @pytest.mark.asyncio
    async def test_authorize_accepts_typed_input(self) -> None:
        from zhiwei.policy.input import PolicyInput

        enforcer, _ = enforcer_with(ok_response())
        typed = PolicyInput.model_validate(valid_input_doc())
        d = await enforcer.authorize(typed)
        assert d.allow is True and d.input_digest is not None

    @pytest.mark.asyncio
    async def test_authorize_never_raises_on_transport_failure(self) -> None:
        enforcer, _ = enforcer_with(fail=httpx.ConnectError("down"))
        d = await enforcer.authorize(valid_input_doc())
        assert d.allow is False and d.reason == "opa_unavailable"

    @pytest.mark.asyncio
    async def test_authorize_never_raises_on_opaque_internal_error(self) -> None:
        enforcer, _ = enforcer_with(fail_opaque=True)
        d = await enforcer.authorize(valid_input_doc())
        assert d.allow is False
        assert d.decision_id is None and d.revision is None
        assert d.reason == "enforcement_internal_error"


class TestBoundaryDeny:
    @pytest.mark.asyncio
    async def test_unknown_role_denied_at_boundary_without_opa_call(self) -> None:
        enforcer, call = enforcer_with(ok_response())
        doc = valid_input_doc()
        doc["actor"]["roles"][0]["name"] = "superuser"
        d = await enforcer.authorize(doc)
        assert d.allow is False
        assert d.reason == "policy_input_invalid"
        assert call["n"] == 0, "未知枚举必须在边界拒绝，不进入 OPA"

    @pytest.mark.asyncio
    async def test_unknown_resource_action_denied_at_boundary(self) -> None:
        enforcer, call = enforcer_with(ok_response())
        doc = valid_input_doc()
        doc["resource"]["type"] = "wat"
        d = await enforcer.authorize(doc)
        assert d.allow is False and d.reason == "policy_input_invalid"
        assert call["n"] == 0

        doc = valid_input_doc()
        doc["action"] = "publsh"
        d = await enforcer.authorize(doc)
        assert d.allow is False and d.reason == "policy_input_invalid"
        assert call["n"] == 0

    @pytest.mark.asyncio
    async def test_missing_purpose_denied_at_boundary(self) -> None:
        enforcer, call = enforcer_with(ok_response())
        doc = valid_input_doc()
        del doc["purpose"]
        d = await enforcer.authorize(doc)
        assert d.allow is False and d.reason == "policy_input_invalid"
        assert call["n"] == 0

    @pytest.mark.asyncio
    async def test_extra_fields_denied_at_boundary(self) -> None:
        enforcer, call = enforcer_with(ok_response())
        doc = valid_input_doc()
        doc["access_token"] = "s3cr3t"
        d = await enforcer.authorize(doc)
        assert d.allow is False and d.reason == "policy_input_invalid"
        assert call["n"] == 0

    @pytest.mark.asyncio
    async def test_missing_sod_evidence_denied_at_boundary(self) -> None:
        enforcer, call = enforcer_with(ok_response())
        doc = valid_input_doc(resource={"type": "agent_publish", "id": R1, "version": "v1"},
                              action="review_publish")
        d = await enforcer.authorize(doc)
        assert d.allow is False and d.reason == "policy_input_invalid"
        assert call["n"] == 0

    @pytest.mark.asyncio
    async def test_agent_without_effective_identity_denied_at_boundary(self) -> None:
        # PERMISSIONS.md:9-10：agent 执行必须记录有效主体；缺失会让 Rego 的
        # via_effective SoD 规则全部失效，边界直接拒绝
        enforcer, call = enforcer_with(ok_response())
        doc = valid_input_doc(
            actor={"principal_id": "00000000-0000-0000-0000-0000000000a2", "kind": "agent_identity",
                   "roles": [{"name": "workspace_admin", "scope": "workspace",
                              "organization_id": ORG, "workspace_id": "00000000-0000-0000-0000-0000000000b2"}]},
            resource={"type": "agent_publish", "id": R1, "version": "v1"},
            action="review_publish",
            resource_context={"last_content_author_principal_id": U1},
            workspace_id="00000000-0000-0000-0000-0000000000b2",
        )
        d = await enforcer.authorize(doc)
        assert d.allow is False and d.reason == "policy_input_invalid"
        assert call["n"] == 0


class TestFreshnessContract:
    @pytest.mark.asyncio
    async def test_every_authorize_reevaluates_through_client(self) -> None:
        # 不存在「已存决策复用」路径：策略更新后同一 input 的再次求值必然走 OPA
        enforcer, call = enforcer_with(ok_response(), ok_response(decision_id="d2"))
        d1 = await enforcer.authorize(valid_input_doc(purpose="general"))
        d2 = await enforcer.authorize(valid_input_doc(purpose="compliance"))
        assert call["n"] == 2
        assert d1.decision_id != d2.decision_id

    @pytest.mark.asyncio
    async def test_decision_carries_audit_metadata(self) -> None:
        enforcer, _ = enforcer_with(ok_response())
        d = await enforcer.authorize(valid_input_doc())
        assert d.decision_id and d.revision and d.reason and d.input_digest
        assert d.evaluated_at == NOW


class TestResourceBindingBoundary:
    @pytest.mark.asyncio
    async def test_resource_without_id_denied_at_boundary_without_opa_call(self) -> None:
        enforcer, call = enforcer_with(ok_response())
        doc = valid_input_doc()
        del doc["resource"]["id"]
        d = await enforcer.authorize(doc)
        assert d.allow is False and d.reason == "policy_input_invalid"
        assert call["n"] == 0, "resource.id 缺失必须在边界拒绝，不进入 OPA"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", [None, ""])
    async def test_resource_version_missing_or_empty_denied_at_boundary(self, version) -> None:
        enforcer, call = enforcer_with(ok_response())
        doc = valid_input_doc()
        doc["resource"]["version"] = version
        d = await enforcer.authorize(doc)
        assert d.allow is False and d.reason == "policy_input_invalid"
        assert call["n"] == 0, "resource.version 缺失/空必须在边界拒绝，不进入 OPA"


class TestNestedSecretBoundary:
    @pytest.mark.asyncio
    async def test_nested_secrets_denied_at_boundary_without_opa_call(self) -> None:
        # 嵌套 extra（secret 形状）在发送前拒绝；sentinel 不得出现在决策、
        # digest 或任何发送内容中
        sentinel = "s3cr3t-boundary"
        cases = [
            {"actor": {"access_token": sentinel}},
            {"resource": {"credential": {"password": sentinel}}},
            {"context": {"password": sentinel}},
        ]
        for patch in cases:
            enforcer, call = enforcer_with(ok_response())
            doc = valid_input_doc()
            for key, value in patch.items():
                doc[key] = {**doc[key], **value}
            d = await enforcer.authorize(doc)
            assert d.allow is False and d.reason == "policy_input_invalid"
            assert d.input_digest is None, "被拒文档不得进入 digest"
            assert sentinel not in d.reason
            assert call["n"] == 0, "嵌套 secret 必须在发送前拒绝"


class TestDenyHelper:
    def test_deny_helper_is_fail_closed(self) -> None:
        enforcer, _ = enforcer_with(ok_response())
        d = enforcer.deny("healthcheck_downgrade:public_health")
        assert d.allow is False
        assert d.decision_id is None and d.revision is None
        assert d.reason == "healthcheck_downgrade:public_health"
        assert d.input_digest is None
