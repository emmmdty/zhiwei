"""S8 Numeric Detector Pack with deterministic known-pattern detection.

PatternRef independent replay. Data quality checks before hypothesis generation.

事实源：specs/s8-discover-actions.md §5，ADR-004。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc
from zhiwei.discover.signals import (
    DataQualityResult,
    NegativeProbe,
    Signal,
    SignalSeverity,
    Watermark,
)


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Comparator(StrEnum):
    """Comparison operators for numeric pattern matching."""

    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    NEQ = "neq"


class PatternRef(_FrozenModel):
    """Deterministic pattern reference for independent replay.

    A PatternRef encapsulates a single numeric comparison rule that can be
    deterministically re-evaluated against any matching data snapshot.
    """

    pattern_id: UUID
    name: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    comparator: Comparator
    threshold: float
    entity_scope: str = Field(default="all")
    window_hours: int = Field(ge=1, default=24)
    description: str = ""


class DetectionResult(_FrozenModel):
    """Result of evaluating a single PatternRef against a data snapshot."""

    pattern_ref: PatternRef
    matched: bool
    actual_value: float | None = None
    evaluated_at: datetime
    sample_size: int = Field(ge=0, default=0)

    @field_validator("evaluated_at")
    @classmethod
    def _utc_eval(cls, value: datetime) -> datetime:
        return ensure_utc(value)


COMPARISON_OPS: dict[Comparator, Any] = {}


def _register_ops() -> None:
    import operator

    COMPARISON_OPS.update(
        {
            Comparator.GT: operator.gt,
            Comparator.GTE: operator.ge,
            Comparator.LT: operator.lt,
            Comparator.LTE: operator.le,
            Comparator.EQ: operator.eq,
            Comparator.NEQ: operator.ne,
        }
    )


_register_ops()


def evaluate_pattern(pattern: PatternRef, actual_value: float) -> bool:
    """Deterministic evaluation of a pattern against a concrete value.

    模型只提出 probe，求值一律由确定性组件完成。
    """
    op = COMPARISON_OPS.get(pattern.comparator)
    if op is None:
        raise ValueError(f"Unknown comparator: {pattern.comparator}")
    return bool(op(actual_value, pattern.threshold))


def generate_probe_from_pattern(pattern: PatternRef) -> NegativeProbe:
    """Convert a PatternRef into a typed NegativeProbe for falsification.

    ADR-004: probe 必须 typed，断言归约为可机器求值的结构。
    """
    return NegativeProbe(
        probe_id=new_id(),
        metric=pattern.metric,
        entity_scope=pattern.entity_scope,
        window_hours=pattern.window_hours,
        comparator=pattern.comparator.value,
        threshold=pattern.threshold,
        description=pattern.description or f"Pattern: {pattern.name}",
    )


class DataQualityCheck(_FrozenModel):
    """Configuration for a single data quality check."""

    check_name: str = Field(min_length=1)
    min_row_count: int = Field(ge=0, default=0)
    required_columns: tuple[str, ...] = Field(default_factory=tuple)
    max_null_ratio: float = Field(ge=0.0, le=1.0, default=0.0)


class DataQualityGate:
    """Runs configured data quality checks before hypothesis generation.

    所有路径必须经过 data quality。
    """

    def __init__(self, checks: tuple[DataQualityCheck, ...] = ()) -> None:
        self._checks = checks

    def run_checks(
        self,
        row_count: int,
        columns: set[str],
        null_ratios: dict[str, float] | None = None,
    ) -> tuple[DataQualityResult, ...]:
        """Evaluate all configured checks. Returns one result per check."""
        results: list[DataQualityResult] = []
        null_ratios = null_ratios or {}

        for check in self._checks:
            passed = True
            details: dict[str, Any] = {}

            if row_count < check.min_row_count:
                passed = False
                details["reason"] = (
                    f"row_count {row_count} < min {check.min_row_count}"
                )

            missing_cols = set(check.required_columns) - columns
            if missing_cols:
                passed = False
                details["missing_columns"] = sorted(missing_cols)

            for col in check.required_columns:
                ratio = null_ratios.get(col, 0.0)
                if ratio > check.max_null_ratio:
                    passed = False
                    details.setdefault("high_null_columns", []).append(
                        {"column": col, "ratio": ratio}
                    )

            results.append(
                DataQualityResult(
                    check_name=check.check_name,
                    passed=passed,
                    details=details,
                    row_count=row_count,
                )
            )

        return tuple(results)


class NumericDetectorPack:
    """Numeric Risk Detector Pack: deterministic known-pattern detection.

    Pipeline: data snapshot → DataQualityGate → PatternRef evaluation → Signal
    """

    def __init__(
        self,
        pack_id: UUID,
        version: int,
        patterns: tuple[PatternRef, ...] = (),
        dq_checks: tuple[DataQualityCheck, ...] = (),
    ) -> None:
        self._pack_id = pack_id
        self._version = version
        self._patterns = patterns
        self._dq_gate = DataQualityGate(dq_checks)

    @property
    def pack_id(self) -> UUID:
        return self._pack_id

    @property
    def version(self) -> int:
        return self._version

    @property
    def patterns(self) -> tuple[PatternRef, ...]:
        return self._patterns

    def run_data_quality(
        self,
        row_count: int,
        columns: set[str],
        null_ratios: dict[str, float] | None = None,
    ) -> tuple[DataQualityResult, ...]:
        """Run data quality checks before hypothesis generation."""
        return self._dq_gate.run_checks(row_count, columns, null_ratios)

    def detect(
        self,
        program_version_id: UUID,
        values: dict[str, float],
        watermarks: tuple[Watermark, ...] = (),
        *,
        dq_row_count: int = 0,
        dq_columns: set[str] | None = None,
        dq_null_ratios: dict[str, float] | None = None,
    ) -> tuple[Signal, ...]:
        """Run all patterns against the provided metric values.

        Returns a Signal for each matched pattern. Data quality checks run
        first; all DQ results are attached to each Signal.
        """
        dq_results = self.run_data_quality(
            dq_row_count,
            dq_columns or set(),
            dq_null_ratios,
        )

        signals: list[Signal] = []
        for pattern in self._patterns:
            actual = values.get(pattern.metric)
            if actual is None:
                continue
            matched = evaluate_pattern(pattern, actual)
            if matched:
                severity = (
                    SignalSeverity.HIGH if pattern.comparator in (Comparator.GT, Comparator.GTE)
                    else SignalSeverity.WARNING
                )
                signals.append(
                    Signal(
                        id=new_id(),
                        program_version_id=program_version_id,
                        detector_pack_id=self._pack_id,
                        detector_pack_version=self._version,
                        severity=severity,
                        title=f"Pattern matched: {pattern.name}",
                        description=pattern.description,
                        source_watermarks=watermarks,
                        data_quality_results=dq_results,
                        affected_entities=(pattern.entity_scope,),
                        created_at=datetime.now(UTC),
                    )
                )

        return tuple(signals)

    def replay_pattern(
        self,
        pattern_id: UUID,
        actual_value: float,
    ) -> DetectionResult:
        """PatternRef independent replay: re-evaluate a single pattern."""
        pattern = self._find_pattern(pattern_id)
        matched = evaluate_pattern(pattern, actual_value)
        return DetectionResult(
            pattern_ref=pattern,
            matched=matched,
            actual_value=actual_value,
            evaluated_at=datetime.now(UTC),
            sample_size=1,
        )

    def _find_pattern(self, pattern_id: UUID) -> PatternRef:
        for p in self._patterns:
            if p.pattern_id == pattern_id:
                return p
        raise ValueError(f"Pattern {pattern_id} not found in pack {self._pack_id}")
