"""S1-T3 RED：OPA client 传输、schema 严格校验、有界缓存与 fail closed。

client.py 只负责 OPA transport、revision/freshness 和有界缓存；授权语义在 Rego。
契约（冻结）：缺 allow/decision_id/revision/reason 拒绝；非 200/超时/连接失败拒绝；
未知 revision 拒绝；缓存 key 绑定完整规范化 input + revision，容量/TTL 明确；
OPA 不可用时不得回落到缓存 allow；fail-closed 决策不得伪造 decision_id。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from zhiwei.contracts.canonical import digest
from zhiwei.policy.client import OPAClient

NOW = datetime(2026, 8, 15, 0, 0, 0, tzinfo=UTC)
BASE = "http://opa.test:8181"

INPUT_A = {
    "organization_id": "00000000-0000-0000-0000-000000000001",
    "workspace_id": None,
    "actor": {"principal_id": "u1", "kind": "user", "roles": [
        {"name": "org_owner", "scope": "org",
         "organization_id": "00000000-0000-0000-0000-000000000001", "workspace_id": None},
    ]},
    "resource": {"type": "org", "id": "r1", "version": "v1"},
    "action": "manage",
    "purpose": "general",
    "classification": None,
    "risk": None,
    "delegation": [],
    "resource_context": {"owner_principal_id": "u1", "requester_principal_id": None,
                         "modifier_principal_ids": [], "agent_identity_principal_id": None,
                         "last_content_author_principal_id": None,
                         "publisher_principal_id": None, "publisher_roles": []},
    "context": {"now": "2026-08-15T00:00:00Z", "classification_ceiling": None,
                "requires_delegation": False},
}

INPUT_B = {**INPUT_A, "purpose": "compliance"}  # 仅 purpose 不同 → 不同缓存 key


def ok_response(allow: bool = True, reason: str | None = None, decision_id: str = "d1",
                revision: str = "rev-1") -> dict:
    if reason is None:
        reason = "allowed:matrix" if allow else "default_deny:no_rule_matched"
    return {
        "decision_id": decision_id,
        "result": {"allow": allow, "reason": reason},
        "provenance": {"version": "1.19.0", "bundles": {"/bundle.tar.gz": {"revision": revision}}},
    }


def make_transport(*responses: dict, status: int = 200, fail: Exception | None = None):
    """MockTransport：依次返回给定响应（可空以便无响应时抛 fail），并记录请求。"""
    requests: list[httpx.Request] = []
    call = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        call["n"] += 1
        if fail is not None:
            raise fail
        if not responses:
            return httpx.Response(status, json={}, request=request)
        idx = min(call["n"] - 1, len(responses) - 1)
        return httpx.Response(status, json=responses[idx], request=request)

    return httpx.MockTransport(handler), requests, call


def client_with(*responses: dict, status: int = 200, fail: Exception | None = None,
                cache_maxsize: int = 256, cache_ttl_seconds: float = 30.0,
                clock=None, timeout: float = 5.0) -> tuple[OPAClient, list, dict]:
    transport, requests, call = make_transport(*responses, status=status, fail=fail)
    client = OPAClient(
        BASE,
        http_client=httpx.AsyncClient(transport=transport, timeout=timeout),
        cache_maxsize=cache_maxsize,
        cache_ttl_seconds=cache_ttl_seconds,
        clock=clock or (lambda: NOW),
    )
    return client, requests, call


class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_allow_decision_fields(self) -> None:
        client, _, _ = client_with(ok_response())
        d = await client.evaluate(INPUT_A)
        assert d.allow is True
        assert d.decision_id == "d1"
        assert d.revision == "rev-1"
        assert d.reason == "allowed:matrix"
        assert d.evaluated_at == NOW
        assert d.input_digest == digest(INPUT_A)

    @pytest.mark.asyncio
    async def test_deny_decision_preserves_metadata(self) -> None:
        client, _, _ = client_with(ok_response(allow=False, reason="sod_deny:self_review"))
        d = await client.evaluate(INPUT_A)
        assert d.allow is False
        assert d.decision_id == "d1"
        assert d.revision == "rev-1"
        assert d.reason == "sod_deny:self_review"

    @pytest.mark.asyncio
    async def test_request_hits_decision_path_with_input(self) -> None:
        client, requests, _ = client_with(ok_response())
        await client.evaluate(INPUT_A)
        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert str(requests[0].url).endswith("/v1/data/zhiwei/authz?provenance=true")
        body = json.loads(requests[0].content)
        assert body == {"input": INPUT_A}

    @pytest.mark.asyncio
    async def test_current_revision_tracks_server(self) -> None:
        client, _, _ = client_with(ok_response(revision="rev-1"))
        assert client.current_revision is None
        await client.evaluate(INPUT_A)
        assert client.current_revision == "rev-1"


class TestMalformedResponseFailsClosed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [
        {},  # 无 result
        {"decision_id": "d1"},  # 无 result
        {"decision_id": "d1", "result": {}},  # 缺 allow
        {"decision_id": "d1", "result": {"allow": "true", "reason": "x"}},  # allow 非布尔
        {"decision_id": "d1", "result": {"allow": 1, "reason": "x"}},
        {"decision_id": "d1", "result": {"allow": None, "reason": "x"}},
        {"decision_id": "d1", "result": {"allow": True}},  # 缺 reason
        {"decision_id": "d1", "result": {"allow": True, "reason": ""}},  # 空 reason
        {"result": {"allow": True, "reason": "x"}},  # 缺 decision_id
        {"decision_id": "", "result": {"allow": True, "reason": "x"}},  # 空 decision_id
        {"decision_id": "d1", "result": {"allow": True, "reason": "x"}},  # 缺 provenance
        {"decision_id": "d1", "result": {"allow": True, "reason": "x"},
         "provenance": {"bundles": {}}},  # 无 bundle
        {"decision_id": "d1", "result": {"allow": True, "reason": "x"},
         "provenance": {"bundles": {"a": {"revision": "r1"}, "b": {"revision": "r2"}}}},  # 两个 bundle
        {"decision_id": "d1", "result": {"allow": True, "reason": "x"},
         "provenance": {"bundles": {"a": {}}}},  # revision 缺失
        {"decision_id": "d1", "result": {"allow": True, "reason": "x"},
         "provenance": {"bundles": {"a": {"revision": ""}}}},  # 空 revision
    ], ids=[
        "empty", "no-result", "missing-allow", "allow-string", "allow-int", "allow-null",
        "missing-reason", "empty-reason", "missing-decision-id", "empty-decision-id",
        "missing-provenance", "no-bundle", "two-bundles", "missing-revision", "empty-revision",
    ])
    async def test_malformed_rejected(self, body: dict) -> None:
        client, _, _ = client_with(body)
        d = await client.evaluate(INPUT_A)
        assert d.allow is False
        assert d.decision_id is None, "畸形响应不得带伪造 decision_id"
        assert d.revision is None
        assert d.reason.startswith("opa_malformed_response")

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self) -> None:
        _transport, _, _ = make_transport()
        _calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            _calls["n"] += 1
            return httpx.Response(200, content=b"{not json", request=request)

        client = OPAClient(
            BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        )
        d = await client.evaluate(INPUT_A)
        assert d.allow is False and d.decision_id is None
        assert d.reason.startswith("opa_malformed_response")
        assert _calls["n"] == 1


class TestTransportFailsClosed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    async def test_non_200_rejected(self, status: int) -> None:
        client, _, _ = client_with(status=status)
        d = await client.evaluate(INPUT_A)
        assert d.allow is False
        assert d.decision_id is None and d.revision is None
        assert d.reason == f"opa_http_error:{status}"

    @pytest.mark.asyncio
    async def test_connection_error_rejected(self) -> None:
        client, _, _ = client_with(fail=httpx.ConnectError("refused"))
        d = await client.evaluate(INPUT_A)
        assert d.allow is False and d.decision_id is None
        assert d.reason == "opa_unavailable"

    @pytest.mark.asyncio
    async def test_timeout_rejected(self) -> None:
        client, _, _ = client_with(fail=httpx.TimeoutException("slow"))
        d = await client.evaluate(INPUT_A)
        assert d.allow is False and d.decision_id is None
        assert d.reason == "opa_unavailable"

    @pytest.mark.asyncio
    async def test_any_http_error_rejected(self) -> None:
        client, _, _ = client_with(fail=httpx.ReadError("boom"))
        d = await client.evaluate(INPUT_A)
        assert d.allow is False and d.decision_id is None
        assert d.reason == "opa_unavailable"

    @pytest.mark.asyncio
    async def test_http500_is_not_unavailable_reason(self) -> None:
        # 5xx 与服务不可用分开记录（审计需要区分），但都 fail closed
        client, _, _ = client_with(status=500)
        d = await client.evaluate(INPUT_A)
        assert d.allow is False and d.reason == "opa_http_error:500"


class TestBoundedCache:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_transport(self) -> None:
        client, requests, _ = client_with(ok_response())
        d1 = await client.evaluate(INPUT_A)
        d2 = await client.evaluate(INPUT_A)
        assert len(requests) == 1
        assert d1.decision_id == d2.decision_id  # 同一决策对象被复用

    @pytest.mark.asyncio
    async def test_cache_key_includes_full_input(self) -> None:
        # purpose 不同 → 缓存 key 不同（MC-8：key 绑定完整规范化 input）
        client, _, call = client_with(ok_response(), ok_response(decision_id="d2"))
        await client.evaluate(INPUT_A)
        await client.evaluate(INPUT_B)
        assert call["n"] == 2

    @pytest.mark.asyncio
    async def test_cache_key_is_canonical_json(self) -> None:
        # 键序不同的等价 input 命中同一缓存条目（canonical_json RFC 8785）
        client, _, call = client_with(ok_response())
        reordered = {k: INPUT_A[k] for k in reversed(list(INPUT_A))}
        await client.evaluate(INPUT_A)
        await client.evaluate(reordered)
        assert call["n"] == 1

    @pytest.mark.asyncio
    async def test_ttl_expiry_reevaluates(self) -> None:
        now = [NOW]
        client, _, call = client_with(ok_response(), ok_response(decision_id="d2"),
                                      clock=lambda: now[0])
        d1 = await client.evaluate(INPUT_A)
        now[0] = NOW + timedelta(seconds=31)
        d2 = await client.evaluate(INPUT_A)
        assert call["n"] == 2, "TTL 过期后必须重新求值，不得命中缓存 allow"
        assert d2.decision_id == "d2" and d1.decision_id == "d1"

    @pytest.mark.asyncio
    async def test_revision_change_invalidates_all_entries(self) -> None:
        client, _, call = client_with(
            ok_response(decision_id="d1", revision="rev-1"),
            ok_response(decision_id="d2", revision="rev-2"),
            ok_response(decision_id="d3", revision="rev-2"),
        )
        await client.evaluate(INPUT_A)  # rev-1 allow 入缓存
        d2 = await client.evaluate(INPUT_B)  # 感知 rev-2 → 清空缓存
        assert d2.revision == "rev-2"
        d3 = await client.evaluate(INPUT_A)  # 旧 rev-1 条目不得继续服务
        assert d3.revision == "rev-2" and d3.decision_id == "d3"
        assert call["n"] == 3, "缓存不得跨 revision"

    @pytest.mark.asyncio
    async def test_opa_down_never_falls_back_to_cached_allow(self) -> None:
        fail: list[httpx.ConnectError | None] = [None]
        _transport, _, _ = make_transport()

        def handler(request: httpx.Request) -> httpx.Response:
            if fail[0] is not None:
                raise fail[0]
            return httpx.Response(200, json=ok_response(), request=request)

        client = OPAClient(
            BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        )
        d1 = await client.evaluate(INPUT_A)
        assert d1.allow is True
        fail[0] = httpx.ConnectError("down")
        d2 = await client.evaluate(INPUT_B)  # 需要求值的请求：不得回落到任何缓存 allow
        assert d2.allow is False
        assert d2.reason == "opa_unavailable" and d2.decision_id is None

    @pytest.mark.asyncio
    async def test_cache_never_serves_beyond_ttl_even_when_denied(self) -> None:
        # 缓存键含 context.now：不同时刻的等价请求必然重新求值（时间维参与决策）
        now = [NOW]
        client, _, call = client_with(ok_response(), ok_response(decision_id="d2"),
                                      clock=lambda: now[0])
        first = {**INPUT_A, "context": {**INPUT_A["context"], "now": NOW.isoformat().replace("+00:00", "Z")}}
        second = {**INPUT_A, "context": {**INPUT_A["context"], "now": (NOW + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")}}
        await client.evaluate(first)
        await client.evaluate(second)
        assert call["n"] == 2

    @pytest.mark.asyncio
    async def test_cache_capacity_bounded_lru(self) -> None:
        client, _, call = client_with(ok_response(), ok_response(), ok_response(),
                                      ok_response(decision_id="d4"), cache_maxsize=2)
        await client.evaluate(INPUT_A)
        await client.evaluate(INPUT_B)
        input_c = {**INPUT_A, "purpose": "security"}
        await client.evaluate(input_c)  # 驱逐 A
        await client.evaluate(INPUT_A)  # A 已驱逐 → 重新求值
        assert call["n"] == 4, "缓存容量必须有界（LRU）"
