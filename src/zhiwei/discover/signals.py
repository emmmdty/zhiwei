"""S8 Signal domain types for the Discover pipeline.

Signal: immutable linked version — 每个 Signal 绑定到产生它的
ProgramVersion 和 DetectorPack，不可变。

Pipeline: Trigger → watermark/snapshot → DataQualityResult → Signal

事实源：specs/s8-discover-actions.md §4。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.time import ensure_utc


class SignalSeverity(StrEnum):
    """Signal severity levels."""

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class SignalStatus(StrEnum):
    """Signal lifecycle states."""

    NEW = "new"
    UNDER_REVIEW = "under_review"
    ESCALATED = "escalated"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class Watermark(_FrozenModel):
    """Source watermark — 表示数据源在某个时间点的状态快照。

    用于 source_delta trigger 的变更检测和 Signal 的溯源。
    """

    source_id: UUID
    field_name: str = Field(min_length=1)
    value: Any = None
    captured_at: datetime


class DataQualityResult(_FrozenModel):
    """Result of data quality checks applied to a snapshot.

    每个 Signal 都必须经过 data quality 检查——所有路径必须经过
    data quality、Evidence/falsification、dedupe 和人类 triage。
    """

    check_name: str = Field(min_length=1)
    passed: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    row_count: int = Field(ge=0, default=0)
    schema_version: str = Field(default="1")


class NegativeProbe(_FrozenModel):
    """ADR-004: Typed negative probe for sequential falsification.

    X 必须是可由 detector / query / retrieval 独立求值的断言，
    不得是自由文本。断言归约为 typed 结构，求值一律由确定性组件完成。
    """

    probe_id: UUID
    metric: str = Field(min_length=1, description="Metric to evaluate")
    entity_scope: str = Field(min_length=1, description="Entity scope for the probe")
    window_hours: int = Field(ge=1, description="Time window in hours")
    comparator: str = Field(
        min_length=1,
        description="Comparison operator (e.g., 'gt', 'lt', 'eq', 'gte', 'lte')",
    )
    threshold: float = Field(description="Threshold value for the comparison")
    description: str = ""


class FalsificationResult(_FrozenModel):
    """ADR-004: Result of evaluating a single NegativeProbe.

    每个 probe 结果作为独立 EvidenceRef 附加，序贯累积证据并控制
    Type-I error。
    """

    probe: NegativeProbe
    passed: bool
    actual_value: float | None = None
    evaluated_at: datetime
    evaluation_method: str = Field(
        default="deterministic",
        description="Must be 'deterministic' — 模型只提出、不判定",
    )

    @field_validator("evaluated_at")
    @classmethod
    def _utc_evaluated(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class Signal(_FrozenModel):
    """Immutable signal produced by the Discover pipeline.

    Signal is an immutable linked version: it records which program version,
    detector pack, data quality results, and watermarks produced it.
    Once created, a Signal cannot be modified — new information creates
    a new linked Signal.
    """

    id: UUID
    program_version_id: UUID
    detector_pack_id: UUID
    detector_pack_version: int = Field(ge=1)
    severity: SignalSeverity
    status: SignalStatus = SignalStatus.NEW
    title: str = Field(min_length=1)
    description: str = ""
    source_watermarks: tuple[Watermark, ...] = Field(default_factory=tuple)
    data_quality_results: tuple[DataQualityResult, ...] = Field(default_factory=tuple)
    falsification_results: tuple[FalsificationResult, ...] = Field(default_factory=tuple)
    affected_entities: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_signal_id: UUID | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_created(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class SignalChain(_FrozenModel):
    """Immutable linked chain of Signals.

    当 Signal 被更新（如状态变更、追加 falsification 结果）时，
    不修改原 Signal，而是创建新的 linked Signal。SignalChain 追踪
    这条链的完整历史。
    """

    root_signal_id: UUID
    chain: tuple[UUID, ...] = Field(min_length=1)
    latest_signal_id: UUID
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_chain(cls, value: datetime) -> datetime:
        return ensure_utc(value)
