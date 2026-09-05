"""S3 Security: pre-send classification gate (ADR-011 §4).

数据门禁设在「数据是否离开信任边界」：context 实际分类超过 endpoint 的
classification_ceiling 时，CaptureTransport 的 gate 在 inner transport 之前拒绝，
请求在结构上不可能出网。全程 mock transport，不发真实请求。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2 as httpx
import pytest

from zhiwei.models.contracts import ClassificationCeiling, EndpointProfile
from zhiwei.models.presend import (
    CaptureTransport,
    ClassificationViolation,
    PreSendRejected,
    classification_gate,
)
from zhiwei.models.profiles import EndpointRegistry

# loopback 端口 9（discard）只作为 URL 标识使用，测试不真正连接。
_EGRESS_URL = "http://127.0.0.1:9/v1/chat/completions"


def _external_endpoint(ceiling: str) -> EndpointProfile:
    return EndpointProfile(
        id="external-gated",
        base_url="https://external.example.com/v1",
        credential_env="EXT_API_KEY",
        allowed_paths=("/chat/completions",),
        classification_ceiling=ClassificationCeiling(ceiling.lower()),
    )


def _post(transport: CaptureTransport, payload: dict[str, Any]) -> BaseException | None:
    """发起一次请求并返回捕获到的异常（无异常返回 None）。

    返回异常而非直接 raises：让每个用例能同时断言「异常类型正确」与
    「inner transport 零调用」两件事，不用各写一遍 asyncio 样板。
    """
    client = httpx.AsyncClient(transport=transport, timeout=10.0)

    async def _run() -> BaseException | None:
        try:
            await client.post(_EGRESS_URL, json=payload)
        except Exception as exc:
            return exc
        return None

    try:
        return asyncio.run(_run())
    finally:
        asyncio.run(client.aclose())


# --------------------------------------------------------------------------- rejection


class TestClassificationGateRejects:
    def test_context_above_ceiling_never_reaches_inner(self) -> None:
        """INTERNAL 分类数据发往 PUBLIC ceiling endpoint：inner 零调用，捕获零记录。"""
        received: list[bytes] = []

        def responder(request: httpx.Request) -> httpx.Response:
            received.append(request.read())
            return httpx.Response(200, json={"ok": True})

        transport = CaptureTransport(
            inner=httpx.MockTransport(responder),
            gate=classification_gate(_external_endpoint("PUBLIC"), "internal"),
        )
        exc = _post(transport, {"model": "m", "messages": [{"role": "user", "content": "secret"}]})

        assert exc is not None
        assert received == []
        assert transport.captures == []

    def test_error_is_classification_violation_not_provider_failure(self) -> None:
        """错误必须是分类违规：类型可与普通 provider failure（429/5xx → httpx.HTTPStatusError）
        区分，Runtime 据此归类而不把它计入 provider failure。"""
        transport = CaptureTransport(
            inner=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
            gate=classification_gate(_external_endpoint("PUBLIC"), "internal"),
        )
        exc = _post(transport, {"model": "m", "messages": []})

        assert isinstance(exc, ClassificationViolation)
        assert isinstance(exc, PreSendRejected)
        assert not isinstance(exc, httpx.HTTPError)
        assert "ceiling" in str(exc).lower()

    def test_unknown_classification_fails_closed(self) -> None:
        """未知分类不取「常见默认」：直接拒绝发送。"""
        transport = CaptureTransport(
            inner=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
            gate=classification_gate(_external_endpoint("CONFIDENTIAL"), "top-secret-unknown-level"),
        )
        exc = _post(transport, {"model": "m", "messages": []})
        assert isinstance(exc, ClassificationViolation)

    def test_floor_endpoint_rejects_internal_data(self) -> None:
        """未登记 endpoint（ceiling=PUBLIC）接 internal 数据：门禁同样生效。"""
        floor = EndpointRegistry.create_floor_endpoint("http://127.0.0.1:9/v1")
        transport = CaptureTransport(
            inner=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
            gate=classification_gate(floor, "internal"),
        )
        exc = _post(transport, {"model": "m", "messages": []})
        assert isinstance(exc, ClassificationViolation)
        assert transport.captures == []


# --------------------------------------------------------------------------- pass-through


class TestClassificationGateAllows:
    def test_context_at_or_below_ceiling_is_sent(self) -> None:
        received: list[bytes] = []

        def responder(request: httpx.Request) -> httpx.Response:
            received.append(request.read())
            return httpx.Response(200, json={"ok": True})

        transport = CaptureTransport(
            inner=httpx.MockTransport(responder),
            gate=classification_gate(_external_endpoint("INTERNAL"), "internal"),
        )
        exc = _post(transport, {"model": "m", "messages": [{"role": "user", "content": "ok"}]})

        assert exc is None
        assert len(received) == 1
        assert len(transport.captures) == 1


def test_gate_rejection_via_pytest_raises() -> None:
    """拒绝路径在 pytest 原生 raises 语义下同样成立，防止 _post 辅助吞掉异常类型。"""
    transport = CaptureTransport(
        inner=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
        gate=classification_gate(_external_endpoint("PUBLIC"), "restricted"),
    )
    client = httpx.AsyncClient(transport=transport, timeout=10.0)

    async def _run() -> None:
        await client.post(_EGRESS_URL, json={"model": "m", "messages": []})

    with pytest.raises(PreSendRejected):
        asyncio.run(_run())
    assert transport.captures == []
