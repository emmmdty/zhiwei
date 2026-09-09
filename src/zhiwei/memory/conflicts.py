"""S7 Temporal conflict detection and resolution.

Same key with different temporal/subject records coexist;
correction creates superseding version; unresolved conflicts projected as conflict.

事实源：S7 spec §4、ADR-009。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from zhiwei.contracts.time import ensure_utc
from zhiwei.memory.candidates import CandidateQueue, DedupKey
from zhiwei.memory.domain import (
    MemoryRecord,
)


class ConflictKind(StrEnum):
    """Type of temporal conflict between records."""

    TEMPORAL = "temporal"
    SUBJECT = "subject"
    VALUE = "value"


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """Describes a detected conflict between two records."""

    conflict_id: UUID
    kind: ConflictKind
    record_a_id: UUID
    record_b_id: UUID
    dedup_key: tuple[str, str, str, str, str, str, str]
    detected_at: datetime
    resolved: bool = False
    resolved_by: UUID | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True)
class ConflictDetector:
    """Detects temporal and subject conflicts across memory records.

    Conflicts are tracked explicitly; no silent overwrite.
    """

    _conflicts: list[ConflictRecord] = field(default_factory=list)

    @staticmethod
    def _temporal_overlap(existing: MemoryRecord, incoming: MemoryRecord) -> bool:
        """Check if two records have overlapping temporal ranges."""
        if existing.valid_from is None or existing.valid_to is None:
            return False
        if incoming.valid_from is None or incoming.valid_to is None:
            return False
        return existing.valid_from < incoming.valid_to and incoming.valid_from < existing.valid_to

    def detect_temporal_conflict(
        self,
        existing: MemoryRecord,
        incoming: MemoryRecord,
    ) -> ConflictRecord | None:
        """Detect if incoming record conflicts with existing on time range.

        Two records share a dedup key but have overlapping or disjoint valid_from/valid_to
        ranges with different canonical_values.
        """
        if existing.id == incoming.id:
            return None

        dedup_existing = existing.dedup_key()
        dedup_incoming = incoming.dedup_key()

        if dedup_existing != dedup_incoming:
            return None

        if existing.canonical_value == incoming.canonical_value:
            return None

        # Check temporal overlap
        if self._temporal_overlap(existing, incoming):
            conflict = ConflictRecord(
                conflict_id=UUID(int=0),  # placeholder, set below
                kind=ConflictKind.TEMPORAL,
                record_a_id=existing.id,
                record_b_id=incoming.id,
                dedup_key=dedup_existing,
                detected_at=ensure_utc(datetime.now(tz=UTC)),
            )
            self._conflicts.append(conflict)
            return conflict

        # Different values with same key and valid_from timestamps: competing
        # claims about the same entity.  Skip if both have closed, non-overlapping
        # ranges (different time periods are not conflicts).
        if existing.valid_from is not None and incoming.valid_from is not None:
            disjoint = False
            if existing.valid_to is not None and incoming.valid_to is not None:
                disjoint = (
                    existing.valid_to <= incoming.valid_from
                    or incoming.valid_to <= existing.valid_to
                )
            if not disjoint:
                conflict = ConflictRecord(
                    conflict_id=UUID(int=0),
                    kind=ConflictKind.TEMPORAL,
                    record_a_id=existing.id,
                    record_b_id=incoming.id,
                    dedup_key=dedup_existing,
                    detected_at=ensure_utc(datetime.now(tz=UTC)),
                )
                self._conflicts.append(conflict)
                return conflict

        return None

    def detect_subject_conflict(
        self,
        existing: MemoryRecord,
        incoming: MemoryRecord,
    ) -> ConflictRecord | None:
        """Detect subject-level conflict: same everything except subject text.

        Since subject is part of the dedup key, this is a defense-in-depth check.
        Two records with different dedup keys coexist; this catches any anomaly
        where dedup keys match but subject fields somehow differ.
        """
        if existing.id == incoming.id:
            return None

        dedup_existing = existing.dedup_key()
        dedup_incoming = incoming.dedup_key()

        # Full dedup key matches but subject field differs — anomaly
        if dedup_existing == dedup_incoming and existing.subject != incoming.subject:
            conflict = ConflictRecord(
                conflict_id=UUID(int=0),
                kind=ConflictKind.SUBJECT,
                record_a_id=existing.id,
                record_b_id=incoming.id,
                dedup_key=dedup_existing,
                detected_at=ensure_utc(datetime.now(tz=UTC)),
            )
            self._conflicts.append(conflict)
            return conflict

        return None

    def detect_value_conflict(
        self,
        existing: MemoryRecord,
        incoming: MemoryRecord,
    ) -> ConflictRecord | None:
        """Detect if incoming record has a different canonical_value for same dedup key."""
        if existing.id == incoming.id:
            return None

        dedup_existing = existing.dedup_key()
        dedup_incoming = incoming.dedup_key()

        if dedup_existing != dedup_incoming:
            return None

        if existing.canonical_value == incoming.canonical_value:
            return None

        conflict = ConflictRecord(
            conflict_id=UUID(int=0),
            kind=ConflictKind.VALUE,
            record_a_id=existing.id,
            record_b_id=incoming.id,
            dedup_key=dedup_existing,
            detected_at=ensure_utc(datetime.now(tz=UTC)),
        )
        self._conflicts.append(conflict)
        return conflict

    def get_unresolved_conflicts(
        self,
        dedup_key: tuple[str, str, str, str, str, str, str] | None = None,
    ) -> list[ConflictRecord]:
        """Return all unresolved conflicts, optionally filtered by dedup key."""
        if dedup_key is None:
            return [c for c in self._conflicts if not c.resolved]
        return [
            c for c in self._conflicts
            if not c.resolved and c.dedup_key == dedup_key
        ]

    def resolve_conflict(
        self,
        conflict_id: UUID,
        resolver_id: UUID,
    ) -> ConflictRecord | None:
        """Mark a conflict as resolved."""
        for conflict in self._conflicts:
            if conflict.conflict_id == conflict_id:
                resolved = ConflictRecord(
                    conflict_id=conflict.conflict_id,
                    kind=conflict.kind,
                    record_a_id=conflict.record_a_id,
                    record_b_id=conflict.record_b_id,
                    dedup_key=conflict.dedup_key,
                    detected_at=conflict.detected_at,
                    resolved=True,
                    resolved_by=resolver_id,
                    resolved_at=ensure_utc(datetime.now(tz=UTC)),
                )
                self._conflicts = [
                    c if c.conflict_id != conflict_id else resolved
                    for c in self._conflicts
                ]
                return resolved
        return None


@dataclass(slots=True)
class ConflictResolver:
    """Resolves conflicts via superseding versions.

    Correction creates a superseding version; the old record becomes SUPERSEDED.
    """

    queue: CandidateQueue
    detector: ConflictDetector

    def correct_record(
        self,
        existing_key: DedupKey,
        correction: MemoryRecord,
        *,
        now: datetime | None = None,
    ) -> tuple[MemoryRecord, MemoryRecord]:
        """Create a superseding version of an existing record.

        Returns (superseded_old, confirmed_new).
        """
        superseded, confirmed = self.queue.supersede_record(
            existing_key, correction, now=now,
        )

        # Mark any related conflicts as resolved
        key_tuple = existing_key.as_tuple()
        for conflict in self.detector.get_unresolved_conflicts(key_tuple):
            self.detector.resolve_conflict(conflict.conflict_id, correction.author_ref)

        return superseded, confirmed

    def project_conflicts(
        self,
        dedup_key: tuple[str, str, str, str, str, str, str],
    ) -> list[ConflictRecord]:
        """Project unresolved conflicts for a given dedup key.

        These must be visible to callers; no silent overwrite.
        """
        return self.detector.get_unresolved_conflicts(dedup_key)


@dataclass(slots=True)
class TemporalConflictManager:
    """High-level manager combining detection, resolution, and projection.

    Ensures:
    - Same key different temporal/subject records coexist
    - Correction creates superseding version
    - Unresolved conflicts projected as conflict, not silent overwrite
    """

    queue: CandidateQueue = field(default_factory=CandidateQueue)
    detector: ConflictDetector = field(default_factory=ConflictDetector)
    resolver: ConflictResolver = field(default=None, init=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.resolver = ConflictResolver(queue=self.queue, detector=self.detector)

    def process_incoming(
        self,
        incoming: MemoryRecord,
    ) -> MemoryRecord:
        """Process an incoming record, detecting conflicts with existing records.

        Returns the incoming record (as candidate or confirmed).
        If conflicts exist, they are tracked but not silently resolved.
        """
        dedup = DedupKey.from_record(incoming)
        existing = self.queue.get_record(dedup)

        if existing is not None and existing.is_active():
            # Detect all conflict types
            self.detector.detect_value_conflict(existing, incoming)
            self.detector.detect_subject_conflict(existing, incoming)
            self.detector.detect_temporal_conflict(existing, incoming)

        return self.queue.add_candidate(incoming)
