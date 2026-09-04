"""S8-T3 tests: Numeric Detector Pack.

覆盖 S8 spec §5:
- Deterministic known-pattern detection
- PatternRef independent replay
- Data quality checks before hypothesis generation
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhiwei.contracts.identifiers import new_id
from zhiwei.discover.detectors import (
    Comparator,
    DataQualityCheck,
    DataQualityGate,
    DetectionResult,
    NumericDetectorPack,
    PatternRef,
    evaluate_pattern,
    generate_probe_from_pattern,
)
from zhiwei.discover.signals import DataQualityResult, Watermark

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pattern(**overrides: object) -> PatternRef:
    defaults: dict[str, object] = {
        "pattern_id": new_id(),
        "name": "high-spend-delta",
        "metric": "spend_delta_pct",
        "comparator": Comparator.GT,
        "threshold": 50.0,
        "entity_scope": "vendor:acme-corp",
        "window_hours": 168,
        "description": "Detect spend delta exceeding 50%",
    }
    defaults.update(overrides)
    return PatternRef(**defaults)  # type: ignore[arg-type]


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
# PatternRef
# ---------------------------------------------------------------------------


class TestPatternRef:
    def test_pattern_ref_is_frozen(self) -> None:
        p = _make_pattern()
        with pytest.raises(ValidationError):
            p.name = "changed"  # type: ignore[misc]

    def test_pattern_ref_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            PatternRef(
                pattern_id=new_id(),
                name="test",
                metric="x",
                comparator=Comparator.GT,
                threshold=0.0,
                bogus=True,  # type: ignore[call-arg]
            )

    def test_pattern_ref_defaults(self) -> None:
        p = PatternRef(
            pattern_id=new_id(),
            name="test",
            metric="x",
            comparator=Comparator.GT,
            threshold=0.0,
        )
        assert p.entity_scope == "all"
        assert p.window_hours == 24


# ---------------------------------------------------------------------------
# Deterministic evaluation
# ---------------------------------------------------------------------------


class TestDeterministicEvaluation:
    def test_gt_match(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=50.0)
        assert evaluate_pattern(p, 75.0) is True

    def test_gt_no_match(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=50.0)
        assert evaluate_pattern(p, 30.0) is False

    def test_gt_boundary(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=50.0)
        assert evaluate_pattern(p, 50.0) is False

    def test_gte_match(self) -> None:
        p = _make_pattern(comparator=Comparator.GTE, threshold=50.0)
        assert evaluate_pattern(p, 50.0) is True

    def test_lt_match(self) -> None:
        p = _make_pattern(comparator=Comparator.LT, threshold=10.0)
        assert evaluate_pattern(p, 5.0) is True

    def test_lte_match(self) -> None:
        p = _make_pattern(comparator=Comparator.LTE, threshold=10.0)
        assert evaluate_pattern(p, 10.0) is True

    def test_eq_match(self) -> None:
        p = _make_pattern(comparator=Comparator.EQ, threshold=42.0)
        assert evaluate_pattern(p, 42.0) is True

    def test_eq_no_match(self) -> None:
        p = _make_pattern(comparator=Comparator.EQ, threshold=42.0)
        assert evaluate_pattern(p, 43.0) is False

    def test_neq_match(self) -> None:
        p = _make_pattern(comparator=Comparator.NEQ, threshold=42.0)
        assert evaluate_pattern(p, 43.0) is True

    def test_neq_no_match(self) -> None:
        p = _make_pattern(comparator=Comparator.NEQ, threshold=42.0)
        assert evaluate_pattern(p, 42.0) is False

    def test_negative_values(self) -> None:
        p = _make_pattern(comparator=Comparator.LT, threshold=-10.0)
        assert evaluate_pattern(p, -20.0) is True

    def test_zero_threshold(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=0.0)
        assert evaluate_pattern(p, 0.001) is True


# ---------------------------------------------------------------------------
# PatternRef independent replay
# ---------------------------------------------------------------------------


class TestPatternReplay:
    def test_replay_match(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=50.0)
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        result = pack.replay_pattern(p.pattern_id, 75.0)
        assert result.matched is True
        assert result.actual_value == 75.0
        assert result.pattern_ref.pattern_id == p.pattern_id

    def test_replay_no_match(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=50.0)
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        result = pack.replay_pattern(p.pattern_id, 30.0)
        assert result.matched is False

    def test_replay_unknown_pattern_raises(self) -> None:
        pack = NumericDetectorPack(pack_id=new_id(), version=1)
        with pytest.raises(ValueError, match="not found"):
            pack.replay_pattern(new_id(), 75.0)

    def test_detection_result_is_frozen(self) -> None:
        p = _make_pattern()
        result = DetectionResult(
            pattern_ref=p, matched=True, actual_value=75.0,
            evaluated_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            result.matched = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Data quality checks
# ---------------------------------------------------------------------------


class TestDataQualityChecks:
    def test_dq_check_is_frozen(self) -> None:
        check = DataQualityCheck(check_name="row_count")
        with pytest.raises(ValidationError):
            check.check_name = "changed"  # type: ignore[misc]

    def test_dq_gate_all_pass(self) -> None:
        checks = (
            DataQualityCheck(
                check_name="min_rows",
                min_row_count=10,
                required_columns=("a", "b"),
            ),
        )
        gate = DataQualityGate(checks)
        results = gate.run_checks(
            row_count=100, columns={"a", "b", "c"},
        )
        assert len(results) == 1
        assert results[0].passed is True

    def test_dq_gate_row_count_fail(self) -> None:
        checks = (
            DataQualityCheck(check_name="min_rows", min_row_count=100),
        )
        gate = DataQualityGate(checks)
        results = gate.run_checks(row_count=5, columns=set())
        assert results[0].passed is False
        assert "row_count" in results[0].details["reason"]

    def test_dq_gate_missing_columns_fail(self) -> None:
        checks = (
            DataQualityCheck(
                check_name="schema",
                required_columns=("a", "b"),
            ),
        )
        gate = DataQualityGate(checks)
        results = gate.run_checks(row_count=10, columns={"a"})
        assert results[0].passed is False
        assert "b" in results[0].details["missing_columns"]

    def test_dq_gate_high_null_ratio_fail(self) -> None:
        checks = (
            DataQualityCheck(
                check_name="null_check",
                required_columns=("col_a",),
                max_null_ratio=0.1,
            ),
        )
        gate = DataQualityGate(checks)
        results = gate.run_checks(
            row_count=100,
            columns={"col_a"},
            null_ratios={"col_a": 0.5},
        )
        assert results[0].passed is False
        assert results[0].details["high_null_columns"][0]["ratio"] == 0.5

    def test_dq_gate_empty_checks(self) -> None:
        gate = DataQualityGate()
        results = gate.run_checks(row_count=0, columns=set())
        assert results == ()

    def test_dq_result_is_frozen(self) -> None:
        result = DataQualityResult(
            check_name="test", passed=True, row_count=10,
        )
        with pytest.raises(ValidationError):
            result.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Data quality before detection
# ---------------------------------------------------------------------------


class TestDQBeforeDetection:
    def test_dq_results_attached_to_signal(self) -> None:
        p = _make_pattern()
        check = DataQualityCheck(
            check_name="min_rows", min_row_count=10,
        )
        pack = NumericDetectorPack(
            pack_id=new_id(),
            version=1,
            patterns=(p,),
            dq_checks=(check,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
            dq_row_count=100,
            dq_columns=set(),
        )
        assert len(signals) == 1
        assert len(signals[0].data_quality_results) == 1
        assert signals[0].data_quality_results[0].check_name == "min_rows"

    def test_no_patterns_no_signals(self) -> None:
        pack = NumericDetectorPack(pack_id=new_id(), version=1)
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
        )
        assert signals == ()

    def test_no_matching_values_no_signals(self) -> None:
        p = _make_pattern(metric="nonexistent_metric")
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
        )
        assert signals == ()


# ---------------------------------------------------------------------------
# NumericDetectorPack detection
# ---------------------------------------------------------------------------


class TestNumericDetection:
    def test_detect_match(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=50.0)
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
        )
        assert len(signals) == 1
        assert "high-spend-delta" in signals[0].title

    def test_detect_no_match(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=50.0)
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 30.0},
        )
        assert signals == ()

    def test_detect_multiple_patterns(self) -> None:
        p1 = _make_pattern(
            name="high-spend",
            metric="spend_delta_pct",
            comparator=Comparator.GT,
            threshold=50.0,
        )
        p2 = _make_pattern(
            name="low-spend",
            metric="spend_delta_pct",
            comparator=Comparator.LT,
            threshold=10.0,
        )
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p1, p2),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 5.0},
        )
        assert len(signals) == 1
        assert "low-spend" in signals[0].title

    def test_detect_high_severity_for_gt(self) -> None:
        p = _make_pattern(comparator=Comparator.GT, threshold=50.0)
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
        )
        from zhiwei.discover.signals import SignalSeverity
        assert signals[0].severity == SignalSeverity.HIGH

    def test_detect_warning_severity_for_lt(self) -> None:
        p = _make_pattern(comparator=Comparator.LT, threshold=10.0)
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 5.0},
        )
        from zhiwei.discover.signals import SignalSeverity
        assert signals[0].severity == SignalSeverity.WARNING

    def test_detect_links_watermarks(self) -> None:
        wm = _make_watermark()
        p = _make_pattern()
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
            watermarks=(wm,),
        )
        assert len(signals[0].source_watermarks) == 1

    def test_detect_links_affected_entities(self) -> None:
        p = _make_pattern(entity_scope="vendor:acme-corp")
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
        )
        assert signals[0].affected_entities == ("vendor:acme-corp",)

    def test_detect_links_to_detector_pack(self) -> None:
        dp_id = new_id()
        p = _make_pattern()
        pack = NumericDetectorPack(
            pack_id=dp_id, version=3, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
        )
        assert signals[0].detector_pack_id == dp_id
        assert signals[0].detector_pack_version == 3

    def test_signal_is_frozen(self) -> None:
        p = _make_pattern()
        pack = NumericDetectorPack(
            pack_id=new_id(), version=1, patterns=(p,),
        )
        signals = pack.detect(
            program_version_id=new_id(),
            values={"spend_delta_pct": 75.0},
        )
        with pytest.raises(ValidationError):
            signals[0].title = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Probe generation from PatternRef (ADR-004)
# ---------------------------------------------------------------------------


class TestProbeGeneration:
    def test_generate_probe_from_pattern(self) -> None:
        p = _make_pattern()
        probe = generate_probe_from_pattern(p)
        assert probe.metric == p.metric
        assert probe.entity_scope == p.entity_scope
        assert probe.window_hours == p.window_hours
        assert probe.comparator == p.comparator.value
        assert probe.threshold == p.threshold

    def test_probe_is_typed_not_free_text(self) -> None:
        p = _make_pattern()
        probe = generate_probe_from_pattern(p)
        assert probe.metric != ""
        assert probe.entity_scope != ""
        assert probe.comparator in ("gt", "gte", "lt", "lte", "eq", "neq")

    def test_probe_has_unique_id(self) -> None:
        p = _make_pattern()
        probe1 = generate_probe_from_pattern(p)
        probe2 = generate_probe_from_pattern(p)
        assert probe1.probe_id != probe2.probe_id

    def test_probe_is_frozen(self) -> None:
        p = _make_pattern()
        probe = generate_probe_from_pattern(p)
        with pytest.raises(ValidationError):
            probe.metric = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Pack properties
# ---------------------------------------------------------------------------


class TestPackProperties:
    def test_pack_id(self) -> None:
        dp_id = new_id()
        pack = NumericDetectorPack(pack_id=dp_id, version=1)
        assert pack.pack_id == dp_id

    def test_pack_version(self) -> None:
        pack = NumericDetectorPack(pack_id=new_id(), version=3)
        assert pack.version == 3

    def test_pack_patterns(self) -> None:
        p = _make_pattern()
        pack = NumericDetectorPack(pack_id=new_id(), version=1, patterns=(p,))
        assert len(pack.patterns) == 1
