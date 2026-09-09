"""F-R2-01/F-R2-12：models/ 唯一 client 工厂的 gate 注入契约（ADR-001）。

工厂是 gate 从「可选」变「必选」的唯一位置：classification gate（ADR-011 §4）与
max_wire_body_bytes gate（ModelProfile 声明值）都由工厂注入，调用方无法构造出
无门 egress。全部离线（MockTransport 作 inner），不发任何真实请求。
"""

from __future__ import annotations

import httpx2 as httpx
import pytest

from zhiwei.models.client_factory import build_model_client
from zhiwei.models.contracts import (
    ClassificationCeiling,
    EndpointProfile,
    ModelProfile,
    WireProtocol,
)
from zhiwei.models.presend import (
    CaptureTransport,
    ClassificationViolation,
    PreSendRejected,
    WireBodyTooLarge,
)

_URL = "http://127.0.0.1:9/v1/chat/completions"
_BODY = b'{"model":"m","messages":[]}'


def _endpoint(ceiling: ClassificationCeiling = ClassificationCeiling.INTERNAL) -> EndpointProfile:
    return EndpointProfile(
        id="ep-test",
        base_url="http://127.0.0.1:9/v1",
        credential_env="TEST_KEY",
        classification_ceiling=ceiling,
        allowed_paths=("/chat/completions",),
    )


def _profile(max_wire_body_bytes: int = 8_388_608) -> ModelProfile:
    return ModelProfile(
        id="mp-test",
        endpoint_id="ep-test",
        model_name="test-model",
        wire_protocol=WireProtocol.OPENAI_CHAT,
        api_path="/chat/completions",
        context_window=1024,
        max_wire_body_bytes=max_wire_body_bytes,
    )


def _recording_responder(received: list[bytes]) -> httpx.MockTransport:
    def responder(request: httpx.Request) -> httpx.Response:
        received.append(request.read())
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(responder)


def _noop_responder() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))


class TestFactoryGateInjection:
    @pytest.mark.asyncio
    async def test_classification_gate_refuses_above_ceiling(self) -> None:
        received: list[bytes] = []
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(),
            context_classification="restricted",
            inner=_recording_responder(received),
        )
        with pytest.raises(PreSendRejected) as excinfo:
            await built.client.post(_URL, content=_BODY)
        assert isinstance(excinfo.value, ClassificationViolation)
        assert received == []
        assert built.transport.captures == []

    @pytest.mark.asyncio
    async def test_unknown_classification_fails_closed(self) -> None:
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(),
            context_classification="not-a-classification",
            inner=_noop_responder(),
        )
        with pytest.raises(ClassificationViolation):
            await built.client.post(_URL, content=_BODY)

    @pytest.mark.asyncio
    async def test_wire_size_gate_refuses_oversized_body(self) -> None:
        received: list[bytes] = []
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(max_wire_body_bytes=16),
            context_classification="internal",
            inner=_recording_responder(received),
        )
        with pytest.raises(PreSendRejected) as excinfo:
            await built.client.post(_URL, content=b"x" * 17)
        assert isinstance(excinfo.value, WireBodyTooLarge)
        assert received == []
        assert built.transport.captures == []

    @pytest.mark.asyncio
    async def test_within_limits_reaches_inner_and_captures(self) -> None:
        received: list[bytes] = []
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(max_wire_body_bytes=1024),
            context_classification="internal",
            inner=_recording_responder(received),
        )
        await built.client.post(_URL, content=_BODY)
        assert len(received) == 1
        assert len(built.transport.captures) == 1
        assert built.transport.captures[0].body_sha256.startswith("sha256:")

    @pytest.mark.asyncio
    async def test_transport_is_capture_transport(self) -> None:
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(),
            context_classification="internal",
            inner=_noop_responder(),
        )
        assert isinstance(built.transport, CaptureTransport)


class TestFactoryDefaults:
    def test_default_inner_is_real_http_transport(self) -> None:
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(),
            context_classification="internal",
        )
        assert isinstance(built.transport.inner, httpx.AsyncHTTPTransport)

    def test_factory_never_returns_ungated_transport(self) -> None:
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(),
            context_classification="internal",
            inner=_noop_responder(),
        )
        assert built.transport.gate is not None


class TestExplicitTimeout:
    """显式超时预算必须落在 client 上（S11 followup-3 bad case 7cd50885）。

    live 合成在 httpx 库默认 5s read timeout 下对 reasoning 模型 ReadTimeout——
    LLM 端到端延迟（reasoning + 生成）常态超过 5s，超时预算必须显式声明；
    缺省不传保持库默认（既有调用方行为不变）。
    """

    def test_explicit_timeout_lands_on_client(self) -> None:
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(),
            context_classification="public",
            inner=_noop_responder(),
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        assert built.client.timeout.read == 120.0
        assert built.client.timeout.connect == 10.0

    def test_default_keeps_library_default(self) -> None:
        built = build_model_client(
            endpoint=_endpoint(),
            profile=_profile(),
            context_classification="public",
            inner=_noop_responder(),
        )
        assert built.client.timeout == httpx.Timeout(5.0)
