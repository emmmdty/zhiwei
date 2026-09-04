"""S8-T5 Discover Case integration for the Discover pipeline.

DiscoverCase links hypotheses, actions, and resolutions in the Discover feed。
Feed shows status/owner/severity/supporting/contradicting/freshness/dedupe。
Triage → create Case → ask Ask for evidence → request tool action → approval → Resolution。
Resolution doesn't rewrite detector output。
Lesson candidate from resolution。

事实源：specs/s8-discover-actions.md §4、§6。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc
from zhiwei.discover.hypotheses import HypothesisKind, HypothesisStatus, RiskHypothesis
from zhiwei.discover.resolutions import LessonCandidate, Resolution
from zhiwei.discover.signals import SignalSeverity


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class DiscoverCaseStatus(StrEnum):
    """Discover Case lifecycle states."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    ARCHIVED = "archived"


class DiscoverCase(_FrozenModel):
    """A Discover-specific case that groups hypotheses, actions, and resolutions。

    与 S6 的 Case 分离——DiscoverCase 专用于 Discover pipeline，
    链接到 hypothesis、action requests、receipts 和 resolutions。
    """

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    title: str = Field(min_length=1)
    description: str = ""
    status: DiscoverCaseStatus = DiscoverCaseStatus.OPEN
    hypothesis_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    action_request_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    action_receipt_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    resolution_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    severity: SignalSeverity = SignalSeverity.INFO
    owner: str = ""
    dedup_key: str = Field(default="", description="Typed RiskFingerprint dedup key")
    created_by: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc_case(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class DiscoverFeedEntry(_FrozenModel):
    """A single entry in the Discover feed。

    Feed shows status/owner/severity/supporting/contradicting/freshness/dedupe。
    """

    id: UUID
    case_id: UUID
    hypothesis_id: UUID
    hypothesis_status: HypothesisStatus
    severity: SignalSeverity
    owner: str = ""
    title: str = Field(min_length=1)
    description: str = ""
    supporting_count: int = Field(ge=0, default=0)
    contradicting_count: int = Field(ge=0, default=0)
    missing_count: int = Field(ge=0, default=0)
    freshness_hours: float = Field(ge=0.0, default=0.0)
    dedup_key: str = ""
    dedup_duplicate: bool = False
    kind: HypothesisKind = HypothesisKind.SUPPORTING
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_feed(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class DiscoverCaseManager:
    """Manages Discover Case lifecycle and feed generation。

    Triage → create Case → ask Ask for evidence → request tool action → approval → Resolution。
    """

    def __init__(self) -> None:
        self._cases: dict[UUID, DiscoverCase] = {}
        self._feed_entries: dict[UUID, DiscoverFeedEntry] = {}

    @property
    def cases(self) -> tuple[DiscoverCase, ...]:
        return tuple(self._cases.values())

    @property
    def feed(self) -> tuple[DiscoverFeedEntry, ...]:
        return tuple(self._feed_entries.values())

    def create_case(
        self,
        hypothesis: RiskHypothesis,
        organization_id: UUID,
        workspace_id: UUID,
        created_by: str,
        *,
        title: str | None = None,
        description: str = "",
        severity: SignalSeverity | None = None,
        owner: str = "",
        dedup_key: str = "",
    ) -> DiscoverCase:
        """Create a Discover Case from a hypothesis。

        Feed entry is also created to reflect the case in the workbench.
        """
        now = datetime.now(UTC)
        case = DiscoverCase(
            id=new_id(),
            organization_id=organization_id,
            workspace_id=workspace_id,
            title=title or hypothesis.title,
            description=description or hypothesis.description,
            hypothesis_ids=(hypothesis.id,),
            severity=severity if severity is not None else SignalSeverity.INFO,
            owner=owner,
            dedup_key=dedup_key,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self._cases[case.id] = case

        feed_entry = self._build_feed_entry(case, hypothesis)
        self._feed_entries[feed_entry.id] = feed_entry
        return case

    def link_hypothesis(self, case_id: UUID, hypothesis: RiskHypothesis) -> DiscoverCase:
        """Link an additional hypothesis to an existing case."""
        case = self._get_case(case_id)
        if hypothesis.id in case.hypothesis_ids:
            return case
        updated = case.model_copy(
            update={
                "hypothesis_ids": (*case.hypothesis_ids, hypothesis.id),
                "updated_at": datetime.now(UTC),
            }
        )
        self._cases[case_id] = updated
        return updated

    def link_action_request(self, case_id: UUID, action_request_id: UUID) -> DiscoverCase:
        """Link an action request to a case."""
        case = self._get_case(case_id)
        if action_request_id in case.action_request_ids:
            return case
        updated = case.model_copy(
            update={
                "action_request_ids": (*case.action_request_ids, action_request_id),
                "updated_at": datetime.now(UTC),
            }
        )
        self._cases[case_id] = updated
        return updated

    def link_action_receipt(self, case_id: UUID, action_receipt_id: UUID) -> DiscoverCase:
        """Link an action receipt to a case."""
        case = self._get_case(case_id)
        if action_receipt_id in case.action_receipt_ids:
            return case
        updated = case.model_copy(
            update={
                "action_receipt_ids": (*case.action_receipt_ids, action_receipt_id),
                "updated_at": datetime.now(UTC),
            }
        )
        self._cases[case_id] = updated
        return updated

    def record_resolution(
        self, case_id: UUID, resolution: Resolution
    ) -> DiscoverCase:
        """Record a resolution against a case。

        Resolution doesn't rewrite detector output.
        """
        case = self._get_case(case_id)
        if resolution.id in case.resolution_ids:
            return case
        updated = case.model_copy(
            update={
                "resolution_ids": (*case.resolution_ids, resolution.id),
                "updated_at": datetime.now(UTC),
            }
        )
        self._cases[case_id] = updated
        return updated

    def resolve_case(
        self,
        case_id: UUID,
        resolution: Resolution,
        lesson_candidate: LessonCandidate | None = None,
    ) -> DiscoverCase:
        """Resolve a case with a resolution and optional lesson candidate。

        Resolution doesn't rewrite detector output。
        """
        case = self._record_resolution_internal(case_id, resolution)
        resolved = case.model_copy(
            update={
                "status": DiscoverCaseStatus.RESOLVED,
                "updated_at": datetime.now(UTC),
            }
        )
        self._cases[case_id] = resolved
        return resolved

    def dismiss_case(self, case_id: UUID) -> DiscoverCase:
        """Dismiss a case."""
        case = self._get_case(case_id)
        dismissed = case.model_copy(
            update={
                "status": DiscoverCaseStatus.DISMISSED,
                "updated_at": datetime.now(UTC),
            }
        )
        self._cases[case_id] = dismissed
        return dismissed

    def get_case(self, case_id: UUID) -> DiscoverCase:
        return self._get_case(case_id)

    def refresh_feed_entry(
        self, case_id: UUID, hypothesis: RiskHypothesis
    ) -> DiscoverFeedEntry:
        """Refresh a feed entry with updated hypothesis information。

        刷新/重试不会复制 hypothesis/case/action。
        """
        case = self._get_case(case_id)
        entry = self._build_feed_entry(case, hypothesis)
        self._feed_entries[entry.id] = entry
        return entry

    def _build_feed_entry(
        self, case: DiscoverCase, hypothesis: RiskHypothesis
    ) -> DiscoverFeedEntry:
        """Build a feed entry from case and hypothesis state。"""
        now = datetime.now(UTC)
        freshness = (now - hypothesis.created_at).total_seconds() / 3600.0

        supporting = sum(
            1 for t in hypothesis.evidence_tags if t.kind == HypothesisKind.SUPPORTING
        )
        contradicting = sum(
            1 for t in hypothesis.evidence_tags if t.kind == HypothesisKind.CONTRADICTING
        )
        missing = sum(
            1 for t in hypothesis.evidence_tags if t.kind == HypothesisKind.MISSING
        )

        return DiscoverFeedEntry(
            id=new_id(),
            case_id=case.id,
            hypothesis_id=hypothesis.id,
            hypothesis_status=hypothesis.status,
            severity=case.severity,
            owner=case.owner,
            title=hypothesis.title,
            description=hypothesis.description,
            supporting_count=supporting,
            contradicting_count=contradicting,
            missing_count=missing,
            freshness_hours=round(freshness, 2),
            dedup_key=case.dedup_key,
            kind=hypothesis.kind,
            score=hypothesis.score,
            created_at=now,
        )

    def _record_resolution_internal(
        self, case_id: UUID, resolution: Resolution
    ) -> DiscoverCase:
        case = self._get_case(case_id)
        if resolution.id in case.resolution_ids:
            return case
        return case.model_copy(
            update={
                "resolution_ids": (*case.resolution_ids, resolution.id),
                "updated_at": datetime.now(UTC),
            }
        )

    def _get_case(self, case_id: UUID) -> DiscoverCase:
        if case_id not in self._cases:
            raise ValueError(f"DiscoverCase {case_id} not found")
        return self._cases[case_id]
