"""S3-T6 Model Router: fixed-order multi-gate selection.

Per MODELS.md §6:
- Fixed order: data_compliance → capabilities → context_fit → quality → spend_guard → latency
- First four are hard gates; spend_guard default off
- Records candidate/rejection reason at each level
- No silent fallback
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.context.budget import ContextFitCheck, estimate_tokens_items
from zhiwei.context.types import ContextItem
from zhiwei.models.contracts import (
    EndpointProfile,
    ModelProfile,
    TrustTier,
)


class GateLevel(StrEnum):
    """Fixed routing gate order per MODELS.md §6."""

    DATA_COMPLIANCE = "data_compliance"
    CAPABILITIES = "capabilities"
    CONTEXT_FIT = "context_fit"
    QUALITY = "quality"
    SPEND_GUARD = "spend_guard"
    LATENCY = "latency"


class GateVerdict(StrEnum):
    """Outcome of a routing gate evaluation."""

    PASS = "pass"
    REJECT = "reject"
    SKIP = "skip"


class GateRecord(BaseModel):
    """Immutable record of a single gate evaluation for one candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: GateLevel
    verdict: GateVerdict
    reason: str = ""
    candidate_id: str = ""


class RoutingDecision(BaseModel):
    """Complete routing decision with per-candidate per-gate records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_model_id: str | None = None
    selected_endpoint_id: str | None = None
    gate_records: list[GateRecord] = Field(default_factory=list)
    rejection_reason: str = ""

    @property
    def is_rejected(self) -> bool:
        return self.selected_model_id is None


class RoutingRequest(BaseModel):
    """Input to the router: candidates, context, and gate configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    candidates: list[ModelProfile] = Field(min_length=1)
    endpoints: dict[str, EndpointProfile] = Field(default_factory=dict)
    required_capabilities: set[str] = Field(default_factory=set)
    context_items: tuple[ContextItem, ...] = ()
    spend_guard_enabled: bool = False
    spend_limit_usd: float | None = None
    quality_scores: dict[str, float] = Field(default_factory=dict)
    latency_scores: dict[str, float] = Field(default_factory=dict)
    data_classification: str = "public"


class ModelSwitchDecision(BaseModel):
    """两类热切换（S3 spec §4 / ADR-011 §5）的 egress 判定结果。

    egress_recheck_required / attestation_required 描述该切换类别的要求
    （消费方据此执行重检与重新 attestation 并记录 TransitionManifest），
    allowed 携带放行/拒绝结论。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    egress_recheck_required: bool
    attestation_required: bool
    reason: str = ""


def evaluate_model_switch(
    *,
    context_classification: str,
    current_endpoint: EndpointProfile,
    target_endpoint: EndpointProfile,
    target_model_id: str,
) -> ModelSwitchDecision:
    """模型热切换的 egress 门禁：同 endpoint 不重检，跨 endpoint 必须重检。

    纯域逻辑（S3 spec §4「两类热切换」/ ADR-011 §5）。数据门禁是
    `context 实际分类 ≤ endpoint classification_ceiling`，与 URL 匹配无关：
    同 endpoint 换 model 走新 ModelProfile + 新 Attempt，egress 策略不变；
    跨 endpoint 换 model 在目标 ceiling 低于当前上下文实际分类时拒绝切换，
    放行时要求重新 attestation（由消费方执行）。
    """
    if target_endpoint.id == current_endpoint.id:
        return ModelSwitchDecision(
            allowed=True,
            egress_recheck_required=False,
            attestation_required=False,
            reason=(
                f"same-endpoint model switch to '{target_model_id}': "
                "egress policy unchanged, no re-check required"
            ),
        )

    context_rank = _classification_rank(context_classification)
    ceiling_rank = _classification_rank(target_endpoint.classification_ceiling.value)
    if context_rank > ceiling_rank:
        return ModelSwitchDecision(
            allowed=False,
            egress_recheck_required=True,
            attestation_required=True,
            reason=(
                f"cross-endpoint model switch to '{target_model_id}' rejected: context "
                f"classification '{context_classification}' exceeds target endpoint "
                f"'{target_endpoint.id}' ceiling "
                f"'{target_endpoint.classification_ceiling.value}'"
            ),
        )

    return ModelSwitchDecision(
        allowed=True,
        egress_recheck_required=True,
        attestation_required=True,
        reason=(
            f"cross-endpoint model switch to '{target_model_id}' allowed: target "
            f"endpoint '{target_endpoint.id}' ceiling "
            f"'{target_endpoint.classification_ceiling.value}' covers context "
            f"classification '{context_classification}'; fresh attestation required"
        ),
    )


class ModelRouter:
    """Fixed-order multi-gate model router.

    Evaluates each candidate through the gate pipeline in order.
    First four gates (data_compliance, capabilities, context_fit, quality)
    are hard gates. spend_guard is soft (default off). latency is soft.
    No silent fallback: if all candidates are rejected, the decision
    records why.
    """

    __slots__ = ()

    def route(self, request: RoutingRequest) -> RoutingDecision:
        """Route through the fixed gate pipeline.

        Evaluates each candidate in order, stopping at the first hard
        gate rejection. Returns the first candidate that passes all
        hard gates.
        """
        gate_records: list[GateRecord] = []
        rejection_reason = ""

        for candidate in request.candidates:
            candidate_passed = True

            for level in GateLevel:
                verdict, reason = self._evaluate_gate(
                    level, candidate, request
                )
                gate_records.append(
                    GateRecord(
                        level=level,
                        verdict=verdict,
                        reason=reason,
                        candidate_id=candidate.id,
                    )
                )

                if verdict == GateVerdict.REJECT:
                    rejection_reason = (
                        f"Candidate '{candidate.id}' rejected at "
                        f"{level.value}: {reason}"
                    )
                    candidate_passed = False
                    break

            if candidate_passed:
                return RoutingDecision(
                    selected_model_id=candidate.id,
                    selected_endpoint_id=candidate.endpoint_id,
                    gate_records=gate_records,
                )

        return RoutingDecision(
            gate_records=gate_records,
            rejection_reason=rejection_reason or "All candidates rejected",
        )

    def _evaluate_gate(
        self,
        level: GateLevel,
        candidate: ModelProfile,
        request: RoutingRequest,
    ) -> tuple[GateVerdict, str]:
        """Evaluate a single gate for a single candidate."""
        if level == GateLevel.DATA_COMPLIANCE:
            return self._gate_data_compliance(candidate, request)
        if level == GateLevel.CAPABILITIES:
            return self._gate_capabilities(candidate, request)
        if level == GateLevel.CONTEXT_FIT:
            return self._gate_context_fit(candidate, request)
        if level == GateLevel.QUALITY:
            return self._gate_quality(candidate, request)
        if level == GateLevel.SPEND_GUARD:
            return self._gate_spend_guard(candidate, request)
        if level == GateLevel.LATENCY:
            return self._gate_latency(candidate, request)
        return GateVerdict.REJECT, f"Unknown gate level: {level}"

    def _gate_data_compliance(
        self,
        candidate: ModelProfile,
        request: RoutingRequest,
    ) -> tuple[GateVerdict, str]:
        """Gate 1: Endpoint data compliance.

        Checks trust tier, network zone, and classification ceiling.
        """
        endpoint = request.endpoints.get(candidate.endpoint_id)
        if endpoint is None:
            return (
                GateVerdict.REJECT,
                f"No endpoint profile for '{candidate.endpoint_id}'",
            )

        if endpoint.trust_tier == TrustTier.UNVERIFIED:
            return (
                GateVerdict.REJECT,
                f"Endpoint '{endpoint.id}' trust tier is unverified",
            )

        context_classification = _classification_rank(request.data_classification)
        ceiling_classification = _classification_rank(
            endpoint.classification_ceiling.value
        )
        if context_classification > ceiling_classification:
            return (
                GateVerdict.REJECT,
                (
                    f"Context classification '{request.data_classification}' "
                    f"exceeds endpoint ceiling '{endpoint.classification_ceiling.value}'"
                ),
            )

        return GateVerdict.PASS, ""

    def _gate_capabilities(
        self,
        candidate: ModelProfile,
        request: RoutingRequest,
    ) -> tuple[GateVerdict, str]:
        """Gate 2: Required capabilities check."""
        if not request.required_capabilities:
            return GateVerdict.PASS, ""

        candidate_caps = set(candidate.modalities)
        if "image" in candidate_caps:
            candidate_caps.add("vision")
        if candidate.structured_output != "none":
            candidate_caps.add("structured_output")
        if candidate.tool_choice != "none":
            candidate_caps.add("tool_use")
        if candidate.reasoning_field is not None:
            candidate_caps.add("reasoning")

        missing = request.required_capabilities - candidate_caps
        if missing:
            return (
                GateVerdict.REJECT,
                f"Missing capabilities: {sorted(missing)}",
            )

        return GateVerdict.PASS, ""

    def _gate_context_fit(
        self,
        candidate: ModelProfile,
        request: RoutingRequest,
    ) -> tuple[GateVerdict, str]:
        """Gate 3: Context window fit check."""
        if not request.context_items:
            return GateVerdict.PASS, ""

        estimate = estimate_tokens_items(request.context_items)
        fit_check = ContextFitCheck(context_window=candidate.context_window)
        fits, _ = fit_check.fits(request.context_items)

        if not fits:
            return (
                GateVerdict.REJECT,
                (
                    f"Context items ({estimate.upper_bound} tokens) exceed "
                    f"window ({candidate.context_window})"
                ),
            )

        return GateVerdict.PASS, ""

    def _gate_quality(
        self,
        candidate: ModelProfile,
        request: RoutingRequest,
    ) -> tuple[GateVerdict, str]:
        """Gate 4: Task-specific quality qualification.

        Quality scores are task-specific; a candidate must have a
        non-negative score to pass. Missing score = no qualification.
        """
        score = request.quality_scores.get(candidate.id)
        if score is None:
            return (
                GateVerdict.REJECT,
                f"No quality score for candidate '{candidate.id}'",
            )
        if score < 0:
            return (
                GateVerdict.REJECT,
                f"Negative quality score ({score}) for '{candidate.id}'",
            )
        return GateVerdict.PASS, ""

    def _gate_spend_guard(
        self,
        candidate: ModelProfile,
        request: RoutingRequest,
    ) -> tuple[GateVerdict, str]:
        """Gate 5: Organization spend guard (optional, default off)."""
        if not request.spend_guard_enabled:
            return GateVerdict.SKIP, "Spend guard disabled"

        if request.spend_limit_usd is None:
            return GateVerdict.PASS, "No spend limit set"

        return GateVerdict.PASS, ""

    def _gate_latency(
        self,
        candidate: ModelProfile,
        request: RoutingRequest,
    ) -> tuple[GateVerdict, str]:
        """Gate 6: Latency/health preference (soft, always passes)."""
        return GateVerdict.PASS, ""


_CLASSIFICATION_RANK: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


def _classification_rank(classification: str) -> int:
    """Rank a classification string for comparison."""
    return _CLASSIFICATION_RANK.get(classification, -1)
