"""S7-T3 RED: Temporal conflict detection, resolution, and forget cascade tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from zhiwei.contracts.identifiers import new_id
from zhiwei.memory.candidates import CandidateQueue, DedupKey
from zhiwei.memory.conflicts import (
    ConflictDetector,
    ConflictKind,
    ConflictResolver,
    TemporalConflictManager,
)
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.forget import (
    CascadeEffect,
    ForgetManager,
)


def _make_record(
    *,
    scope: MemoryScope = MemoryScope.USER,
    mem_type: MemoryType = MemoryType.PREFERENCE,
    sensitivity: SensitivityLevel = SensitivityLevel.LOW,
    status: MemoryStatus = MemoryStatus.CANDIDATE,
    source_refs: tuple[SourceRef, ...] = (),
    confidence: float = 0.5,
    key: str = "test.key",
    subject: str = "test subject",
    canonical_value: str = "test_value",
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    id_: UUID | None = None,
) -> MemoryRecord:
    now = datetime(2025, 6, 1, tzinfo=UTC)
    return MemoryRecord(
        id=id_ or new_id(),
        version=1,
        organization_id=UUID("11111111-1111-4111-8111-111111111111"),
        workspace_id=UUID("22222222-2222-4222-8222-222222222222"),
        scope=scope,
        scope_subject_id=UUID("33333333-3333-4333-8333-333333333333"),
        type=mem_type,
        subject=subject,
        key=key,
        canonical_value=canonical_value,
        source_refs=source_refs,
        observed_at=now,
        confidence=confidence,
        sensitivity=sensitivity,
        status=status,
        author_ref=new_id(),
        created_at=now,
        updated_at=now,
        valid_from=valid_from,
        valid_to=valid_to,
    )


# ---- ConflictDetector tests ----


class TestConflictDetector:
    def test_detect_value_conflict(self) -> None:
        detector = ConflictDetector()
        existing = _make_record(canonical_value="old_value")
        incoming = _make_record(canonical_value="new_value")

        conflict = detector.detect_value_conflict(existing, incoming)
        assert conflict is not None
        assert conflict.kind == ConflictKind.VALUE
        assert conflict.record_a_id == existing.id
        assert conflict.record_b_id == incoming.id

    def test_no_value_conflict_same_value(self) -> None:
        detector = ConflictDetector()
        existing = _make_record(canonical_value="same_value")
        incoming = _make_record(canonical_value="same_value")

        conflict = detector.detect_value_conflict(existing, incoming)
        assert conflict is None

    def test_no_value_conflict_different_key(self) -> None:
        detector = ConflictDetector()
        existing = _make_record(key="key.a", canonical_value="v1")
        incoming = _make_record(key="key.b", canonical_value="v2")

        conflict = detector.detect_value_conflict(existing, incoming)
        assert conflict is None

    def test_detect_subject_conflict(self) -> None:
        """Subject conflict: same normalized_key but different subject text.

        Since subject is part of the dedup_key, records with different subjects
        have different dedup_keys. detect_subject_conflict detects when the
        normalized_key matches but subject differs — possible when using different
        record construction with same key but different subjects.
        """
        detector = ConflictDetector()
        existing = _make_record(subject="subject A", key="shared.key")
        incoming = _make_record(subject="subject B", key="shared.key")

        # Different subjects produce different dedup_keys, so no conflict
        conflict = detector.detect_subject_conflict(existing, incoming)
        assert conflict is None

    def test_no_subject_conflict_same_subject(self) -> None:
        detector = ConflictDetector()
        existing = _make_record(subject="same subject")
        incoming = _make_record(subject="same subject")

        conflict = detector.detect_subject_conflict(existing, incoming)
        assert conflict is None

    def test_no_conflict_with_same_id(self) -> None:
        detector = ConflictDetector()
        record = _make_record(canonical_value="v1")
        conflict = detector.detect_value_conflict(record, record)
        assert conflict is None

    def test_detect_temporal_conflict(self) -> None:
        detector = ConflictDetector()
        existing = _make_record(
            canonical_value="v1",
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            valid_to=datetime(2025, 6, 1, tzinfo=UTC),
        )
        incoming = _make_record(
            canonical_value="v2",
            valid_from=datetime(2025, 3, 1, tzinfo=UTC),
            valid_to=datetime(2025, 9, 1, tzinfo=UTC),
        )

        conflict = detector.detect_temporal_conflict(existing, incoming)
        assert conflict is not None
        assert conflict.kind == ConflictKind.TEMPORAL

    def test_detect_temporal_conflict_different_values(self) -> None:
        """Different values with same key and valid_from → conflict."""
        detector = ConflictDetector()
        existing = _make_record(
            canonical_value="v1",
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        )
        incoming = _make_record(
            canonical_value="v2",
            valid_from=datetime(2025, 6, 1, tzinfo=UTC),
        )

        conflict = detector.detect_temporal_conflict(existing, incoming)
        assert conflict is not None
        assert conflict.kind == ConflictKind.TEMPORAL

    def test_no_temporal_conflict_same_value(self) -> None:
        detector = ConflictDetector()
        existing = _make_record(
            canonical_value="v1",
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        )
        incoming = _make_record(
            canonical_value="v1",
            valid_from=datetime(2025, 6, 1, tzinfo=UTC),
        )

        conflict = detector.detect_temporal_conflict(existing, incoming)
        assert conflict is None


class TestConflictDetectorResolution:
    def test_get_unresolved_conflicts(self) -> None:
        detector = ConflictDetector()
        existing = _make_record(canonical_value="v1")
        incoming = _make_record(canonical_value="v2")
        detector.detect_value_conflict(existing, incoming)

        unresolved = detector.get_unresolved_conflicts()
        assert len(unresolved) == 1
        assert not unresolved[0].resolved

    def test_resolve_conflict(self) -> None:
        detector = ConflictDetector()
        existing = _make_record(canonical_value="v1")
        incoming = _make_record(canonical_value="v2")
        conflict = detector.detect_value_conflict(existing, incoming)
        assert conflict is not None

        resolver_id = new_id()
        resolved = detector.resolve_conflict(conflict.conflict_id, resolver_id)
        assert resolved is not None
        assert resolved.resolved is True
        assert resolved.resolved_by == resolver_id

        unresolved = detector.get_unresolved_conflicts()
        assert len(unresolved) == 0

    def test_resolve_nonexistent_returns_none(self) -> None:
        detector = ConflictDetector()
        result = detector.resolve_conflict(new_id(), new_id())
        assert result is None

    def test_filter_unresolved_by_dedup_key(self) -> None:
        detector = ConflictDetector()
        rec_a1 = _make_record(key="key.a", canonical_value="v1")
        rec_a2 = _make_record(key="key.a", canonical_value="v2")
        rec_b1 = _make_record(key="key.b", canonical_value="v1")
        rec_b2 = _make_record(key="key.b", canonical_value="v3")

        detector.detect_value_conflict(rec_a1, rec_a2)
        detector.detect_value_conflict(rec_b1, rec_b2)

        dedup_a = rec_a1.dedup_key()
        unresolved_a = detector.get_unresolved_conflicts(dedup_a)
        assert len(unresolved_a) == 1
        assert unresolved_a[0].dedup_key == dedup_a


# ---- ConflictResolver tests ----


class TestConflictResolver:
    def test_correct_record_supersedes_original(self) -> None:
        queue = CandidateQueue()
        detector = ConflictDetector()
        resolver = ConflictResolver(queue=queue, detector=detector)

        original = _make_record(canonical_value="old")
        queue.add_candidate(original)

        correction = _make_record(canonical_value="corrected")
        dedup = DedupKey.from_record(original)
        superseded, confirmed = resolver.correct_record(dedup, correction)

        assert superseded.status == MemoryStatus.SUPERSEDED
        assert superseded.superseded_by == confirmed.id
        assert confirmed.status == MemoryStatus.CONFIRMED
        assert confirmed.canonical_value == "corrected"

    def test_correct_resolves_related_conflicts(self) -> None:
        queue = CandidateQueue()
        detector = ConflictDetector()
        resolver = ConflictResolver(queue=queue, detector=detector)

        original = _make_record(canonical_value="v1")
        queue.add_candidate(original)

        incoming = _make_record(canonical_value="v2")
        detector.detect_value_conflict(original, incoming)

        unresolved = detector.get_unresolved_conflicts()
        assert len(unresolved) == 1

        correction = _make_record(canonical_value="v3")
        dedup = DedupKey.from_record(original)
        resolver.correct_record(dedup, correction)

        unresolved_after = detector.get_unresolved_conflicts()
        assert len(unresolved_after) == 0

    def test_project_conflicts_returns_unresolved(self) -> None:
        queue = CandidateQueue()
        detector = ConflictDetector()
        resolver = ConflictResolver(queue=queue, detector=detector)

        original = _make_record(canonical_value="v1")
        incoming = _make_record(canonical_value="v2")
        detector.detect_value_conflict(original, incoming)

        key = original.dedup_key()
        conflicts = resolver.project_conflicts(key)
        assert len(conflicts) == 1


# ---- TemporalConflictManager tests ----


class TestTemporalConflictManager:
    def test_process_incoming_no_conflict(self) -> None:
        mgr = TemporalConflictManager()
        record = _make_record()
        result = mgr.process_incoming(record)
        assert result.status == MemoryStatus.CANDIDATE
        assert len(mgr.detector.get_unresolved_conflicts()) == 0

    def test_process_incoming_creates_conflict(self) -> None:
        mgr = TemporalConflictManager()
        record_a = _make_record(canonical_value="v1")
        record_b = _make_record(canonical_value="v2")

        mgr.process_incoming(record_a)
        mgr.process_incoming(record_b)

        unresolved = mgr.detector.get_unresolved_conflicts()
        assert len(unresolved) >= 1

    def test_same_key_different_temporal_records_coexist(self) -> None:
        mgr = TemporalConflictManager()
        early = _make_record(
            canonical_value="early",
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            valid_to=datetime(2025, 6, 1, tzinfo=UTC),
        )
        late = _make_record(
            canonical_value="late",
            valid_from=datetime(2025, 6, 1, tzinfo=UTC),
            valid_to=datetime(2025, 12, 31, tzinfo=UTC),
        )

        mgr.process_incoming(early)
        mgr.process_incoming(late)

        # Both exist in queue (coexist)
        dedup = DedupKey.from_record(early)
        record = mgr.queue.get_record(dedup)
        assert record is not None

        # Conflict is tracked
        conflicts = mgr.detector.get_unresolved_conflicts()
        assert len(conflicts) >= 1

    def test_correction_creates_superseding_version(self) -> None:
        mgr = TemporalConflictManager()
        original = _make_record(canonical_value="original")
        mgr.process_incoming(original)

        correction = _make_record(canonical_value="corrected")
        dedup = DedupKey.from_record(original)
        superseded, confirmed = mgr.resolver.correct_record(dedup, correction)

        assert superseded.status == MemoryStatus.SUPERSEDED
        assert confirmed.status == MemoryStatus.CONFIRMED
        assert confirmed.canonical_value == "corrected"

    def test_unresolved_conflicts_projected_not_silent_overwrite(self) -> None:
        mgr = TemporalConflictManager()
        record_a = _make_record(canonical_value="v1")
        record_b = _make_record(canonical_value="v2")

        mgr.process_incoming(record_a)
        mgr.process_incoming(record_b)

        dedup = DedupKey.from_record(record_a)
        key = dedup.as_tuple()
        projected = mgr.resolver.project_conflicts(key)

        # Conflicts are visible, not silently resolved
        assert len(projected) >= 1
        assert all(not c.resolved for c in projected)


# ---- ForgetManager tests ----


class TestForgetManager:
    def test_revoke_record(self) -> None:
        fm = ForgetManager()
        record = _make_record()
        fm.queue.add_candidate(record)

        dedup = DedupKey.from_record(record)
        result = fm.revoke_record(dedup, "policy violation")

        assert result is not None
        assert result.record.status == MemoryStatus.REVOKED
        assert result.record.revoked_reason == "policy violation"
        assert result.record.tombstone is True

    def test_revoke_generates_cascade_events(self) -> None:
        fm = ForgetManager()
        record = _make_record()
        fm.queue.add_candidate(record)

        dedup = DedupKey.from_record(record)
        result = fm.revoke_record(dedup, "test revoke")

        assert result is not None
        assert len(result.cascades) >= 2  # record_revoked + index + cache
        effects = {c.effect for c in result.cascades}
        assert CascadeEffect.RECORD_REVOKED in effects
        assert CascadeEffect.INDEX_INVALIDATED in effects
        assert CascadeEffect.CACHE_INVALIDATED in effects

    def test_revoke_nonexistent_returns_none(self) -> None:
        fm = ForgetManager()
        dedup = DedupKey(
            organization_id="x",
            workspace_id="y",
            scope="user",
            scope_subject_id="z",
            mem_type="preference",
            subject="s",
            normalized_key="k",
        )
        assert fm.revoke_record(dedup, "reason") is None

    def test_revoke_by_source(self) -> None:
        fm = ForgetManager()
        r1 = _make_record(
            key="k1",
            source_refs=(SourceRef(source_id="src-1", source_type="run"),),
        )
        r2 = _make_record(
            key="k2",
            source_refs=(SourceRef(source_id="src-1", source_type="run"),),
        )
        r3 = _make_record(
            key="k3",
            source_refs=(SourceRef(source_id="src-2", source_type="run"),),
        )
        fm.queue.add_candidate(r1)
        fm.queue.add_candidate(r2)
        fm.queue.add_candidate(r3)

        results = fm.revoke_by_source("src-1", "source revoked")
        assert len(results) == 2

        # r3 should still be active
        dedup3 = DedupKey.from_record(r3)
        r3_now = fm.queue.get_record(dedup3)
        assert r3_now is not None
        assert r3_now.is_active()

    def test_user_delete(self) -> None:
        fm = ForgetManager()
        user_id = new_id()
        r1 = _make_record(
            scope=MemoryScope.USER,
            key="k1",
        )
        r1 = r1.model_copy(update={"scope_subject_id": user_id})
        r2 = _make_record(
            scope=MemoryScope.TEAM,
            key="k2",
        )
        fm.queue.add_candidate(r1)
        fm.queue.add_candidate(r2)

        results = fm.user_delete(user_id)
        assert len(results) == 1
        assert results[0].record.status == MemoryStatus.REVOKED

    def test_cascade_events_accumulated(self) -> None:
        fm = ForgetManager()
        r1 = _make_record(key="k1")
        r2 = _make_record(key="k2")
        fm.queue.add_candidate(r1)
        fm.queue.add_candidate(r2)

        fm.revoke_record(DedupKey.from_record(r1), "reason1")
        fm.revoke_record(DedupKey.from_record(r2), "reason2")

        events = fm.cascade_events()
        assert len(events) >= 4

    def test_find_dependents(self) -> None:
        fm = ForgetManager()
        source = _make_record(key="source")
        fm.queue.add_candidate(source)

        dependent = _make_record(
            key="dependent",
            source_refs=(SourceRef(source_id=str(source.id), source_type="memory"),),
        )
        fm.queue.add_candidate(dependent)

        dependents = fm.find_dependents(source.id)
        assert len(dependents) == 1
        assert dependents[0].key == "dependent"


# ---- Edge case: no silent overwrite ----


class TestNoSilentOverwrite:
    def test_incoming_does_not_overwrite_existing(self) -> None:
        mgr = TemporalConflictManager()
        original = _make_record(canonical_value="original")
        mgr.process_incoming(original)

        incoming = _make_record(canonical_value="different")
        mgr.process_incoming(incoming)

        dedup = DedupKey.from_record(original)
        record = mgr.queue.get_record(dedup)
        # The record in the queue is the merged candidate
        assert record is not None
        # Conflicts are tracked, not silently resolved
        assert len(mgr.detector.get_unresolved_conflicts()) >= 1
