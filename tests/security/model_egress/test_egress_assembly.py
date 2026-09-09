"""F-R5-04：热切换生产路径——egress 组装串起 switch 门 + attestation 重做 + manifest 落账。

egress 组装（models.egress.ModelEgressAssembler）是生产路径上把
evaluate_model_switch、attestation 重做、ContextManifest/TransitionManifest 写入
串成一条链的唯一入口；跨 endpoint 切换必须经 switch 门（拒绝即不出网），
attestation_required 时重做 fixture attestation，发送后 wire/transition manifest
经 ManifestSink 落账（TransitionManifest.wire_body_digest 绑定真实 capture digest）。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx2 as httpx
import pytest

from zhiwei.context.manifests import ContextManifest, TransitionManifest
from zhiwei.models.attestations import AttestationRegistry
from zhiwei.models.contracts import (
    ClassificationCeiling,
    EndpointProfile,
    ModelProfile,
    WireProtocol,
)
from zhiwei.models.egress import ModelEgressAssembler

_URL = "http://127.0.0.1:9/v1/chat/completions"


def _endpoint(endpoint_id: str, ceiling: ClassificationCeiling) -> EndpointProfile:
    return EndpointProfile(
        id=endpoint_id,
        base_url=f"http://127.0.0.1:9/{endpoint_id}/v1",
        credential_env="TEST_KEY",
        classification_ceiling=ceiling,
        allowed_paths=("/chat/completions",),
    )


def _profile(endpoint_id: str) -> ModelProfile:
    return ModelProfile(
        id=f"mp-{endpoint_id}",
        endpoint_id=endpoint_id,
        model_name="test-model",
        wire_protocol=WireProtocol.OPENAI_CHAT,
        api_path="/chat/completions",
        context_window=1024,
    )


class RecordingSink:
    """ManifestSink 的测试替身：记录全部写入调用。"""

    def __init__(self) -> None:
        self.wire_manifests: list[ContextManifest] = []
        self.transition_manifests: list[TransitionManifest] = []

    async def record_wire_manifest(
        self, manifest: ContextManifest, *, run_id: UUID
    ) -> bool:
        self.wire_manifests.append(manifest)
        return True

    async def record_transition_manifest(
        self, manifest: TransitionManifest, *, run_id: UUID
    ) -> bool:
        self.transition_manifests.append(manifest)
        return True


def _responder() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))


class TestCrossEndpointSwitch:
    @pytest.mark.asyncio
    async def test_allowed_switch_records_attestation_and_transition_manifest(self) -> None:
        sink = RecordingSink()
        registry = AttestationRegistry()
        assembler = ModelEgressAssembler(manifest_sink=sink, attestation_registry=registry)
        current = _endpoint("ep-a", ClassificationCeiling.INTERNAL)
        target = _endpoint("ep-b", ClassificationCeiling.INTERNAL)

        prepared = assembler.prepare(
            endpoint=target,
            profile=_profile("ep-b"),
            context_classification="internal",
            current_endpoint=current,
            inner=_responder(),
        )
        run_id = uuid4()

        async def send(client: httpx.AsyncClient) -> None:
            await client.post(_URL, content=b'{"model":"m"}')

        result = await assembler.execute(prepared, send, run_id=run_id)

        assert result.error is None
        fresh = registry.get_latest("ep-b", "test-model")
        assert fresh is not None, "attestation_required 的跨 endpoint 切换必须重做 attestation"
        assert len(sink.transition_manifests) == 1
        manifest = sink.transition_manifests[0]
        assert manifest.transition_type == "model.switch"
        assert manifest.wire_body_digest is not None
        assert prepared.transport is not None
        assert manifest.wire_body_digest == prepared.transport.captures[0].body_sha256
        assert len(sink.wire_manifests) == 1
        assert sink.wire_manifests[0].body_sha256 == manifest.wire_body_digest

    @pytest.mark.asyncio
    async def test_refused_switch_never_sends_and_records_nothing(self) -> None:
        sink = RecordingSink()
        registry = AttestationRegistry()
        assembler = ModelEgressAssembler(manifest_sink=sink, attestation_registry=registry)
        current = _endpoint("ep-a", ClassificationCeiling.RESTRICTED)
        target = _endpoint("ep-b", ClassificationCeiling.PUBLIC)

        prepared = assembler.prepare(
            endpoint=target,
            profile=_profile("ep-b"),
            context_classification="restricted",
            current_endpoint=current,
            inner=_responder(),
        )
        assert prepared.client is None
        assert prepared.refusal_reason != ""
        assert registry.get_latest("ep-b", "test-model") is None
        assert sink.wire_manifests == []
        assert sink.transition_manifests == []

        async def send(client: httpx.AsyncClient) -> None:  # pragma: no cover
            raise AssertionError("refused egress must not send")

        result = await assembler.execute(prepared, send, run_id=uuid4())
        assert result.error is not None
        assert sink.wire_manifests == []


class TestSameEndpoint:
    @pytest.mark.asyncio
    async def test_same_endpoint_skips_switch_gate_and_transition_manifest(self) -> None:
        sink = RecordingSink()
        registry = AttestationRegistry()
        assembler = ModelEgressAssembler(manifest_sink=sink, attestation_registry=registry)
        endpoint = _endpoint("ep-a", ClassificationCeiling.INTERNAL)

        prepared = assembler.prepare(
            endpoint=endpoint,
            profile=_profile("ep-a"),
            context_classification="internal",
            current_endpoint=endpoint,
            inner=_responder(),
        )
        assert prepared.client is not None
        assert prepared.switch_decision is None
        assert registry.get_latest("ep-a", "test-model") is None

        async def send(client: httpx.AsyncClient) -> None:
            await client.post(_URL, content=b'{"model":"m"}')

        await assembler.execute(prepared, send, run_id=uuid4())
        assert len(sink.wire_manifests) == 1
        assert sink.transition_manifests == []


class TestSinkDiscipline:
    def test_assembler_requires_manifest_sink(self) -> None:
        with pytest.raises(ValueError, match="manifest_sink"):
            ModelEgressAssembler(manifest_sink=None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_first_endpoint_egress_needs_no_switch_gate(self) -> None:
        sink = RecordingSink()
        assembler = ModelEgressAssembler(
            manifest_sink=sink, attestation_registry=AttestationRegistry()
        )

        prepared = assembler.prepare(
            endpoint=_endpoint("ep-a", ClassificationCeiling.INTERNAL),
            profile=_profile("ep-a"),
            context_classification="internal",
            inner=_responder(),
        )
        assert prepared.client is not None
        assert prepared.switch_decision is None

        async def send(client: httpx.AsyncClient) -> None:
            await client.post(_URL, content=b'{"model":"m"}')

        await assembler.execute(prepared, send, run_id=uuid4())
        assert len(sink.wire_manifests) == 1
        assert sink.transition_manifests == []


class TestClientTimeoutThreading:
    """assembler.prepare 必须把显式超时预算传入工厂（S11 followup-3 bad case
    7cd50885：live 合成在库默认 5s read timeout 下 ReadTimeout）。"""

    @pytest.mark.asyncio
    async def test_prepare_threads_client_timeout_to_client(self) -> None:
        assembler = ModelEgressAssembler(manifest_sink=RecordingSink())
        prepared = assembler.prepare(
            endpoint=_endpoint("ep-a", ClassificationCeiling.PUBLIC),
            profile=_profile("ep-a"),
            context_classification="public",
            inner=_responder(),
            client_timeout=httpx.Timeout(120.0, connect=10.0),
        )
        assert prepared.client is not None
        assert prepared.client.timeout.read == 120.0
        assert prepared.client.timeout.connect == 10.0
