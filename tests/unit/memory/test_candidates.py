"""S7-T1 RED: Candidate lifecycle and dedup (ADR-009)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.memory.candidates import CandidateQueue, DedupKey
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    RetentionPolicy,
    SensitivityLevel,
    SourceRef,
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
) -> MemoryRecord:
    now = datetime(2025, 6, 1, tzinfo=UTC)
    return MemoryRecord(
        id=new_id(),
        version=1,
        organization_id=UUID("11111111-1111-4111-8111-111111111111"),
        workspace_id=UUID("22222222-2222-4222-8222-222222222222"),
        scope=scope,
        scope_subject_id=UUID("33333333-3333-4333-8333-333333333333"),
        type=mem_type,
        subject=subject,
        key=key,
        canonical_value="test_value",
        source_refs=source_refs,
        observed_at=now,
        confidence=confidence,
        sensitivity=sensitivity,
        status=status,
        author_ref=new_id(),
        created_at=now,
        updated_at=now,
    )


def _make_source(source_id: str = "src-1", source_type: str = "run") -> SourceRef:
    return SourceRef(source_id=source_id, source_type=source_type)


# ---- DedupKey tests ----


class TestDedupKey:
    def test_from_record(self) -> None:
        record = _make_record()
        dedup = DedupKey.from_record(record)
        assert dedup.scope == "user"
        assert dedup.mem_type == "preference"

    def test_as_tuple(self) -> None:
        record = _make_record()
        dedup = DedupKey.from_record(record)
        t = dedup.as_tuple()
        assert len(t) == 7
        assert isinstance(t, tuple)

    def test_equal_records_produce_equal_dedup_keys(self) -> None:
        record_a = _make_record()
        record_b = _make_record()
        key_a = DedupKey.from_record(record_a)
        key_b = DedupKey.from_record(record_b)
        # Different ids but same scope/type/key → same dedup key tuple values
        assert key_a.as_tuple()[2:] == key_b.as_tuple()[2:]


# ---- CandidateQueue tests ----


class TestCandidateQueue:
    def test_add_candidate(self) -> None:
        queue = CandidateQueue()
        record = _make_record()
        result = queue.add_candidate(record)
        assert result.id == record.id
        assert queue.candidate_count() == 1

    def test_add_non_candidate_raises(self) -> None:
        queue = CandidateQueue()
        record = _make_record(status=MemoryStatus.CONFIRMED)
        with pytest.raises(ValueError, match="only CANDIDATE"):
            queue.add_candidate(record)

    def test_dedup_merges_evidence(self) -> None:
        queue = CandidateQueue()
        record_a = _make_record(
            source_refs=(_make_source("src-1", "run"),),
            confidence=0.6,
        )
        queue.add_candidate(record_a)

        record_b = _make_record(
            source_refs=(_make_source("src-2", "knowledge"),),
            confidence=0.8,
        )
        merged = queue.add_candidate(record_b)

        assert queue.candidate_count() == 1
        assert len(merged.source_refs) == 2
        assert merged.confidence == 0.8

    def test_dedup_preserves_existing_source_refs(self) -> None:
        queue = CandidateQueue()
        record_a = _make_record(
            source_refs=(_make_source("src-1", "run"),),
        )
        queue.add_candidate(record_a)

        record_b = _make_record(
            source_refs=(_make_source("src-1", "run"),),  # duplicate source_id
        )
        merged = queue.add_candidate(record_b)
        assert len(merged.source_refs) == 1

    def test_dedup_updates_observed_at_to_later(self) -> None:
        queue = CandidateQueue()
        early = datetime(2025, 1, 1, tzinfo=UTC)
        late = datetime(2025, 6, 1, tzinfo=UTC)

        record_a = _make_record()
        record_a = record_a.model_copy(update={"observed_at": early})
        queue.add_candidate(record_a)

        record_b = _make_record()
        record_b = record_b.model_copy(update={"observed_at": late})
        merged = queue.add_candidate(record_b)
        assert merged.observed_at == late

    def test_dedup_different_keys_do_not_merge(self) -> None:
        queue = CandidateQueue()
        record_a = _make_record(key="key.a")
        record_b = _make_record(key="key.b")
        queue.add_candidate(record_a)
        queue.add_candidate(record_b)
        assert queue.candidate_count() == 2

    def test_expire_candidates(self) -> None:
        queue = CandidateQueue(retention=RetentionPolicy(candidate_ttl=timedelta(days=1)))
        old_time = datetime(2025, 1, 1, tzinfo=UTC)
        record = _make_record()
        record = record.model_copy(update={"created_at": old_time, "observed_at": old_time})
        queue.add_candidate(record)

        now = datetime(2025, 1, 5, tzinfo=UTC)
        expired = queue.expire_candidates(now)
        assert len(expired) == 1
        assert expired[0].status == MemoryStatus.EXPIRED
        assert expired[0].tombstone is True

    def test_expire_candidates_leaves_confirmed_alone(self) -> None:
        queue = CandidateQueue(retention=RetentionPolicy(candidate_ttl=timedelta(days=1)))
        old_time = datetime(2025, 1, 1, tzinfo=UTC)
        record = _make_record()
        record = record.model_copy(update={"created_at": old_time, "observed_at": old_time})
        queue.add_candidate(record)
        # Manually confirm by updating status in the internal dict
        dedup = DedupKey.from_record(record)
        key = dedup.as_tuple()
        queue.records[key] = queue.records[key].model_copy(
            update={"status": MemoryStatus.CONFIRMED}
        )

        now = datetime(2025, 1, 5, tzinfo=UTC)
        expired = queue.expire_candidates(now)
        assert len(expired) == 0

    def test_confirm_candidate(self) -> None:
        queue = CandidateQueue()
        record = _make_record()
        queue.add_candidate(record)

        dedup = DedupKey.from_record(record)
        approver_id = new_id()
        confirmed = queue.confirm_candidate(dedup, approver_id)
        assert confirmed is not None
        assert confirmed.status == MemoryStatus.CONFIRMED
        assert confirmed.approver_ref == approver_id

    def test_confirm_nonexistent_returns_none(self) -> None:
        queue = CandidateQueue()
        dedup = DedupKey(
            organization_id="x",
            workspace_id="y",
            scope="user",
            scope_subject_id="z",
            mem_type="preference",
            subject="s",
            normalized_key="k",
        )
        assert queue.confirm_candidate(dedup, new_id()) is None

    def test_revoke_record(self) -> None:
        queue = CandidateQueue()
        record = _make_record()
        queue.add_candidate(record)

        dedup = DedupKey.from_record(record)
        revoked = queue.revoke_record(dedup, "policy violation")
        assert revoked is not None
        assert revoked.status == MemoryStatus.REVOKED
        assert revoked.revoked_reason == "policy violation"
        assert revoked.tombstone is True

    def test_revoke_nonexistent_returns_none(self) -> None:
        queue = CandidateQueue()
        dedup = DedupKey(
            organization_id="x",
            workspace_id="y",
            scope="user",
            scope_subject_id="z",
            mem_type="preference",
            subject="s",
            normalized_key="k",
        )
        assert queue.revoke_record(dedup, "reason") is None

    def test_revoke_terminal_returns_none(self) -> None:
        queue = CandidateQueue()
        record = _make_record()
        queue.add_candidate(record)
        # Manually set terminal status in the internal dict
        dedup = DedupKey.from_record(record)
        key = dedup.as_tuple()
        queue.records[key] = queue.records[key].model_copy(update={"status": MemoryStatus.REVOKED})

        assert queue.revoke_record(dedup, "reason") is None

    def test_supersede_record(self) -> None:
        queue = CandidateQueue()
        original = _make_record()
        queue.add_candidate(original)

        new_record = _make_record(key="test.key", subject="test subject")
        original_dedup = DedupKey.from_record(original)
        superseded, confirmed = queue.supersede_record(original_dedup, new_record)

        assert superseded.status == MemoryStatus.SUPERSEDED
        assert confirmed.status == MemoryStatus.CONFIRMED

    def test_supersede_nonexistent_raises(self) -> None:
        queue = CandidateQueue()
        dedup = DedupKey(
            organization_id="x",
            workspace_id="y",
            scope="user",
            scope_subject_id="z",
            mem_type="preference",
            subject="s",
            normalized_key="k",
        )
        with pytest.raises(KeyError):
            queue.supersede_record(dedup, _make_record())

    def test_sorted_candidates_by_priority(self) -> None:
        queue = CandidateQueue()
        low = _make_record(key="low", sensitivity=SensitivityLevel.LOW, confidence=0.3)
        high = _make_record(
            key="high",
            sensitivity=SensitivityLevel.HIGH,
            confidence=0.9,
            source_refs=(_make_source("s1"), _make_source("s2"), _make_source("s3")),
        )
        medium = _make_record(key="medium", sensitivity=SensitivityLevel.MEDIUM, confidence=0.6)
        queue.add_candidate(low)
        queue.add_candidate(high)
        queue.add_candidate(medium)

        sorted_records = queue.sorted_candidates()
        assert sorted_records[0].sensitivity == SensitivityLevel.HIGH
        assert sorted_records[-1].sensitivity == SensitivityLevel.LOW

    def test_active_count(self) -> None:
        queue = CandidateQueue()
        queue.add_candidate(_make_record(status=MemoryStatus.CANDIDATE, key="a.active"))
        queue.add_candidate(_make_record(status=MemoryStatus.CANDIDATE, key="b.active"))
        assert queue.active_count() == 2

    def test_candidate_count(self) -> None:
        queue = CandidateQueue()
        queue.add_candidate(_make_record(key="a.count"))
        queue.add_candidate(_make_record(key="b.count"))
        assert queue.candidate_count() == 2

    def test_get_record(self) -> None:
        queue = CandidateQueue()
        record = _make_record()
        queue.add_candidate(record)
        dedup = DedupKey.from_record(record)
        found = queue.get_record(dedup)
        assert found is not None
        assert found.id == record.id


# ---- Queue convergence test (ADR-009 Gate condition) ----


class TestQueueConvergence:
    """S7 spec §7: 注入 N 个同键重复 candidate 的负载测试，
    断言待确认条目数不随 Run 数线性增长。"""

    def test_n_identical_candidates_produce_one_entry(self) -> None:
        queue = CandidateQueue()
        n = 100
        for _ in range(n):
            record = _make_record()
            queue.add_candidate(record)

        assert queue.candidate_count() == 1

    def test_merging_preserves_all_source_refs(self) -> None:
        queue = CandidateQueue()
        for i in range(50):
            record = _make_record(
                source_refs=(_make_source(f"src-{i}", "run"),),
            )
            queue.add_candidate(record)

        dedup = DedupKey.from_record(_make_record())
        merged = queue.get_record(dedup)
        assert merged is not None
        assert len(merged.source_refs) == 50

    def test_candidate_count_bounded_with_repeated_runs(self) -> None:
        """Core ADR-009 assertion: candidate count stays O(1) not O(N) with repeated keys."""
        queue = CandidateQueue()
        for run_idx in range(200):
            # Each run produces a candidate with the same dedup key
            record = _make_record(
                source_refs=(_make_source(f"run-{run_idx}", "run"),),
            )
            queue.add_candidate(record)

        assert queue.candidate_count() == 1

    def test_ttl_expiry_leaves_tombstone(self) -> None:
        queue = CandidateQueue(retention=RetentionPolicy(candidate_ttl=timedelta(days=1)))
        old_time = datetime(2025, 1, 1, tzinfo=UTC)
        record = _make_record()
        record = record.model_copy(update={"created_at": old_time, "observed_at": old_time})
        queue.add_candidate(record)

        expired_list = queue.expire_candidates(datetime(2025, 1, 5, tzinfo=UTC))
        assert len(expired_list) == 1
        assert expired_list[0].tombstone is True
        assert expired_list[0].status == MemoryStatus.EXPIRED

    def test_mixed_unique_and_duplicate_keys(self) -> None:
        queue = CandidateQueue()
        # 5 unique keys, each repeated 10 times
        for key_idx in range(5):
            for run_idx in range(10):
                record = _make_record(
                    key=f"unique.key.{key_idx}",
                    source_refs=(_make_source(f"src-{key_idx}-{run_idx}", "run"),),
                )
                queue.add_candidate(record)

        assert queue.candidate_count() == 5
        # Each should have 10 source_refs
        for key_idx in range(5):
            dedup = DedupKey.from_record(_make_record(key=f"unique.key.{key_idx}"))
            rec = queue.get_record(dedup)
            assert rec is not None
            assert len(rec.source_refs) == 10
