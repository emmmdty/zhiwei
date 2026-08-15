"""S1-T3 RED：OPA client 传输、schema 严格校验、有界缓存与 fail closed。

client.py 只负责 OPA transport、revision/freshness 和有界缓存；授权语义在 Rego。
契约（冻结）：缺 allow/decision_id/revision/reason 拒绝；非 200/超时/连接失败拒绝；
未知 revision 拒绝；缓存 key 绑定完整规范化 input + revision，容量/TTL 明确；
OPA 不可用时不得回落到缓存 allow；fail-closed 决策不得伪造 decision_id。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from zhiwei.contracts.canonical import digest
from zhiwei.policy.client import OPAClient
from zhiwei.policy.input import PolicyInput

NOW = datetime(2026, 8, 15, 0, 0, 0, tzinfo=UTC)
BASE = "http://opa.test:8181"


def make_input_doc(**overrides) -> dict:
    """合法 UUID fixture，经 PolicyInput 规范化后的文档（client 以规范化文档为准）。"""
    doc = {
        "organization_id": "00000000-0000-0000-0000-000000000001",
        "workspace_id": None,
        "actor": {"principal_id": "00000000-0000-0000-0000-0000000000a1", "kind": "user", "roles": [
            {"name": "org_owner", "scope": "org",
             "organization_id": "00000000-0000-0000-0000-000000000001", "workspace_id": None},
        ]},
        "effective_identity": None,
        "resource": {"type": "org", "id": "00000000-0000-0000-0000-0000000000b1", "version": "v1"},
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
    return PolicyInput.model_validate(doc).model_dump(mode="json")


INPUT_A = make_input_doc()
INPUT_B = make_input_doc(purpose="compliance")  # 仅 purpose 不同 → 不同缓存 key


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
    async def test_opa_down_same_input_served_only_within_bounds(self) -> None:
        """有界缓存契约（PERMISSIONS.md:85 不能使用缓存 allow 超过明确 TTL/版本）：

        同 input 的 allow 只能在 TTL+revision 界内复用（不再联系 OPA）；一旦
        需要求值（TTL 过期 / revision 变化 / 其他 input），OPA 不可用即拒绝，
        绝不回落到任何缓存 allow。
        """
        fail: list[httpx.ConnectError | None] = [None]
        _transport, requests, _ = make_transport()

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
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
        # 同 input、TTL 内、同 revision：有界复用（不再发请求）
        d_same = await client.evaluate(INPUT_A)
        assert d_same.allow is True and d_same.decision_id == d1.decision_id
        assert len(requests) == 1, "TTL 界内的同 input 复用不得再联系 OPA"
        # 需要求值的请求（不同 input）：必须拒绝，不得回落到任何缓存 allow
        d2 = await client.evaluate(INPUT_B)
        assert d2.allow is False
        assert d2.reason == "opa_unavailable" and d2.decision_id is None

    @pytest.mark.asyncio
    async def test_recovery_after_outage_repopulates_cache(self) -> None:
        fail: list[httpx.ConnectError | None] = [None]
        _transport, _, _ = make_transport()

        def handler(request: httpx.Request) -> httpx.Response:
            if fail[0] is not None:
                raise fail[0]
            return httpx.Response(200, json=ok_response(decision_id="d2"), request=request)

        client = OPAClient(
            BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        )
        d1 = await client.evaluate(INPUT_A)
        assert d1.allow is True
        fail[0] = httpx.ConnectError("down")
        await client.evaluate(INPUT_B)  # 故障期请求拒绝
        fail[0] = None
        d3 = await client.evaluate(INPUT_B)  # 恢复后重新求值成功
        assert d3.allow is True and d3.decision_id == "d2"
        d4 = await client.evaluate(INPUT_B)  # 缓存恢复工作
        assert d4.decision_id == "d2"

    @pytest.mark.asyncio
    async def test_unknown_top_level_key_rejected_before_send(self) -> None:
        """client 顶层键守卫：未声明字段（含 secret 形状）不得进入 OPA decision log。"""
        client, requests, _ = client_with(ok_response())
        d = await client.evaluate({**INPUT_A, "access_token": "s3cr3t"})
        assert d.allow is False
        assert d.reason == "opa_input_invalid"
        assert len(requests) == 0, "未知顶层键必须在发送前拒绝"

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


class TestFullSchemaValidation:
    @pytest.mark.asyncio
    async def test_nested_extra_rejected_before_send(self) -> None:
        """client 必须校验完整 PolicyInput schema（不只顶层键）：嵌套 extra
        （secret 形状）在发送前拒绝；sentinel 不得进入 request body、
        input_digest、reason 或任何决策字段。
        """
        sentinel = "s3cr3t-nested"
        cases = [
            {"actor": {"access_token": sentinel}},
            {"resource": {"credential": {"password": sentinel}}},
            {"context": {"password": sentinel}},
            {"actor": {"roles": [{"api_key": sentinel}]}},
            {"delegation": [{"granted_by_principal_id": "00000000-0000-0000-0000-0000000000c1",
                             "scope": "org.manage", "expires_at": "2026-08-15T01:00:00Z",
                             "token": sentinel}]},
        ]
        for patch in cases:
            client, requests, _ = client_with(ok_response())
            doc = {**INPUT_A}
            for key, value in patch.items():
                if key == "delegation":
                    doc[key] = value
                elif key == "actor" and "roles" in value:
                    # 浅拷贝下 doc["actor"] 与 INPUT_A["actor"] 是同一对象：
                    # 必须换新 dict，不能原位改 roles（会污染共享 fixture）
                    actor = {**doc["actor"]}
                    actor["roles"] = [{**doc["actor"]["roles"][0], **value["roles"][0]}]
                    doc["actor"] = actor
                else:
                    doc[key] = {**doc[key], **value}
            d = await client.evaluate(doc)
            assert d.allow is False
            assert d.reason == "opa_input_invalid"
            assert d.input_digest is None, "被拒文档不得进入 digest"
            assert d.decision_id is None and d.revision is None
            assert sentinel not in d.reason
            assert sentinel not in repr(d)
            assert len(requests) == 0, "嵌套 extra 必须在发送前拒绝"
            await client.aclose()

    @pytest.mark.asyncio
    async def test_schema_invalid_but_recoverable_after_fix(self) -> None:
        # 拒绝后 client 仍可继续服务合法请求（校验失败不污染内部状态）
        client, requests, _ = client_with(ok_response())
        bad = {**INPUT_A, "context": {**INPUT_A["context"], "password": "s3cr3t"}}
        d1 = await client.evaluate(bad)
        assert d1.allow is False
        d2 = await client.evaluate(INPUT_A)
        assert d2.allow is True and len(requests) == 1
        await client.aclose()

    @pytest.mark.asyncio
    async def test_resource_binding_required_before_send(self) -> None:
        client, requests, _ = client_with(ok_response())
        missing_id = {**INPUT_A, "resource": {"type": "org", "version": "v1"}}
        d1 = await client.evaluate(missing_id)
        assert d1.allow is False and d1.reason == "opa_input_invalid"
        empty_version = {**INPUT_A, "resource": {"type": "org",
                                                 "id": "00000000-0000-0000-0000-0000000000b1",
                                                 "version": ""}}
        d2 = await client.evaluate(empty_version)
        assert d2.allow is False and d2.reason == "opa_input_invalid"
        assert len(requests) == 0, "resource 绑定缺失必须在发送前拒绝"
        await client.aclose()


class TestConcurrentRevisionFencing:
    @pytest.mark.asyncio
    async def test_old_inflight_allow_cannot_regress_revision(self) -> None:
        """并发 revision fencing（独立验收反例）：

        old 请求在途挂起；new 请求先返回 rev-new deny；释放 old 后其
        rev-old allow 必须被丢弃（fail closed）——current_revision 不得回退
        到 rev-old，old allow 不得进入缓存，后续相同 old input 不得获得旧
        allow（必须重新求值）。
        """
        old_started = asyncio.Event()
        release_old = asyncio.Event()
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                old_started.set()
                await release_old.wait()
                return httpx.Response(
                    200, json=ok_response(allow=True, decision_id="d-old", revision="rev-old"),
                    request=request,
                )
            return httpx.Response(
                200, json=ok_response(allow=False, decision_id="d-new", revision="rev-new"),
                request=request,
            )

        client = OPAClient(
            BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
        )
        old_task = asyncio.create_task(client.evaluate(INPUT_A))
        await old_started.wait()
        d_new = await client.evaluate(INPUT_B)
        assert d_new.allow is False and d_new.revision == "rev-new"
        assert client.current_revision == "rev-new"
        release_old.set()
        d_old = await old_task
        assert d_old.allow is False, "旧在途 rev-old allow 必须被丢弃（fail closed）"
        assert d_old.decision_id is None and d_old.revision is None
        assert d_old.reason == "opa_stale_response"
        assert client.current_revision == "rev-new", "current_revision 不得回退到 rev-old"
        d_again = await client.evaluate(INPUT_A)
        assert d_again.allow is False and d_again.revision == "rev-new"
        assert calls["n"] == 3, "旧 allow 不得进入缓存：相同 old input 必须重新求值"
        d_hit = await client.evaluate(INPUT_A)
        assert calls["n"] == 3, "重求值结果进入缓存后可复用"
        assert d_hit.allow is False
        await client.aclose()

    @pytest.mark.asyncio
    async def test_newer_response_applied_after_old_still_wins(self) -> None:
        """自然顺序：old 响应先采纳，new 响应后到仍要覆盖为 rev-new。"""
        first_release = asyncio.Event()
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            body = json.loads(request.content)["input"]
            if body["purpose"] == "general":
                first_release.set()
                return httpx.Response(
                    200, json=ok_response(allow=True, decision_id="d-old", revision="rev-old"),
                    request=request,
                )
            return httpx.Response(
                200, json=ok_response(allow=False, decision_id="d-new", revision="rev-new"),
                request=request,
            )

        client = OPAClient(
            BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
        )
        d_old = await client.evaluate(INPUT_A)
        assert d_old.allow is True and d_old.revision == "rev-old"
        d_new = await client.evaluate(INPUT_B)
        assert d_new.allow is False and d_new.revision == "rev-new"
        assert client.current_revision == "rev-new"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_same_revision_concurrent_responses_both_cached(self) -> None:
        # 同一 revision 的两个并发响应：不得误判为 stale（缓存同 revision 决策）
        started = asyncio.Event()
        release = asyncio.Event()
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                started.set()
                await release.wait()
                return httpx.Response(
                    200, json=ok_response(allow=True, decision_id="d-old", revision="rev-1"),
                    request=request,
                )
            return httpx.Response(
                200, json=ok_response(allow=False, decision_id="d-new", revision="rev-1"),
                request=request,
            )

        client = OPAClient(
            BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
        )
        old_task = asyncio.create_task(client.evaluate(INPUT_A))
        await started.wait()
        d_new = await client.evaluate(INPUT_B)
        release.set()
        d_old = await old_task
        assert d_old.allow is True and d_old.revision == "rev-1"
        assert d_new.allow is False and d_new.revision == "rev-1"
        assert client.current_revision == "rev-1"
        assert d_old.decision_id == "d-old", "同 revision 在途响应不是 stale，应被采纳"
        await client.aclose()
