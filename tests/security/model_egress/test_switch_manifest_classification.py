"""F-R6-08（T-P4.6b/B7）：model.switch 的 TransitionManifest 记录分类输入。

finding 建议「diff 写进 TransitionManifest/usage 事件」的第一步：manifest
携带做出切换判定所用 classification（egress prepare 的 context_classification），
审计可回答「当时按什么分类放行」。派生机制（B7 主体）落地后 runtime 调用方
传入派生值——本批先钉 manifest 记录面。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx2 as httpx
import pytest

from zhiwei.models.contracts import (
    ClassificationCeiling,
    EndpointProfile,
    ModelProfile,
    WireProtocol,
)
from zhiwei.models.egress import ModelEgressAssembler

_URL = "http://127.0.0.1:9/ep-b/v1/chat/completions"


def _endpoint(endpoint_id: str) -> EndpointProfile:
    return EndpointProfile(
        id=endpoint_id,
        base_url=f"http://127.0.0.1:9/{endpoint_id}/v1",
        credential_env="TEST_KEY",
        classification_ceiling=ClassificationCeiling.RESTRICTED,
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


class _RecordingSink:
    def __init__(self) -> None:
        self.transition_manifests: list = []

    async def record_wire_manifest(self, manifest, *, run_id: UUID) -> bool:
        return True

    async def record_transition_manifest(self, manifest, *, run_id: UUID) -> bool:
        self.transition_manifests.append(manifest)
        return True


def _responder() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))


@pytest.mark.asyncio
async def test_switch_manifest_records_context_classification() -> None:
    from zhiwei.models.attestations import AttestationRegistry

    sink = _RecordingSink()
    assembler = ModelEgressAssembler(
        manifest_sink=sink, attestation_registry=AttestationRegistry()
    )
    prepared = assembler.prepare(
        endpoint=_endpoint("ep-b"),
        profile=_profile("ep-b"),
        context_classification="confidential",
        current_endpoint=_endpoint("ep-a"),
        inner=_responder(),
    )

    async def send(client: httpx.AsyncClient) -> None:
        await client.post(_URL, content=b'{"model":"m"}')

    result = await assembler.execute(prepared, send, run_id=uuid4())
    assert result.error is None
    assert sink.transition_manifests, "跨 endpoint 切换必须记录 TransitionManifest"
    assert sink.transition_manifests[0].context_classification == "confidential"
