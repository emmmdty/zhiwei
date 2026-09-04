"""S7 Candidate lifecycle and dedup (ADR-009).

Handles candidate deduplication, evidence merging, TTL expiry, and queue sorting.

事实源：ADR-009（Memory candidate 写入去重与队列收敛）、S7 spec §4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from zhiwei.contracts.time import ensure_utc
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryStatus,
    RetentionPolicy,
    SensitivityLevel,
)


@dataclass(frozen=True, slots=True)
class DedupKey:
    """ADR-009 dedup key: (org, workspace, scope, scope_subject, type, subject, normalized_key)."""

    organization_id: str
    workspace_id: str
    scope: str
    scope_subject_id: str
    mem_type: str
    subject: str
    normalized_key: str

    @classmethod
    def from_record(cls, record: MemoryRecord) -> DedupKey:
        """Extract dedup key from a MemoryRecord."""
        key_tuple = record.dedup_key()
        return cls(
            organization_id=key_tuple[0],
            workspace_id=key_tuple[1],
            scope=key_tuple[2],
            scope_subject_id=key_tuple[3],
            mem_type=key_tuple[4],
            subject=key_tuple[5],
            normalized_key=key_tuple[6],
        )

    def as_tuple(self) -> tuple[str, str, str, str, str, str, str]:
        """Serialize to immutable tuple for use as dict key."""
        return (
            self.organization_id,
            self.workspace_id,
            self.scope,
            self.scope_subject_id,
            self.mem_type,
            self.subject,
            self.normalized_key,
        )


@dataclass(slots=True)
class CandidateQueue:
    """In-memory candidate queue with dedup and TTL expiry.

    Implements ADR-009:
    - Same dedup key: merge evidence (append source_refs, update observed_at, boost confidence)
    - Auto-expire: candidate_ttl (default 30 days)
    - Queue sort: trigger_run_count × impact × sensitivity
    """

    records: dict[tuple[str, str, str, str, str, str, str], MemoryRecord] = field(
        default_factory=dict
    )
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    _trigger_counts: dict[UUID, int] = field(default_factory=dict)

    def add_candidate(
        self,
        record: MemoryRecord,
        *,
        now: datetime | None = None,
    ) -> MemoryRecord:
        """Add a candidate, merging evidence if dedup key already exists.

        ADR-009: 同键新 candidate 合并证据（追加 source_refs、更新 observed_at、
        提升 confidence），不新建记录。
        """
        if record.status != MemoryStatus.CANDIDATE:
            raise ValueError(f"only CANDIDATE records can be added, got {record.status}")

        dedup = DedupKey.from_record(record)
        key = dedup.as_tuple()
        now_ = ensure_utc(now) if now else ensure_utc(datetime.now(tz=record.created_at.tzinfo))

        if key in self.records:
            return self._merge_evidence(key, record, now_)
        self.records[key] = record
        self._trigger_counts[record.id] = self._trigger_counts.get(record.id, 0)
        return record

    def increment_trigger_count(self, record_id: UUID) -> None:
        """Increment the trigger run count for a record.

        Used by ADR-009 queue sort: trigger_run_count × impact × sensitivity.
        """
        self._trigger_counts[record_id] = self._trigger_counts.get(record_id, 0) + 1

    def _merge_evidence(
        self,
        key: tuple[str, str, str, str, str, str, str],
        new_record: MemoryRecord,
        now: datetime,
    ) -> MemoryRecord:
        """Merge evidence from new_record into existing record at key.

        追加 source_refs、更新 observed_at、提升 confidence。
        """
        existing = self.records[key]

        # Merge source_refs (append, preserving order, dedup by source_id)
        existing_source_ids = {sr.source_id for sr in existing.source_refs}
        new_sources = tuple(
            sr for sr in new_record.source_refs if sr.source_id not in existing_source_ids
        )
        merged_sources = existing.source_refs + new_sources

        # Update observed_at to the later of the two
        merged_observed = max(existing.observed_at, new_record.observed_at, key=ensure_utc)

        # Boost confidence: take the max of the two
        merged_confidence = max(existing.confidence, new_record.confidence)

        merged = existing.model_copy(
            update={
                "source_refs": merged_sources,
                "observed_at": merged_observed,
                "confidence": merged_confidence,
                "updated_at": now,
            }
        )
        self.records[key] = merged
        return merged

    def expire_candidates(self, now: datetime) -> list[MemoryRecord]:
        """Expire candidates that have exceeded their TTL.

        ADR-009: candidate 超过 candidate_ttl 未确认自动转 expired 并留 tombstone。
        """
        now_utc = ensure_utc(now)
        expired: list[MemoryRecord] = []

        for key, record in list(self.records.items()):
            if record.status != MemoryStatus.CANDIDATE:
                continue
            if self.retention.is_candidate_expired(record.created_at, now_utc):
                updated = record.model_copy(
                    update={
                        "status": MemoryStatus.EXPIRED,
                        "tombstone": True,
                        "updated_at": now_utc,
                    }
                )
                self.records[key] = updated
                expired.append(updated)

        return expired

    def confirm_candidate(
        self,
        dedup_key: DedupKey,
        approver_id: UUID,
        *,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        """Confirm a candidate record.

        Returns the updated record, or None if not found.
        """
        key = dedup_key.as_tuple()
        if key not in self.records:
            return None

        record = self.records[key]
        if record.status != MemoryStatus.CANDIDATE:
            return None

        now_ = ensure_utc(now) if now else ensure_utc(datetime.now(tz=record.created_at.tzinfo))
        confirmed = record.model_copy(
            update={
                "status": MemoryStatus.CONFIRMED,
                "approver_ref": approver_id,
                "updated_at": now_,
            }
        )
        self.records[key] = confirmed
        return confirmed

    def supersede_record(
        self,
        original_key: DedupKey,
        new_record: MemoryRecord,
        *,
        now: datetime | None = None,
    ) -> tuple[MemoryRecord, MemoryRecord]:
        """Create a superseding version of an existing record.

        The original record becomes SUPERSEDED, the new record becomes CONFIRMED.
        """
        key = original_key.as_tuple()
        if key not in self.records:
            raise KeyError("no record found for dedup key")

        original = self.records[key]
        now_ = ensure_utc(now) if now else ensure_utc(datetime.now(tz=new_record.created_at.tzinfo))

        superseded = original.model_copy(
            update={
                "status": MemoryStatus.SUPERSEDED,
                "superseded_by": new_record.id,
                "updated_at": now_,
            }
        )
        self.records[key] = superseded

        confirmed = new_record.model_copy(update={"status": MemoryStatus.CONFIRMED})
        new_dedup = DedupKey.from_record(new_record)
        self.records[new_dedup.as_tuple()] = confirmed

        return superseded, confirmed

    def revoke_record(
        self,
        dedup_key: DedupKey,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        """Revoke a record (candidate or confirmed).

        Sets status to REVOKED with tombstone.
        """
        key = dedup_key.as_tuple()
        if key not in self.records:
            return None

        record = self.records[key]
        if record.terminal_status():
            return None

        now_ = ensure_utc(now) if now else ensure_utc(datetime.now(tz=record.created_at.tzinfo))
        revoked = record.model_copy(
            update={
                "status": MemoryStatus.REVOKED,
                "revoked_reason": reason,
                "tombstone": True,
                "updated_at": now_,
            }
        )
        self.records[key] = revoked
        return revoked

    def sort_key(self, record: MemoryRecord) -> tuple[float, float, float]:
        """ADR-009 queue sort: trigger_run_count × impact × sensitivity.

        Returns a tuple for sorting in descending priority order (negated for reverse).
        """
        trigger_count = self._trigger_counts.get(record.id, 0)
        sensitivity_weight = {
            SensitivityLevel.LOW: 1.0,
            SensitivityLevel.MEDIUM: 2.0,
            SensitivityLevel.HIGH: 3.0,
        }.get(record.sensitivity, 1.0)
        # Impact approximated by source count and confidence
        impact = len(record.source_refs) * record.confidence

        return (-trigger_count, -impact, -sensitivity_weight)

    def sorted_candidates(self) -> list[MemoryRecord]:
        """Return active candidates sorted by ADR-009 priority."""
        candidates = [r for r in self.records.values() if r.status == MemoryStatus.CANDIDATE]
        return sorted(candidates, key=self.sort_key)

    def active_count(self) -> int:
        """Count of active (non-terminal) records."""
        return sum(1 for r in self.records.values() if r.is_active())

    def candidate_count(self) -> int:
        """Count of candidate records."""
        return sum(1 for r in self.records.values() if r.status == MemoryStatus.CANDIDATE)

    def get_record(self, dedup_key: DedupKey) -> MemoryRecord | None:
        """Retrieve a record by its dedup key."""
        return self.records.get(dedup_key.as_tuple())
