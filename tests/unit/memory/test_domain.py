"""S7-T1 RED: MemoryRecord and lifecycle domain models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from zhiwei.contracts.identifiers import new_id
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    RetentionPolicy,
    SensitivityLevel,
    SourceRef,
)


def _make_record(**overrides: object) -> MemoryRecord:
    defaults = {
        "id": new_id(),
        "version": 1,
        "organization_id": new_id(),
        "workspace_id": new_id(),
        "scope": MemoryScope.USER,
        "scope_subject_id": new_id(),
        "type": MemoryType.PREFERENCE,
        "subject": "vim keybindings",
        "key": "editor.vim_mode",
        "canonical_value": "enabled",
        "source_refs": (),
        "observed_at": datetime(2025, 1, 1, tzinfo=UTC),
        "confidence": 0.8,
        "sensitivity": SensitivityLevel.LOW,
        "status": MemoryStatus.CANDIDATE,
        "author_ref": new_id(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return MemoryRecord(**defaults)


def _make_source(source_id: str = "src-1", source_type: str = "run") -> SourceRef:
    return SourceRef(source_id=source_id, source_type=source_type)


# ---- MemoryStatus tests ----


class TestMemoryStatus:
    def test_all_statuses_defined(self) -> None:
        assert {s.value for s in MemoryStatus} == {
            "candidate",
            "confirmed",
            "superseded",
            "revoked",
            "expired",
        }


# ---- MemoryScope tests ----


class TestMemoryScope:
    def test_all_scopes_defined(self) -> None:
        assert {s.value for s in MemoryScope} == {"user", "team", "case"}


# ---- MemoryType tests ----


class TestMemoryType:
    def test_all_types_defined(self) -> None:
        assert {t.value for t in MemoryType} == {
            "preference",
            "fact",
            "decision",
            "episode",
            "lesson",
        }


# ---- SensitivityLevel tests ----


class TestSensitivityLevel:
    def test_all_levels_defined(self) -> None:
        assert {s.value for s in SensitivityLevel} == {"low", "medium", "high"}


# ---- RetentionPolicy tests ----


class TestRetentionPolicy:
    def test_default_ttl_is_30_days(self) -> None:
        policy = RetentionPolicy()
        assert policy.candidate_ttl == timedelta(days=30)

    def test_custom_ttl(self) -> None:
        policy = RetentionPolicy(candidate_ttl=timedelta(days=7))
        assert policy.candidate_ttl == timedelta(days=7)

    def test_candidate_not_expired_within_ttl(self) -> None:
        policy = RetentionPolicy()
        created = datetime(2025, 1, 1, tzinfo=UTC)
        now = datetime(2025, 1, 15, tzinfo=UTC)
        assert not policy.is_candidate_expired(created, now)

    def test_candidate_expired_after_ttl(self) -> None:
        policy = RetentionPolicy()
        created = datetime(2025, 1, 1, tzinfo=UTC)
        now = datetime(2025, 2, 15, tzinfo=UTC)
        assert policy.is_candidate_expired(created, now)

    def test_candidate_expired_at_boundary(self) -> None:
        policy = RetentionPolicy(candidate_ttl=timedelta(days=1))
        created = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2025, 1, 2, 0, 0, 1, tzinfo=UTC)
        assert policy.is_candidate_expired(created, now)


# ---- SourceRef tests ----


class TestSourceRef:
    def test_creation(self) -> None:
        ref = _make_source()
        assert ref.source_id == "src-1"
        assert ref.source_type == "run"

    def test_frozen(self) -> None:
        ref = _make_source()
        with pytest.raises(ValidationError):
            ref.source_id = "changed"  # type: ignore[misc]


# ---- MemoryRecord tests ----


class TestMemoryRecord:
    def test_creation_with_valid_fields(self) -> None:
        record = _make_record()
        assert record.status == MemoryStatus.CANDIDATE
        assert record.version == 1
        assert record.scope == MemoryScope.USER
        assert record.type == MemoryType.PREFERENCE

    def test_frozen_model_rejects_field_mutation(self) -> None:
        record = _make_record()
        with pytest.raises(ValidationError):
            record.subject = "changed"  # type: ignore[misc]

    def test_rejects_empty_subject(self) -> None:
        with pytest.raises(ValidationError):
            _make_record(subject="")

    def test_rejects_empty_key(self) -> None:
        with pytest.raises(ValidationError):
            _make_record(key="")

    def test_rejects_negative_version(self) -> None:
        with pytest.raises(ValidationError):
            _make_record(version=0)

    def test_rejects_out_of_range_confidence(self) -> None:
        with pytest.raises(ValidationError):
            _make_record(confidence=1.5)

    def test_rejects_negative_confidence(self) -> None:
        with pytest.raises(ValidationError):
            _make_record(confidence=-0.1)

    def test_status_transitions_via_model_copy(self) -> None:
        candidate = _make_record()
        confirmed = candidate.model_copy(
            update={"status": MemoryStatus.CONFIRMED, "approver_ref": new_id()}
        )
        assert confirmed.status == MemoryStatus.CONFIRMED
        superseded = confirmed.model_copy(
            update={"status": MemoryStatus.SUPERSEDED, "superseded_by": new_id()}
        )
        assert superseded.status == MemoryStatus.SUPERSEDED

    def test_is_active_for_candidate_and_confirmed(self) -> None:
        candidate = _make_record(status=MemoryStatus.CANDIDATE)
        assert candidate.is_active()
        confirmed = candidate.model_copy(update={"status": MemoryStatus.CONFIRMED})
        assert confirmed.is_active()

    def test_is_not_active_for_terminal_statuses(self) -> None:
        for status in (MemoryStatus.SUPERSEDED, MemoryStatus.REVOKED, MemoryStatus.EXPIRED):
            record = _make_record(status=status)
            assert not record.is_active()

    def test_terminal_status_true_for_terminal(self) -> None:
        for status in (MemoryStatus.SUPERSEDED, MemoryStatus.REVOKED, MemoryStatus.EXPIRED):
            record = _make_record(status=status)
            assert record.terminal_status()

    def test_terminal_status_false_for_active(self) -> None:
        for status in (MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED):
            record = _make_record(status=status)
            assert not record.terminal_status()

    def test_dedup_key_deterministic(self) -> None:
        record = _make_record()
        key1 = record.dedup_key()
        key2 = record.dedup_key()
        assert key1 == key2
        assert len(key1) == 7

    def test_dedup_key_normalizes_key(self) -> None:
        org_id = new_id()
        ws_id = new_id()
        subj_id = new_id()
        record_upper = _make_record(
            key="EDITOR.VIM_MODE",
            organization_id=org_id,
            workspace_id=ws_id,
            scope_subject_id=subj_id,
        )
        record_lower = _make_record(
            key="editor.vim_mode",
            organization_id=org_id,
            workspace_id=ws_id,
            scope_subject_id=subj_id,
        )
        assert record_upper.dedup_key() == record_lower.dedup_key()

    def test_dedup_key_includes_all_fields(self) -> None:
        org_id = new_id()
        ws_id = new_id()
        subj_id = new_id()
        record = _make_record(
            organization_id=org_id,
            workspace_id=ws_id,
            scope=MemoryScope.TEAM,
            scope_subject_id=subj_id,
            type=MemoryType.DECISION,
            subject="code style",
            key="style.python.ruff",
        )
        key = record.dedup_key()
        assert key[0] == str(org_id)
        assert key[1] == str(ws_id)
        assert key[2] == "team"
        assert key[3] == str(subj_id)
        assert key[4] == "decision"
        assert key[5] == "code style"
        assert key[6] == "style.python.ruff"

    def test_dedup_hash_is_computed(self) -> None:
        record = _make_record()
        assert record.dedup_hash.startswith("sha256:")

    def test_dedup_hash_deterministic(self) -> None:
        record = _make_record()
        assert record.dedup_hash == record.dedup_hash

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            MemoryRecord(
                id=new_id(),
                version=1,
                organization_id=new_id(),
                workspace_id=new_id(),
                scope=MemoryScope.USER,
                scope_subject_id=new_id(),
                type=MemoryType.PREFERENCE,
                subject="test",
                key="test.key",
                canonical_value="val",
                observed_at=datetime(2025, 1, 1, tzinfo=UTC),
                author_ref=new_id(),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
                unknown_field="bad",  # type: ignore[call-arg]
            )

    def test_source_refs_can_be_populated(self) -> None:
        record = _make_record(
            source_refs=(
                _make_source("src-1", "run"),
                _make_source("src-2", "knowledge"),
            )
        )
        assert len(record.source_refs) == 2
        assert record.source_refs[0].source_id == "src-1"

    def test_tombstone_default_false(self) -> None:
        record = _make_record()
        assert record.tombstone is False

    def test_tombstone_settable(self) -> None:
        record = _make_record(tombstone=True)
        assert record.tombstone is True
