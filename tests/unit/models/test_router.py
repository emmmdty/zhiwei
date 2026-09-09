"""S3-T6 Unit: Model Router tests.

Tests the fixed-order multi-gate routing pipeline per MODELS.md §6.
"""

from __future__ import annotations

from typing import Any

from zhiwei.models.contracts import (
    ClassificationCeiling,
    CredentialMode,
    EndpointProfile,
    ModelProfile,
    NetworkZone,
    TrustTier,
    WireProtocol,
)
from zhiwei.models.router import (
    GateLevel,
    GateVerdict,
    ModelRouter,
    RoutingDecision,
    RoutingRequest,
)

# ---- Factories ----


def _make_endpoint(**overrides: Any) -> EndpointProfile:
    defaults: dict[str, Any] = {
        "id": "ep-1",
        "base_url": "https://api.test.example.com/v1",
        "credential_mode": CredentialMode.BEARER,
        "credential_env": "TEST_KEY",
        "trust_tier": TrustTier.REVIEWED,
        "network_zone": NetworkZone.EXTERNAL,
        "classification_ceiling": ClassificationCeiling.INTERNAL,
        "allowed_paths": ("/chat/completions",),
    }
    defaults.update(overrides)
    return EndpointProfile(**defaults)


def _make_model(**overrides: Any) -> ModelProfile:
    defaults: dict[str, Any] = {
        "id": "model-1",
        "endpoint_id": "ep-1",
        "model_name": "test-model",
        "wire_protocol": WireProtocol.OPENAI_CHAT,
        "api_path": "/chat/completions",
        "context_window": 128000,
        "max_output": 8192,
        "modalities": ("text",),
    }
    defaults.update(overrides)
    return ModelProfile(**defaults)


# ---- Gate ordering tests ----


class TestGateOrdering:
    """Gates must be evaluated in the fixed order."""

    def test_gate_levels_are_ordered(self) -> None:
        levels = list(GateLevel)
        expected = [
            GateLevel.DATA_COMPLIANCE,
            GateLevel.CAPABILITIES,
            GateLevel.CONTEXT_FIT,
            GateLevel.QUALITY,
            GateLevel.SPEND_GUARD,
            GateLevel.LATENCY,
        ]
        assert levels == expected

    def test_data_compliance_evaluated_first(self) -> None:
        """If data_compliance rejects, no other gate is evaluated."""
        router = ModelRouter()
        ep = _make_endpoint(trust_tier=TrustTier.UNVERIFIED)
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
        )
        decision = router.route(request)

        assert decision.is_rejected
        compliance_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.DATA_COMPLIANCE
        ]
        assert len(compliance_records) == 1
        assert compliance_records[0].verdict == GateVerdict.REJECT
        # No records beyond data_compliance for this candidate
        later_records = [
            r for r in decision.gate_records
            if r.level not in (GateLevel.DATA_COMPLIANCE,)
        ]
        assert len(later_records) == 0


# ---- Data compliance gate tests ----


class TestDataComplianceGate:
    def test_rejects_unverified_endpoint(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint(trust_tier=TrustTier.UNVERIFIED)
        model = _make_model()
        request = RoutingRequest(candidates=[model], endpoints={"ep-1": ep})
        decision = router.route(request)

        assert decision.is_rejected
        assert "unverified" in decision.rejection_reason.lower()

    def test_rejects_missing_endpoint(self) -> None:
        router = ModelRouter()
        model = _make_model(endpoint_id="nonexistent")
        request = RoutingRequest(candidates=[model], endpoints={})
        decision = router.route(request)

        assert decision.is_rejected
        assert "nonexistent" in decision.rejection_reason

    def test_rejects_classification_exceeds_ceiling(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint(
            classification_ceiling=ClassificationCeiling.PUBLIC,
        )
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            data_classification="internal",
        )
        decision = router.route(request)

        assert decision.is_rejected
        assert "exceeds" in decision.rejection_reason.lower()

    def test_passes_when_classification_within_ceiling(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint(
            classification_ceiling=ClassificationCeiling.INTERNAL,
        )
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            data_classification="public",
        )
        decision = router.route(request)

        compliance_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.DATA_COMPLIANCE
        ]
        assert compliance_records[0].verdict == GateVerdict.PASS


# ---- Capabilities gate tests ----


class TestCapabilitiesGate:
    def test_passes_when_no_required_capabilities(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            required_capabilities=set(),
        )
        decision = router.route(request)

        cap_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.CAPABILITIES
        ]
        assert cap_records[0].verdict == GateVerdict.PASS

    def test_rejects_missing_capability(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model(modalities=("text",), structured_output="none")
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            required_capabilities={"vision"},
        )
        decision = router.route(request)

        assert decision.is_rejected
        assert "vision" in decision.rejection_reason

    def test_passes_with_matching_capabilities(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model(
            modalities=("text", "image"),
            structured_output="json_schema",
        )
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            required_capabilities={"vision", "structured_output"},
        )
        decision = router.route(request)

        cap_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.CAPABILITIES
        ]
        assert cap_records[0].verdict == GateVerdict.PASS


# ---- Context fit gate tests ----


class TestContextFitGate:
    def test_passes_when_no_context_items(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            context_items=(),
        )
        decision = router.route(request)

        fit_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.CONTEXT_FIT
        ]
        assert fit_records[0].verdict == GateVerdict.PASS

    def test_rejects_when_items_exceed_window(self) -> None:
        from zhiwei.context.types import ContextCategory, ContextItem

        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model(context_window=100)
        # Create content that will exceed 100 tokens
        large_content = {"data": "x " * 2000}
        items = (ContextItem(category=ContextCategory.CONVERSATIONAL, content=large_content),)
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            context_items=items,
        )
        decision = router.route(request)

        assert decision.is_rejected
        assert "exceed" in decision.rejection_reason.lower()


# ---- Quality gate tests ----


class TestQualityGate:
    def test_rejects_when_no_quality_score(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            quality_scores={},
        )
        decision = router.route(request)

        quality_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.QUALITY
        ]
        assert quality_records[0].verdict == GateVerdict.REJECT

    def test_rejects_negative_quality_score(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            quality_scores={"model-1": -1.0},
        )
        decision = router.route(request)

        assert decision.is_rejected

    def test_passes_with_non_negative_score(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            quality_scores={"model-1": 0.8},
        )
        decision = router.route(request)

        quality_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.QUALITY
        ]
        assert quality_records[0].verdict == GateVerdict.PASS


# ---- Spend guard gate tests ----


class TestSpendGuardGate:
    def test_skipped_when_disabled(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            spend_guard_enabled=False,
            quality_scores={"model-1": 0.5},
        )
        decision = router.route(request)

        spend_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.SPEND_GUARD
        ]
        assert spend_records[0].verdict == GateVerdict.SKIP

    def test_passes_when_enabled_without_limit(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            spend_guard_enabled=True,
            spend_limit_usd=None,
            quality_scores={"model-1": 0.5},
        )
        decision = router.route(request)

        spend_records = [
            r for r in decision.gate_records
            if r.level == GateLevel.SPEND_GUARD
        ]
        assert spend_records[0].verdict == GateVerdict.PASS


# ---- Multi-candidate tests ----


class TestMultiCandidate:
    def test_first_passing_candidate_selected(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        m1 = _make_model(id="model-1")
        m2 = _make_model(id="model-2")
        request = RoutingRequest(
            candidates=[m1, m2],
            endpoints={"ep-1": ep},
            quality_scores={"model-1": 0.9, "model-2": 0.7},
        )
        decision = router.route(request)

        assert decision.selected_model_id == "model-1"

    def test_fallback_to_second_candidate(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        m1 = _make_model(id="model-1", context_window=100)
        m2 = _make_model(id="model-2", context_window=128000)
        from zhiwei.context.types import ContextCategory, ContextItem

        large_content = {"data": "x " * 2000}
        items = (ContextItem(category=ContextCategory.CONVERSATIONAL, content=large_content),)
        request = RoutingRequest(
            candidates=[m1, m2],
            endpoints={"ep-1": ep},
            context_items=items,
            quality_scores={"model-1": 0.9, "model-2": 0.7},
        )
        decision = router.route(request)

        assert decision.selected_model_id == "model-2"

    def test_all_candidates_rejected(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint(trust_tier=TrustTier.UNVERIFIED)
        m1 = _make_model(id="model-1")
        m2 = _make_model(id="model-2")
        request = RoutingRequest(
            candidates=[m1, m2],
            endpoints={"ep-1": ep},
        )
        decision = router.route(request)

        assert decision.is_rejected
        assert decision.selected_model_id is None


# ---- No silent fallback tests ----


class TestNoSilentFallback:
    def test_rejection_reason_recorded(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint()
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
            quality_scores={},
        )
        decision = router.route(request)

        assert decision.is_rejected
        assert len(decision.rejection_reason) > 0

    def test_gate_records_complete(self) -> None:
        router = ModelRouter()
        ep = _make_endpoint(trust_tier=TrustTier.UNVERIFIED)
        model = _make_model()
        request = RoutingRequest(
            candidates=[model],
            endpoints={"ep-1": ep},
        )
        decision = router.route(request)

        assert len(decision.gate_records) >= 1
        assert decision.gate_records[0].level == GateLevel.DATA_COMPLIANCE
        assert decision.gate_records[0].verdict == GateVerdict.REJECT


# ---- RoutingDecision model tests ----


class TestRoutingDecision:
    def test_is_rejected_when_no_selection(self) -> None:
        d = RoutingDecision()
        assert d.is_rejected

    def test_not_rejected_when_model_selected(self) -> None:
        d = RoutingDecision(selected_model_id="m1", selected_endpoint_id="e1")
        assert not d.is_rejected
