"""S8-T2 tests: RiskHypothesis with ADR-004 falsification.

覆盖 ADR-004:
- RiskHypothesis: supporting/contradicting/missing, affected entities,
  source watermark, detector version
- NegativeProbe typed: {metric, entity_scope, window, comparator, threshold}
- Sequential falsification: generate probes → execute → accumulate evidence
- Separation: probe generation and evaluation by independent task nodes
- Model proposes, deterministic evaluation only
- Admission: min N probes executed and not refuted → triage
- Falsification coverage and refutation rate as first-class metrics
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhiwei.contracts.identifiers import new_id
from zhiwei.discover.hypotheses import (
    EvidenceTag,
    FalsificationTracker,
    HypothesisChain,
    HypothesisKind,
    HypothesisStatus,
    RiskHypothesis,
)
from zhiwei.discover.signals import (
    FalsificationResult,
    NegativeProbe,
    Watermark,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hypothesis(**overrides: object) -> RiskHypothesis:
    defaults: dict[str, object] = {
        "id": new_id(),
        "signal_id": new_id(),
        "program_version_id": new_id(),
        "detector_pack_id": new_id(),
        "detector_pack_version": 1,
        "kind": HypothesisKind.SUPPORTING,
        "title": "Anomalous spending in vendor X",
        "description": "Vendor X spending increased 300% MoM",
        "affected_entities": ("vendor:acme-corp", "dept:finance"),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return RiskHypothesis(**defaults)  # type: ignore[arg-type]


def _make_probe(**overrides: object) -> NegativeProbe:
    defaults: dict[str, object] = {
        "probe_id": new_id(),
        "metric": "spend_delta_pct",
        "entity_scope": "vendor:acme-corp",
        "window_hours": 168,
        "comparator": "gt",
        "threshold": 50.0,
        "description": "If hypothesis is false, spend delta should exceed 50%",
    }
    defaults.update(overrides)
    return NegativeProbe(**defaults)  # type: ignore[arg-type]


def _make_watermark(**overrides: object) -> Watermark:
    defaults: dict[str, object] = {
        "source_id": new_id(),
        "field_name": "updated_at",
        "value": "2026-01-01T00:00:00Z",
        "captured_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Watermark(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RiskHypothesis basic fields
# ---------------------------------------------------------------------------


class TestRiskHypothesisFields:
    def test_hypothesis_is_frozen(self) -> None:
        h = _make_hypothesis()
        with pytest.raises(ValidationError):
            h.title = "changed"  # type: ignore[misc]

    def test_hypothesis_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            RiskHypothesis(
                id=new_id(),
                signal_id=new_id(),
                program_version_id=new_id(),
                detector_pack_id=new_id(),
                detector_pack_version=1,
                title="test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                bogus=True,  # type: ignore[call-arg]
            )

    def test_hypothesis_has_affected_entities(self) -> None:
        h = _make_hypothesis(affected_entities=("vendor:a", "vendor:b"))
        assert h.affected_entities == ("vendor:a", "vendor:b")

    def test_hypothesis_has_source_watermarks(self) -> None:
        wm = _make_watermark()
        h = _make_hypothesis(source_watermarks=(wm,))
        assert len(h.source_watermarks) == 1
        assert h.source_watermarks[0].source_id == wm.source_id

    def test_hypothesis_links_to_detector_pack(self) -> None:
        dp_id = new_id()
        h = _make_hypothesis(detector_pack_id=dp_id, detector_pack_version=3)
        assert h.detector_pack_id == dp_id
        assert h.detector_pack_version == 3

    def test_hypothesis_links_to_signal(self) -> None:
        sig_id = new_id()
        h = _make_hypothesis(signal_id=sig_id)
        assert h.signal_id == sig_id

    def test_hypothesis_default_status_proposed(self) -> None:
        h = _make_hypothesis()
        assert h.status == HypothesisStatus.PROPOSED

    def test_hypothesis_score_is_optional(self) -> None:
        h = _make_hypothesis()
        assert h.score is None

    def test_hypothesis_score_range(self) -> None:
        h = _make_hypothesis(score=0.75)
        assert h.score == 0.75

    def test_hypothesis_score_rejects_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            _make_hypothesis(score=1.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HypothesisKind (supporting/contradicting/missing)
# ---------------------------------------------------------------------------


class TestHypothesisKind:
    def test_supporting(self) -> None:
        h = _make_hypothesis(kind=HypothesisKind.SUPPORTING)
        assert h.kind == HypothesisKind.SUPPORTING

    def test_contradicting(self) -> None:
        h = _make_hypothesis(kind=HypothesisKind.CONTRADICTING)
        assert h.kind == HypothesisKind.CONTRADICTING

    def test_missing(self) -> None:
        h = _make_hypothesis(kind=HypothesisKind.MISSING)
        assert h.kind == HypothesisKind.MISSING


# ---------------------------------------------------------------------------
# Evidence tags
# ---------------------------------------------------------------------------


class TestEvidenceTags:
    def test_evidence_tag_is_frozen(self) -> None:
        tag = EvidenceTag(
            tag_id=new_id(),
            kind=HypothesisKind.SUPPORTING,
            description="Spending confirmed by ledger",
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            tag.description = "changed"  # type: ignore[misc]

    def test_hypothesis_has_evidence_tags(self) -> None:
        tag = EvidenceTag(
            tag_id=new_id(),
            kind=HypothesisKind.SUPPORTING,
            description="Test evidence",
            created_at=datetime.now(UTC),
        )
        h = _make_hypothesis(evidence_tags=(tag,))
        assert len(h.evidence_tags) == 1
        assert h.evidence_tags[0].kind == HypothesisKind.SUPPORTING


# ---------------------------------------------------------------------------
# Linked version (immutable chain)
# ---------------------------------------------------------------------------


class TestHypothesisLinkedVersion:
    def test_with_falsification_result_creates_new_id(self) -> None:
        h = _make_hypothesis()
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe,
            passed=True,
            actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        h2 = h.with_falsification_result(result)
        assert h2.id != h.id
        assert h2.parent_hypothesis_id == h.id
        assert len(h2.falsification_results) == 1

    def test_with_status_creates_new_id(self) -> None:
        h = _make_hypothesis()
        h2 = h.with_status(HypothesisStatus.FALSIFICATION_IN_PROGRESS)
        assert h2.id != h.id
        assert h2.parent_hypothesis_id == h.id
        assert h2.status == HypothesisStatus.FALSIFICATION_IN_PROGRESS

    def test_original_unchanged_after_linked_version(self) -> None:
        h = _make_hypothesis()
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe,
            passed=True,
            actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        h.with_falsification_result(result)
        assert len(h.falsification_results) == 0
        assert h.status == HypothesisStatus.PROPOSED


# ---------------------------------------------------------------------------
# Falsification coverage (ADR-004 first-class metric)
# ---------------------------------------------------------------------------


class TestFalsificationCoverage:
    def test_coverage_zero_when_no_probes(self) -> None:
        h = _make_hypothesis()
        assert h.falsification_coverage == 0.0

    def test_coverage_partial(self) -> None:
        probe1 = _make_probe()
        probe2 = _make_probe()
        result1 = FalsificationResult(
            probe=probe1, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        h = _make_hypothesis(
            proposed_probes=(probe1, probe2),
            falsification_results=(result1,),
        )
        assert h.falsification_coverage == pytest.approx(0.5)

    def test_coverage_full(self) -> None:
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        h = _make_hypothesis(
            proposed_probes=(probe,),
            falsification_results=(result,),
        )
        assert h.falsification_coverage == 1.0

    def test_coverage_capped_at_1(self) -> None:
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        # More results than proposed probes — still capped at 1.0
        h = _make_hypothesis(
            proposed_probes=(probe,),
            falsification_results=(result, result),
        )
        assert h.falsification_coverage == 1.0


# ---------------------------------------------------------------------------
# Refutation detection (ADR-004)
# ---------------------------------------------------------------------------


class TestRefutationDetection:
    def test_not_refuted_when_all_pass(self) -> None:
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        h = _make_hypothesis(falsification_results=(result,))
        assert h.is_fully_falsified is False

    def test_refuted_when_any_fails(self) -> None:
        probe = _make_probe()
        passed = FalsificationResult(
            probe=probe, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        failed = FalsificationResult(
            probe=probe, passed=False, actual_value=5.0,
            evaluated_at=datetime.now(UTC),
        )
        h = _make_hypothesis(falsification_results=(passed, failed))
        assert h.is_fully_falsified is True

    def test_min_probes_met(self) -> None:
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        h = _make_hypothesis(falsification_results=(result,))
        assert h.min_probes_met is True

    def test_min_probes_not_met_when_empty(self) -> None:
        h = _make_hypothesis()
        assert h.min_probes_met is False

    def test_min_probes_not_met_when_refuted(self) -> None:
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=False, actual_value=5.0,
            evaluated_at=datetime.now(UTC),
        )
        h = _make_hypothesis(falsification_results=(result,))
        assert h.min_probes_met is False


# ---------------------------------------------------------------------------
# Admission check (ADR-004)
# ---------------------------------------------------------------------------


class TestAdmissionCheck:
    def test_cannot_admit_with_no_results(self) -> None:
        h = _make_hypothesis()
        assert h.can_enter_triage(min_probes_required=3) is False

    def test_cannot_admit_with_insufficient_results(self) -> None:
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        h = _make_hypothesis(falsification_results=(result,))
        assert h.can_enter_triage(min_probes_required=3) is False

    def test_can_admit_when_enough_probes_pass(self) -> None:
        probe = _make_probe()
        results = tuple(
            FalsificationResult(
                probe=probe, passed=True, actual_value=float(i),
                evaluated_at=datetime.now(UTC),
            )
            for i in range(3)
        )
        h = _make_hypothesis(falsification_results=results)
        assert h.can_enter_triage(min_probes_required=3) is True

    def test_cannot_admit_when_any_probe_fails(self) -> None:
        probe = _make_probe()
        results = []
        for i in range(3):
            results.append(
                FalsificationResult(
                    probe=probe,
                    passed=i < 2,
                    actual_value=float(i),
                    evaluated_at=datetime.now(UTC),
                )
            )
        h = _make_hypothesis(falsification_results=tuple(results))
        assert h.can_enter_triage(min_probes_required=3) is False


# ---------------------------------------------------------------------------
# FalsificationTracker (separation of probe generation and evaluation)
# ---------------------------------------------------------------------------


class TestFalsificationTracker:
    def test_tracker_starts_with_hypothesis_probes(self) -> None:
        probe = _make_probe()
        h = _make_hypothesis(proposed_probes=(probe,))
        tracker = FalsificationTracker(h, min_probes_required=3)
        assert len(tracker.generated_probes) == 1

    def test_tracker_adds_probe(self) -> None:
        h = _make_hypothesis()
        tracker = FalsificationTracker(h, min_probes_required=3)
        probe = _make_probe()
        tracker.add_probe(probe)
        assert len(tracker.generated_probes) == 1

    def test_tracker_records_result(self) -> None:
        h = _make_hypothesis()
        tracker = FalsificationTracker(h, min_probes_required=1)
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        tracker.record_result(result)
        assert len(tracker.results) == 1

    def test_tracker_coverage(self) -> None:
        probe1 = _make_probe()
        probe2 = _make_probe()
        h = _make_hypothesis(proposed_probes=(probe1, probe2))
        tracker = FalsificationTracker(h, min_probes_required=1)
        result = FalsificationResult(
            probe=probe1, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        tracker.record_result(result)
        assert tracker.coverage == pytest.approx(0.5)

    def test_tracker_is_refuted(self) -> None:
        h = _make_hypothesis()
        tracker = FalsificationTracker(h, min_probes_required=1)
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=False, actual_value=5.0,
            evaluated_at=datetime.now(UTC),
        )
        tracker.record_result(result)
        assert tracker.is_refuted is True

    def test_tracker_admission_check_rejected_when_refuted(self) -> None:
        h = _make_hypothesis()
        tracker = FalsificationTracker(h, min_probes_required=1)
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=False, actual_value=5.0,
            evaluated_at=datetime.now(UTC),
        )
        tracker.record_result(result)
        can_admit, reason = tracker.admission_check()
        assert can_admit is False
        assert "refuted" in reason

    def test_tracker_admission_check_rejected_when_insufficient(self) -> None:
        h = _make_hypothesis()
        tracker = FalsificationTracker(h, min_probes_required=3)
        probe = _make_probe()
        result = FalsificationResult(
            probe=probe, passed=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        tracker.record_result(result)
        can_admit, reason = tracker.admission_check()
        assert can_admit is False
        assert "3 probes" in reason

    def test_tracker_admission_check_admitted(self) -> None:
        h = _make_hypothesis()
        tracker = FalsificationTracker(h, min_probes_required=2)
        probe = _make_probe()
        for _ in range(2):
            result = FalsificationResult(
                probe=probe, passed=True, actual_value=75.0,
                evaluated_at=datetime.now(UTC),
            )
            tracker.record_result(result)
        can_admit, reason = tracker.admission_check()
        assert can_admit is True
        assert reason == "admitted"

    def test_tracker_can_admit_property(self) -> None:
        h = _make_hypothesis()
        tracker = FalsificationTracker(h, min_probes_required=2)
        assert tracker.can_admit is False
        probe = _make_probe()
        for _ in range(2):
            result = FalsificationResult(
                probe=probe, passed=True, actual_value=75.0,
                evaluated_at=datetime.now(UTC),
            )
            tracker.record_result(result)
        assert tracker.can_admit is True


# ---------------------------------------------------------------------------
# HypothesisChain
# ---------------------------------------------------------------------------


class TestHypothesisChain:
    def test_chain_links_hypotheses(self) -> None:
        h1 = new_id()
        h2 = new_id()
        chain = HypothesisChain(
            root_hypothesis_id=h1,
            chain=(h1, h2),
            latest_hypothesis_id=h2,
            created_at=datetime.now(UTC),
        )
        assert chain.root_hypothesis_id == h1
        assert chain.latest_hypothesis_id == h2
        assert len(chain.chain) == 2

    def test_chain_is_frozen(self) -> None:
        chain = HypothesisChain(
            root_hypothesis_id=new_id(),
            chain=(new_id(),),
            latest_hypothesis_id=new_id(),
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            chain.latest_hypothesis_id = new_id()  # type: ignore[misc]
