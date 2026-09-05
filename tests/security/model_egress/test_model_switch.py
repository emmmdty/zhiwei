"""S3 §4/§5: 两类模型热切换的 egress 门禁（ADR-011 §5）。

- 同 endpoint 换 model：egress 策略不变——不重做 egress 检查、不要求新 attestation
- 跨 endpoint 换 model：必须重新 egress 检查——目标 ceiling 低于当前上下文实际
  分类则拒绝切换；ceiling 足够则允许，且要求重新 attestation（TransitionManifest
  由消费方记录，本判定只输出语义）
"""

from __future__ import annotations

from zhiwei.models.contracts import (
    ClassificationCeiling,
    CredentialMode,
    EndpointProfile,
)
from zhiwei.models.router import evaluate_model_switch

# loopback 端口 9（discard）只作为 URL 标识使用，测试不真正连接。
_BASE_URL = "http://127.0.0.1:9/v1"


def _endpoint(endpoint_id: str, ceiling: ClassificationCeiling) -> EndpointProfile:
    return EndpointProfile(
        id=endpoint_id,
        base_url=_BASE_URL,
        credential_mode=CredentialMode.BEARER,
        credential_env=f"{endpoint_id.upper().replace('-', '_')}_API_KEY",
        allowed_paths=("/v1",),
        classification_ceiling=ceiling,
    )


class TestSameEndpointModelSwitch:
    def test_same_endpoint_switch_skips_egress_recheck(self) -> None:
        """热切换：同 endpoint 换 model 不重做 egress，不要求新 attestation。"""
        current = _endpoint("ep-a", ClassificationCeiling.CONFIDENTIAL)
        decision = evaluate_model_switch(
            context_classification="confidential",
            current_endpoint=current,
            target_endpoint=current,
            target_model_id="m2",
        )
        assert decision.allowed is True
        assert decision.egress_recheck_required is False
        assert decision.attestation_required is False


class TestCrossEndpointModelSwitch:
    def test_lower_ceiling_rejects_switch(self) -> None:
        """跨 endpoint 且目标 ceiling 低于当前上下文实际分类 → 拒绝切换。"""
        decision = evaluate_model_switch(
            context_classification="internal",
            current_endpoint=_endpoint("ep-a", ClassificationCeiling.CONFIDENTIAL),
            target_endpoint=_endpoint("ep-b", ClassificationCeiling.PUBLIC),
            target_model_id="m2",
        )
        assert decision.allowed is False
        assert decision.egress_recheck_required is True

    def test_sufficient_ceiling_allows_with_fresh_attestation(self) -> None:
        """跨 endpoint 且 ceiling 足够 → 允许，并要求重新 attestation。"""
        decision = evaluate_model_switch(
            context_classification="internal",
            current_endpoint=_endpoint("ep-a", ClassificationCeiling.PUBLIC),
            target_endpoint=_endpoint("ep-b", ClassificationCeiling.CONFIDENTIAL),
            target_model_id="m2",
        )
        assert decision.allowed is True
        assert decision.egress_recheck_required is True
        assert decision.attestation_required is True
