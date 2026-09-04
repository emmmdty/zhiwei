"""S8-T4 tests: AnalysisSpec and controlled exploration.

覆盖 S8 spec §5:
- Source diff/watermark generates typed comparison tasks
- Model can only propose AnalysisSpec, not freely read DB
- All paths go through data quality, Evidence/falsification, dedupe, human triage
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from zhiwei.contracts.identifiers import new_id
from zhiwei.discover.analysis import (
    AnalysisResult,
    AnalysisSpec,
    AnalysisStatus,
    AnalysisType,
    ComparisonSpec,
    ControlledExplorationEngine,
    create_analysis_spec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_comparison(**overrides: object) -> ComparisonSpec:
    defaults: dict[str, object] = {
        "source_entity": "vendor:acme-corp",
        "metric": "spend_delta_pct",
        "window_hours": 168,
        "comparator": "gt",
        "threshold": 50.0,
    }
    defaults.update(overrides)
    return ComparisonSpec(**defaults)  # type: ignore[arg-type]


def _make_spec(**overrides: object) -> AnalysisSpec:
    defaults: dict[str, object] = {
        "id": new_id(),
        "signal_id": new_id(),
        "program_version_id": new_id(),
        "analysis_type": AnalysisType.COMPARISON,
        "comparison": _make_comparison(),
        "rationale": "Investigate spending anomaly",
        "requested_by": "model-proposer",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return AnalysisSpec(**defaults)  # type: ignore[arg-type]


def _make_result(spec_id: UUID | None = None, **overrides: object) -> AnalysisResult:
    defaults: dict[str, object] = {
        "id": new_id(),
        "spec_id": spec_id or new_id(),
        "findings": ("spend increased 300%",),
        "evidence_refs": ("ref:ledger-001",),
        "metrics": {"spend_delta_pct": 300.0},
        "row_count": 42,
        "executed_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return AnalysisResult(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ComparisonSpec
# ---------------------------------------------------------------------------


class TestComparisonSpec:
    def test_comparison_is_frozen(self) -> None:
        c = _make_comparison()
        with pytest.raises(ValidationError):
            c.metric = "changed"  # type: ignore[misc]

    def test_comparison_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ComparisonSpec(
                source_entity="vendor:x",
                metric="spend",
                window_hours=24,
                comparator="gt",
                bogus=True,  # type: ignore[call-arg]
            )

    def test_comparison_defaults(self) -> None:
        c = ComparisonSpec(
            source_entity="vendor:x",
            metric="spend",
            window_hours=24,
            comparator="gt",
        )
        assert c.target_entity == ""
        assert c.threshold is None
        assert c.group_by == ""


# ---------------------------------------------------------------------------
# AnalysisSpec
# ---------------------------------------------------------------------------


class TestAnalysisSpec:
    def test_spec_is_frozen(self) -> None:
        s = _make_spec()
        with pytest.raises(ValidationError):
            s.rationale = "changed"  # type: ignore[misc]

    def test_spec_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            _make_spec(bogus=True)  # type: ignore[call-arg]

    def test_spec_default_status_proposed(self) -> None:
        s = _make_spec()
        assert s.status == AnalysisStatus.PROPOSED

    def test_spec_links_to_signal(self) -> None:
        sig_id = new_id()
        s = _make_spec(signal_id=sig_id)
        assert s.signal_id == sig_id

    def test_spec_links_to_hypothesis(self) -> None:
        hyp_id = new_id()
        s = _make_spec(hypothesis_id=hyp_id)
        assert s.hypothesis_id == hyp_id

    def test_spec_has_comparison(self) -> None:
        comp = _make_comparison(metric="revenue_delta")
        s = _make_spec(comparison=comp)
        assert s.comparison.metric == "revenue_delta"

    def test_spec_analysis_types(self) -> None:
        for at in AnalysisType:
            s = _make_spec(analysis_type=at)
            assert s.analysis_type == at


# ---------------------------------------------------------------------------
# AnalysisResult
# ---------------------------------------------------------------------------


class TestAnalysisResult:
    def test_result_is_frozen(self) -> None:
        r = _make_result()
        with pytest.raises(ValidationError):
            r.findings = ()  # type: ignore[misc]

    def test_result_links_to_spec(self) -> None:
        spec_id = new_id()
        r = _make_result(spec_id=spec_id)
        assert r.spec_id == spec_id

    def test_result_execution_method_default(self) -> None:
        r = _make_result()
        assert r.execution_method == "deterministic"


# ---------------------------------------------------------------------------
# ControlledExplorationEngine
# ---------------------------------------------------------------------------


class TestControlledExplorationEngine:
    def test_engine_starts_empty(self) -> None:
        engine = ControlledExplorationEngine()
        assert len(engine.specs) == 0
        assert len(engine.results) == 0

    def test_register_valid_spec(self) -> None:
        engine = ControlledExplorationEngine()
        spec = _make_spec()
        registered = engine.register_spec(spec)
        assert registered.id == spec.id
        assert len(engine.specs) == 1

    def test_register_rejects_invalid_type(self) -> None:
        engine = ControlledExplorationEngine(
            allowed_types=(AnalysisType.COMPARISON,),
        )
        spec = _make_spec(analysis_type=AnalysisType.ANOMALY)
        with pytest.raises(ValueError, match="not in allowed types"):
            engine.register_spec(spec)

    def test_comparison_rejects_empty_metric(self) -> None:
        with pytest.raises(ValidationError, match="at least 1 character"):
            ComparisonSpec(
                source_entity="vendor:x",
                metric="",
                window_hours=24,
                comparator="gt",
            )

    def test_comparison_rejects_empty_source_entity(self) -> None:
        with pytest.raises(ValidationError, match="at least 1 character"):
            ComparisonSpec(
                source_entity="",
                metric="spend",
                window_hours=24,
                comparator="gt",
            )

    def test_approve_spec(self) -> None:
        engine = ControlledExplorationEngine()
        spec = _make_spec()
        engine.register_spec(spec)
        approved = engine.approve_spec(spec.id)
        assert approved.status == AnalysisStatus.APPROVED

    def test_approve_rejects_non_proposed(self) -> None:
        engine = ControlledExplorationEngine()
        spec = _make_spec()
        engine.register_spec(spec)
        engine.approve_spec(spec.id)
        with pytest.raises(ValueError, match="Cannot approve"):
            engine.approve_spec(spec.id)

    def test_reject_spec(self) -> None:
        engine = ControlledExplorationEngine()
        spec = _make_spec()
        engine.register_spec(spec)
        rejected = engine.reject_spec(spec.id)
        assert rejected.status == AnalysisStatus.REJECTED

    def test_reject_rejects_non_proposed(self) -> None:
        engine = ControlledExplorationEngine()
        spec = _make_spec()
        engine.register_spec(spec)
        engine.approve_spec(spec.id)
        with pytest.raises(ValueError, match="Cannot reject"):
            engine.reject_spec(spec.id)

    def test_record_result(self) -> None:
        engine = ControlledExplorationEngine()
        spec = _make_spec()
        engine.register_spec(spec)
        engine.approve_spec(spec.id)
        result = _make_result(spec_id=spec.id)
        recorded = engine.record_result(result)
        assert recorded.id == result.id
        assert len(engine.results) == 1

    def test_record_result_updates_spec_status(self) -> None:
        engine = ControlledExplorationEngine()
        spec = _make_spec()
        engine.register_spec(spec)
        engine.approve_spec(spec.id)
        result = _make_result(spec_id=spec.id)
        engine.record_result(result)
        updated_spec = engine._specs[spec.id]
        assert updated_spec.status == AnalysisStatus.COMPLETED

    def test_record_result_rejects_non_approved_spec(self) -> None:
        engine = ControlledExplorationEngine()
        spec = _make_spec()
        engine.register_spec(spec)
        result = _make_result(spec_id=spec.id)
        with pytest.raises(ValueError, match="Cannot record result"):
            engine.record_result(result)

    def test_get_unknown_spec_raises(self) -> None:
        engine = ControlledExplorationEngine()
        with pytest.raises(ValueError, match="not found"):
            engine._get_spec(new_id())


# ---------------------------------------------------------------------------
# create_analysis_spec factory
# ---------------------------------------------------------------------------


class TestCreateAnalysisSpec:
    def test_factory_creates_valid_spec(self) -> None:
        sig_id = new_id()
        pvid = new_id()
        comp = _make_comparison()
        spec = create_analysis_spec(
            signal_id=sig_id,
            program_version_id=pvid,
            analysis_type=AnalysisType.COMPARISON,
            comparison=comp,
            rationale="Investigate anomaly",
            requested_by="model",
        )
        assert spec.signal_id == sig_id
        assert spec.program_version_id == pvid
        assert spec.analysis_type == AnalysisType.COMPARISON
        assert spec.status == AnalysisStatus.PROPOSED
        assert spec.created_at.tzinfo is not None

    def test_factory_with_optional_fields(self) -> None:
        hyp_id = new_id()
        spec = create_analysis_spec(
            signal_id=new_id(),
            program_version_id=new_id(),
            analysis_type=AnalysisType.TREND,
            comparison=_make_comparison(),
            rationale="Trend check",
            requested_by="model",
            hypothesis_id=hyp_id,
            metadata={"key": "value"},
        )
        assert spec.hypothesis_id == hyp_id
        assert spec.metadata == {"key": "value"}

    def test_factory_unique_ids(self) -> None:
        spec1 = create_analysis_spec(
            signal_id=new_id(),
            program_version_id=new_id(),
            analysis_type=AnalysisType.COMPARISON,
            comparison=_make_comparison(),
            rationale="a",
            requested_by="m",
        )
        spec2 = create_analysis_spec(
            signal_id=new_id(),
            program_version_id=new_id(),
            analysis_type=AnalysisType.COMPARISON,
            comparison=_make_comparison(),
            rationale="b",
            requested_by="m",
        )
        assert spec1.id != spec2.id
