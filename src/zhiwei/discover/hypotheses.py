"""S8 RiskHypothesis domain types with ADR-004 sequential falsification.

Hypothesis 包含支持/反证/缺失、affected entities、source watermark、
detector/analysis version、建议验证动作、owner/status；启发式 score 不称 probability。

序贯证伪机制（ADR-004）：
  → 生成 N 个 typed NegativeProbe：「若此假设为假，应观察到 X」
  → X 必须是可由 detector / query / retrieval 独立求值的断言，不得是自由文本
  → 逐个执行，每个 probe 结果作为独立 EvidenceRef 附加
  → 序贯累积证据，控制 Type-I error
  → 未被推翻且证据充分 → 进入 human triage
  → 被推翻 → 记录并终止，保留完整证伪轨迹

三条硬约束：
  1. 职责分离：probe 的生成与求值由独立 task node 承担
  2. 模型只提出、不判定：求值一律由确定性组件完成
  3. 准入门槛：至少 N 个 negative probe 已执行且未推翻 → triage

事实源：specs/s8-discover-actions.md §4，ADR-004。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc
from zhiwei.discover.signals import FalsificationResult, NegativeProbe, Watermark


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class HypothesisStatus(StrEnum):
    """Hypothesis lifecycle states."""

    PROPOSED = "proposed"
    FALSIFICATION_IN_PROGRESS = "falsification_in_progress"
    READY_FOR_TRIAGE = "ready_for_triage"
    REJECTED = "rejected"
    IN_TRIAGE = "in_triage"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    SUPERSEDED = "superseded"


class HypothesisKind(StrEnum):
    """ADR-004: supporting / contradicting / missing evidence."""

    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"
    MISSING = "missing"


class EvidenceTag(_FrozenModel):
    """A single piece of supporting or contradicting evidence attached to a hypothesis."""

    tag_id: UUID
    kind: HypothesisKind
    description: str = Field(min_length=1)
    source_ref: str = ""
    created_at: datetime


class RiskHypothesis(_FrozenModel):
    """ADR-004: Immutable risk hypothesis with sequential falsification.

    Immutable linked version: 每个 Hypothesis 绑定到产生它的 Signal 和
    DetectorPack，不可变。新建信息创建新的 linked Hypothesis。
    """

    id: UUID
    signal_id: UUID
    program_version_id: UUID
    detector_pack_id: UUID
    detector_pack_version: int = Field(ge=1)
    kind: HypothesisKind = HypothesisKind.SUPPORTING
    title: str = Field(min_length=1)
    description: str = ""
    affected_entities: tuple[str, ...] = Field(default_factory=tuple)
    source_watermarks: tuple[Watermark, ...] = Field(default_factory=tuple)
    evidence_tags: tuple[EvidenceTag, ...] = Field(default_factory=tuple)
    proposed_probes: tuple[NegativeProbe, ...] = Field(default_factory=tuple)
    falsification_results: tuple[FalsificationResult, ...] = Field(default_factory=tuple)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    owner: str = ""
    suggested_validation_actions: tuple[str, ...] = Field(default_factory=tuple)
    score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Heuristic score (NOT a probability)",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_hypothesis_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc_hypothesis(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @property
    def falsification_coverage(self) -> float:
        """ADR-004: 已执行 probe 数 / 应执行 probe 数。"""
        total = len(self.proposed_probes)
        if total == 0:
            return 0.0
        executed = len(self.falsification_results)
        return min(executed / total, 1.0)

    @property
    def is_fully_falsified(self) -> bool:
        """True if any falsification result did NOT pass (probe refuted)."""
        return any(not r.passed for r in self.falsification_results)

    @property
    def min_probes_met(self) -> bool:
        """True if at least one probe has been executed and none refuted."""
        return len(self.falsification_results) > 0 and not self.is_fully_falsified

    def can_enter_triage(self, min_probes_required: int) -> bool:
        """ADR-004: hypothesis 只有在「至少 N 个 negative probe 已执行且未推翻」时
        才能进入 human triage 队列。
        """
        return (
            len(self.falsification_results) >= min_probes_required
            and not self.is_fully_falsified
        )

    def with_falsification_result(self, result: FalsificationResult) -> RiskHypothesis:
        """Return a new linked Hypothesis with the falsification result appended.

        不修改原 Hypothesis——新建 linked version。
        """
        new_results = (*self.falsification_results, result)
        return self.model_copy(
            update={
                "id": new_id(),
                "parent_hypothesis_id": self.id,
                "falsification_results": new_results,
                "updated_at": datetime.now(UTC),
            }
        )

    def with_status(self, status: HypothesisStatus) -> RiskHypothesis:
        """Return a new linked Hypothesis with updated status."""
        return self.model_copy(
            update={
                "id": new_id(),
                "parent_hypothesis_id": self.id,
                "status": status,
                "updated_at": datetime.now(UTC),
            }
        )


class FalsificationTracker:
    """ADR-004: Tracks the sequential falsification state of a hypothesis.

    Probe generation and evaluation are separated into independent task nodes.
    Model proposes, deterministic evaluation only.
    """

    def __init__(
        self,
        hypothesis: RiskHypothesis,
        min_probes_required: int,
    ) -> None:
        self._hypothesis = hypothesis
        self._min_probes_required = min_probes_required
        self._generated_probes: list[NegativeProbe] = list(hypothesis.proposed_probes)
        self._results: list[FalsificationResult] = list(hypothesis.falsification_results)

    @property
    def hypothesis(self) -> RiskHypothesis:
        return self._hypothesis

    @property
    def generated_probes(self) -> tuple[NegativeProbe, ...]:
        return tuple(self._generated_probes)

    @property
    def results(self) -> tuple[FalsificationResult, ...]:
        return tuple(self._results)

    @property
    def coverage(self) -> float:
        total = len(self._generated_probes)
        if total == 0:
            return 0.0
        return min(len(self._results) / total, 1.0)

    @property
    def is_refuted(self) -> bool:
        return any(not r.passed for r in self._results)

    @property
    def can_admit(self) -> bool:
        return (
            len(self._results) >= self._min_probes_required
            and not self.is_refuted
        )

    def add_probe(self, probe: NegativeProbe) -> None:
        """Record a generated probe (from independent task node)."""
        self._generated_probes.append(probe)

    def record_result(self, result: FalsificationResult) -> None:
        """Record a deterministic evaluation result."""
        self._results.append(result)

    def admission_check(self) -> tuple[bool, str]:
        """Check admission to triage queue.

        Returns (can_admit, reason).
        """
        if self.is_refuted:
            return False, "hypothesis refuted by falsification"
        if len(self._results) < self._min_probes_required:
            return (
                False,
                (
                    f"need {self._min_probes_required} probes, "
                    f"only {len(self._results)} executed"
                ),
            )
        return True, "admitted"


class HypothesisChain(_FrozenModel):
    """Immutable linked chain of Hypotheses.

    当 Hypothesis 被更新（如状态变更、追加 falsification 结果）时，
    不修改原 Hypothesis，而是创建新的 linked Hypothesis。
    """

    root_hypothesis_id: UUID
    chain: tuple[UUID, ...] = Field(min_length=1)
    latest_hypothesis_id: UUID
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_chain(cls, value: datetime) -> datetime:
        return ensure_utc(value)
