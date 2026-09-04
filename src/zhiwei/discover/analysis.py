"""S8-T4 AnalysisSpec and controlled exploration for the Discover pipeline.

Source diff/watermark 生成 typed comparison tasks (AnalysisSpec)。
模型只能提出 AnalysisSpec，不得自由读 DB 或写脚本。
所有路径必须经过 data quality、Evidence/falsification、dedupe、人类 triage。

事实源：specs/s8-discover-actions.md §5（controlled exploration）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class AnalysisType(StrEnum):
    """Typed analysis kinds — 模型只选类型，不写自由脚本。"""

    COMPARISON = "comparison"
    AGGREGATION = "aggregation"
    TREND = "trend"
    CORRELATION = "correlation"
    ANOMALY = "anomaly"


class AnalysisStatus(StrEnum):
    """AnalysisSpec lifecycle states."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class ComparisonSpec(_FrozenModel):
    """Typed comparison task — source diff/watermark 生成。

    结构化字段代替自由文本，确保可由确定性组件独立求值。
    """

    source_entity: str = Field(min_length=1, description="Primary entity to compare")
    target_entity: str = Field(
        default="",
        description="Comparison target; empty means baseline comparison",
    )
    metric: str = Field(min_length=1, description="Metric to compare")
    window_hours: int = Field(ge=1, description="Time window in hours")
    comparator: str = Field(
        min_length=1,
        description="Comparison operator (gt, lt, eq, etc.)",
    )
    threshold: float | None = Field(
        default=None,
        description="Threshold for significance; None means any difference",
    )
    group_by: str = Field(
        default="",
        description="Optional grouping dimension (e.g., 'region', 'department')",
    )


class AnalysisSpec(_FrozenModel):
    """Immutable typed analysis proposal — 模型只能提出，不能自由读 DB。

    AnalysisSpec 封装一次 controlled exploration 请求。执行由 allowed
    analysis tool 完成；模型不直接访问数据源或编写查询脚本。
    """

    id: UUID
    signal_id: UUID
    program_version_id: UUID
    analysis_type: AnalysisType
    comparison: ComparisonSpec
    rationale: str = Field(min_length=1, description="Why this analysis is needed")
    requested_by: str = Field(min_length=1, description="Entity that proposed this spec")
    status: AnalysisStatus = AnalysisStatus.PROPOSED
    hypothesis_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_spec_id: UUID | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_spec(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class AnalysisResult(_FrozenModel):
    """Immutable result of executing an AnalysisSpec。

    结果附加 EvidenceRef，进入 falsification/dedupe/人类 triage 流程。
    """

    id: UUID
    spec_id: UUID
    findings: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Typed findings produced by the analysis tool",
    )
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    metrics: dict[str, float] = Field(default_factory=dict)
    row_count: int = Field(ge=0, default=0)
    executed_at: datetime
    execution_method: str = Field(
        default="deterministic",
        description="Must be 'deterministic' — 模型只提出、不执行",
    )

    @field_validator("executed_at")
    @classmethod
    def _utc_result(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ControlledExplorationEngine:
    """Enforces the controlled exploration contract.

    模型只能提出 AnalysisSpec → engine 执行 → 结果经过
    data quality → Evidence/falsification → dedupe → 人类 triage。
    不允许模型自由读 DB 或写脚本。
    """

    def __init__(
        self,
        allowed_types: tuple[AnalysisType, ...] = (
            AnalysisType.COMPARISON,
            AnalysisType.AGGREGATION,
            AnalysisType.TREND,
        ),
    ) -> None:
        self._allowed_types = allowed_types
        self._specs: dict[UUID, AnalysisSpec] = {}
        self._results: dict[UUID, AnalysisResult] = {}

    @property
    def specs(self) -> tuple[AnalysisSpec, ...]:
        return tuple(self._specs.values())

    @property
    def results(self) -> tuple[AnalysisResult, ...]:
        return tuple(self._results.values())

    def validate_spec(self, spec: AnalysisSpec) -> tuple[bool, str]:
        """Validate an AnalysisSpec against allowed constraints.

        Returns (is_valid, reason).
        """
        if spec.analysis_type not in self._allowed_types:
            return (
                False,
                f"analysis type '{spec.analysis_type}' not in allowed types",
            )
        if not spec.comparison.metric:
            return False, "comparison metric must not be empty"
        if not spec.comparison.source_entity:
            return False, "comparison source_entity must not be empty"
        return True, "valid"

    def register_spec(self, spec: AnalysisSpec) -> AnalysisSpec:
        """Register an AnalysisSpec after validation.

        Raises ValueError if the spec is invalid.
        """
        is_valid, reason = self.validate_spec(spec)
        if not is_valid:
            raise ValueError(f"AnalysisSpec rejected: {reason}")
        self._specs[spec.id] = spec
        return spec

    def approve_spec(self, spec_id: UUID) -> AnalysisSpec:
        """Approve a spec for execution — transitions PROPOSED → APPROVED."""
        spec = self._get_spec(spec_id)
        if spec.status != AnalysisStatus.PROPOSED:
            raise ValueError(
                f"Cannot approve spec in {spec.status} status; only proposed can be approved"
            )
        approved = spec.model_copy(
            update={"status": AnalysisStatus.APPROVED, "created_at": spec.created_at}
        )
        self._specs[spec_id] = approved
        return approved

    def record_result(self, result: AnalysisResult) -> AnalysisResult:
        """Record the deterministic execution result of a spec.

        执行结果经过 data quality → Evidence/falsification → dedupe → 人类 triage。
        """
        spec = self._get_spec(result.spec_id)
        if spec.status not in (AnalysisStatus.APPROVED, AnalysisStatus.EXECUTING):
            raise ValueError(
                f"Cannot record result for spec in {spec.status} status"
            )
        completed_spec = spec.model_copy(
            update={"status": AnalysisStatus.COMPLETED, "created_at": spec.created_at}
        )
        self._specs[result.spec_id] = completed_spec
        self._results[result.id] = result
        return result

    def reject_spec(self, spec_id: UUID) -> AnalysisSpec:
        """Reject a proposed spec."""
        spec = self._get_spec(spec_id)
        if spec.status != AnalysisStatus.PROPOSED:
            raise ValueError(
                f"Cannot reject spec in {spec.status} status; only proposed can be rejected"
            )
        rejected = spec.model_copy(
            update={"status": AnalysisStatus.REJECTED, "created_at": spec.created_at}
        )
        self._specs[spec_id] = rejected
        return rejected

    def _get_spec(self, spec_id: UUID) -> AnalysisSpec:
        if spec_id not in self._specs:
            raise ValueError(f"AnalysisSpec {spec_id} not found")
        return self._specs[spec_id]


def create_analysis_spec(
    signal_id: UUID,
    program_version_id: UUID,
    analysis_type: AnalysisType,
    comparison: ComparisonSpec,
    rationale: str,
    requested_by: str,
    *,
    hypothesis_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> AnalysisSpec:
    """Factory: create an immutable AnalysisSpec with generated id and timestamp."""
    return AnalysisSpec(
        id=new_id(),
        signal_id=signal_id,
        program_version_id=program_version_id,
        analysis_type=analysis_type,
        comparison=comparison,
        rationale=rationale,
        requested_by=requested_by,
        hypothesis_id=hypothesis_id,
        metadata=metadata or {},
        created_at=datetime.now(UTC),
    )
