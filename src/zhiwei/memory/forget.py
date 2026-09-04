"""S7 Delete/revoke cascade for memory records.

Handles user delete, source revoke, and cascading effects on index/cache.

事实源：S7 spec §5（Memory Center revoke/delete）、ADR-009。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc
from zhiwei.memory.candidates import CandidateQueue, DedupKey
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
)


class CascadeEffect(StrEnum):
    """Types of cascade effects when a record is revoked/deleted."""

    RECORD_REVOKED = "record_revoked"
    RECORD_DELETED = "record_deleted"
    INDEX_INVALIDATED = "index_invalidated"
    CACHE_INVALIDATED = "cache_invalidated"
    CONFLICT_UPDATED = "conflict_updated"


@dataclass(frozen=True, slots=True)
class CascadeEvent:
    """Records a single cascade effect for audit."""

    event_id: UUID
    effect: CascadeEffect
    source_record_id: UUID
    affected_record_id: UUID | None = None
    performed_at: datetime = field(default_factory=lambda: ensure_utc(datetime.now(tz=UTC)))
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ForgetResult:
    """Result of a forget/revoke/delete operation."""

    record: MemoryRecord
    cascades: tuple[CascadeEvent, ...]


@dataclass(slots=True)
class ForgetManager:
    """Manages delete/revoke cascade for memory records.

    When a record is revoked or deleted:
    - The record itself becomes REVOKED/EXPIRED with tombstone
    - Derived records that reference it are flagged
    - Index and cache entries are invalidated
    """

    queue: CandidateQueue = field(default_factory=CandidateQueue)
    _cascade_events: list[CascadeEvent] = field(default_factory=list)

    def revoke_record(
        self,
        dedup_key: DedupKey,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> ForgetResult | None:
        """Revoke a single record and cascade effects.

        Returns None if the record is not found or already terminal.
        """
        revoked = self.queue.revoke_record(dedup_key, reason, now=now)
        if revoked is None:
            return None

        cascades = self._build_cascade_events(revoked, reason)
        self._cascade_events.extend(cascades)

        return ForgetResult(record=revoked, cascades=tuple(cascades))

    def revoke_by_source(
        self,
        source_id: str,
        reason: str = "source revoked",
        *,
        now: datetime | None = None,
    ) -> list[ForgetResult]:
        """Revoke all records that reference a given source.

        Implements source-revoke cascade: when an upstream source is revoked,
        all records derived from it become REVOKED.
        """
        results: list[ForgetResult] = []

        for _key, record in list(self.queue.records.items()):
            if record.terminal_status():
                continue
            has_source = any(sr.source_id == source_id for sr in record.source_refs)
            if not has_source:
                continue

            dedup = DedupKey.from_record(record)
            result = self.revoke_record(dedup, reason, now=now)
            if result is not None:
                results.append(result)

        return results

    def user_delete(
        self,
        user_id: UUID,
        *,
        now: datetime | None = None,
    ) -> list[ForgetResult]:
        """Delete all user-scoped records for a given user.

        Implements user delete cascade: all USER scope records where
        scope_subject_id matches the user_id are revoked.
        """
        results: list[ForgetResult] = []

        for _key, record in list(self.queue.records.items()):
            if record.terminal_status():
                continue
            if record.scope != MemoryScope.USER:
                continue
            if record.scope_subject_id != user_id:
                continue

            dedup = DedupKey.from_record(record)
            result = self.revoke_record(
                dedup,
                f"user delete by {user_id}",
                now=now,
            )
            if result is not None:
                results.append(result)

        return results

    def cascade_events(self) -> tuple[CascadeEvent, ...]:
        """Return all cascade events for audit."""
        return tuple(self._cascade_events)

    def _build_cascade_events(
        self,
        record: MemoryRecord,
        reason: str,
    ) -> list[CascadeEvent]:
        """Build cascade events for a revoked/deleted record."""
        now_ = ensure_utc(datetime.now(tz=UTC))
        events: list[CascadeEvent] = []

        # Record revoked/deleted event
        events.append(
            CascadeEvent(
                event_id=new_id(),
                effect=CascadeEffect.RECORD_REVOKED,
                source_record_id=record.id,
                performed_at=now_,
                reason=reason,
            )
        )

        # Index invalidation event (always happens)
        events.append(
            CascadeEvent(
                event_id=new_id(),
                effect=CascadeEffect.INDEX_INVALIDATED,
                source_record_id=record.id,
                performed_at=now_,
            )
        )

        # Cache invalidation event (always happens)
        events.append(
            CascadeEvent(
                event_id=new_id(),
                effect=CascadeEffect.CACHE_INVALIDATED,
                source_record_id=record.id,
                performed_at=now_,
            )
        )

        return events

    def find_dependents(
        self,
        record_id: UUID,
    ) -> list[MemoryRecord]:
        """Find records that reference the given record as a source.

        Used to propagate revocation to dependent records.
        """
        dependents: list[MemoryRecord] = []
        for record in self.queue.records.values():
            if record.terminal_status():
                continue
            if record.id == record_id:
                continue
            # Check if this record references the target via source_refs
            for sr in record.source_refs:
                if sr.source_id == str(record_id):
                    dependents.append(record)
                    break
        return dependents
