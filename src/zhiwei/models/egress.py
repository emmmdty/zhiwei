"""S3/F-R5-04：模型 egress 生产组装——热切换门 + attestation 重做 + manifest 落账。

生产路径上把三件事串成一条链（T-P3.1 + T-P3.8）：

1. 跨 endpoint 热切换必经 ``evaluate_model_switch`` 门：拒绝即不出网（fail closed）；
2. ``attestation_required`` 的切换重做 fixture attestation（live attestation 属
   S3 登记债务，specs/s3-models-context.md §「计划实现」口径不变）；
3. 发送后 wire capture → ContextManifest、切换 → TransitionManifest（
   wire_body_digest 绑定真实 capture digest），经 ``ManifestSink`` port 落账——
   生产实现见 persistence.model_manifests（canonical event + audit + outbox
   同事务）。manifest 不落账 = 事后审计无证据可取（F-R2-12），sink 必填。

models 层不直接依赖 PG——sink 经 port 注入（与 models.first_use 同款分层）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict

from zhiwei.context.manifests import ContextManifest, TransitionManifest
from zhiwei.contracts.canonical import digest
from zhiwei.contracts.time import utc_now
from zhiwei.models.attestations import AttestationRegistry
from zhiwei.models.client_factory import build_model_client
from zhiwei.models.contracts import EndpointProfile, ModelProfile
from zhiwei.models.presend import CaptureTransport, WireCapture
from zhiwei.models.probes import probe_fixture_attestation
from zhiwei.models.router import ModelSwitchDecision, evaluate_model_switch

WIRE_MANIFEST_EVENT_TYPE = "models.egress.wire.manifest"
SWITCH_MANIFEST_EVENT_TYPE = "models.egress.switch.manifest"
MANIFEST_PAYLOAD_SCHEMA_VERSION = 1


class WireManifestPayload(BaseModel):
    """canonical event payload schema：ContextManifest 的 JSON 值域投影。"""

    model_config = ConfigDict(extra="forbid")

    manifest_id: str
    body_sha256: str
    body_len: int
    url: str
    method: str
    redacted_headers: dict[str, str]
    header_names: list[str]
    captured_at: str
    sequence_no: int


class SwitchManifestPayload(BaseModel):
    """canonical event payload schema：切换 TransitionManifest 的 JSON 值域投影。"""

    model_config = ConfigDict(extra="forbid")

    manifest_id: str
    transition_type: str
    wire_body_digest: str | None
    before_state_digest: str | None
    after_state_digest: str | None
    occurred_at: str


class ManifestSink(Protocol):
    """manifest 落账写入器 port（生产实现见 persistence.model_manifests）。

    返回 True 表示本次调用创建了记录；实现必须原子（canonical event + audit
    同事务），幂等键防重。
    """

    async def record_wire_manifest(
        self, manifest: ContextManifest, *, run_id: UUID
    ) -> bool: ...

    async def record_transition_manifest(
        self, manifest: TransitionManifest, *, run_id: UUID
    ) -> bool: ...


@dataclass(frozen=True)
class PreparedEgress:
    """prepare 阶段产物：拒绝时 client/transport 为 None， refusal_reason 必非空。"""

    client: httpx.AsyncClient | None
    transport: CaptureTransport | None
    switch_decision: ModelSwitchDecision | None
    refusal_reason: str = ""
    before_state_digest: str | None = None
    after_state_digest: str | None = None
    # F-R6-08：做出切换判定所用 context 分类（随 TransitionManifest 记录）
    context_classification: str | None = None


@dataclass(frozen=True)
class EgressResult:
    sent: bool
    error: str | None = None


def _egress_state_digest(endpoint: EndpointProfile) -> str:
    """egress 策略相关状态的确定性 digest（endpoint 身份 + 分类上界）。"""
    return digest(
        {
            "endpoint_id": endpoint.id,
            "classification_ceiling": endpoint.classification_ceiling.value,
        }
    )


class ModelEgressAssembler:
    """模型 egress 生产组装入口：switch 门 → attestation 重做 → 发送 → manifest 落账。"""

    def __init__(
        self,
        *,
        manifest_sink: ManifestSink,
        attestation_registry: AttestationRegistry | None = None,
    ) -> None:
        if manifest_sink is None:
            raise ValueError(
                "manifest_sink is required: manifests must be recorded (F-R2-12)"
            )
        self._sink = manifest_sink
        self._attestations = attestation_registry or AttestationRegistry()

    def prepare(
        self,
        *,
        endpoint: EndpointProfile,
        profile: ModelProfile,
        context_classification: str,
        current_endpoint: EndpointProfile | None = None,
        inner: httpx.AsyncBaseTransport | None = None,
        client_timeout: httpx.Timeout | None = None,
    ) -> PreparedEgress:
        """解析切换门并组装 gated client；拒绝时不构造任何可发送对象。"""
        switch_decision: ModelSwitchDecision | None = None
        before_digest: str | None = None
        after_digest: str | None = None

        if current_endpoint is not None and current_endpoint.id != endpoint.id:
            switch_decision = evaluate_model_switch(
                context_classification=context_classification,
                current_endpoint=current_endpoint,
                target_endpoint=endpoint,
                target_model_id=profile.model_name,
            )
            if not switch_decision.allowed:
                return PreparedEgress(
                    client=None,
                    transport=None,
                    switch_decision=switch_decision,
                    refusal_reason=switch_decision.reason,
                )
            if switch_decision.attestation_required:
                # attestation 重做：fixture 级离线探测（live attestation 是 S3
                # 登记债务）；注册失败异常原样上抛，切换中止。
                self._attestations.register(probe_fixture_attestation(profile))
            before_digest = _egress_state_digest(current_endpoint)
            after_digest = _egress_state_digest(endpoint)

        built = build_model_client(
            endpoint=endpoint,
            profile=profile,
            context_classification=context_classification,
            inner=inner,
            timeout=client_timeout,
        )
        return PreparedEgress(
            client=built.client,
            transport=built.transport,
            switch_decision=switch_decision,
            before_state_digest=before_digest,
            after_state_digest=after_digest,
            context_classification=context_classification,
        )

    async def execute(
        self,
        prepared: PreparedEgress,
        send: Callable[[httpx.AsyncClient], Awaitable[None]],
        *,
        run_id: UUID,
    ) -> EgressResult:
        """执行发送并在事后落账 manifest；gate 拒绝/无 capture 时不产生 manifest。"""
        if prepared.client is None or prepared.transport is None:
            return EgressResult(sent=False, error=prepared.refusal_reason or "egress refused")

        error: str | None = None
        try:
            await send(prepared.client)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await prepared.client.aclose()

        captures = prepared.transport.captures
        if not captures:
            return EgressResult(sent=False, error=error)

        for capture in captures:
            await self._sink.record_wire_manifest(
                _wire_manifest_from_capture(capture), run_id=run_id
            )
        if prepared.switch_decision is not None:
            await self._sink.record_transition_manifest(
                TransitionManifest(
                    manifest_id=f"trans-{uuid4()}",
                    before_state_digest=prepared.before_state_digest,
                    after_state_digest=prepared.after_state_digest,
                    transition_type="model.switch",
                    wire_body_digest=captures[-1].body_sha256,
                    items_added=0,
                    items_removed=0,
                    items_unchanged=0,
                    # F-R6-08：记录切换判定所用分类输入（审计面）
                    context_classification=prepared.context_classification,
                    occurred_at=utc_now().isoformat(),
                ),
                run_id=run_id,
            )
        return EgressResult(sent=error is None, error=error)


def _wire_manifest_from_capture(capture: WireCapture) -> ContextManifest:
    return ContextManifest(
        manifest_id=f"ctx-{capture.seq}-{capture.body_sha256.split(':', 1)[1][:16]}",
        body_sha256=capture.body_sha256,
        body_len=capture.body_len,
        url=capture.url,
        method=capture.method,
        redacted_headers=dict(capture.redacted_headers),
        header_names=tuple(capture.header_names),
        captured_at=capture.captured_at,
        sequence_no=capture.seq,
    )
