"""S7-T2 RED: Non-symmetric write policy — confirmation workflow tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.memory.candidates import DedupKey
from zhiwei.memory.confirmation import (
    ConfirmationAction,
    ConfirmationWorkflow,
)
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.policy import WriteForbiddenError


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
        canonical_value=canonical_value,
        source_refs=source_refs,
        observed_at=now,
        confidence=confidence,
        sensitivity=sensitivity,
        status=status,
        author_ref=new_id(),
        created_at=now,
        updated_at=now,
    )


# ---- Auto-confirm tests ----


class TestAutoConfirm:
    def test_low_risk_user_preference_auto_confirmed(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.USER,
            mem_type=MemoryType.PREFERENCE,
            sensitivity=SensitivityLevel.LOW,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CONFIRMED

    def test_case_episode_auto_confirmed(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.CASE,
            mem_type=MemoryType.EPISODE,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CONFIRMED

    def test_case_fact_auto_confirmed(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.CASE,
            mem_type=MemoryType.FACT,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CONFIRMED

    def test_case_decision_auto_confirmed(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.CASE,
            mem_type=MemoryType.DECISION,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CONFIRMED


# ---- Candidate (needs steward) tests ----


class TestCandidateRoute:
    def test_sensitive_user_preference_becomes_candidate(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.USER,
            mem_type=MemoryType.PREFERENCE,
            sensitivity=SensitivityLevel.MEDIUM,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CANDIDATE

    def test_high_sensitivity_user_preference_becomes_candidate(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.USER,
            mem_type=MemoryType.PREFERENCE,
            sensitivity=SensitivityLevel.HIGH,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CANDIDATE

    def test_team_preference_becomes_candidate(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.PREFERENCE,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CANDIDATE

    def test_team_decision_becomes_candidate(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.DECISION,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CANDIDATE

    def test_team_lesson_becomes_candidate(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.LESSON,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CANDIDATE

    def test_case_lesson_becomes_candidate(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.CASE,
            mem_type=MemoryType.LESSON,
        )
        result = wf.write_record(record)
        assert result.status == MemoryStatus.CANDIDATE


# ---- Forbidden content tests ----


class TestForbiddenContent:
    def test_secret_in_subject_forbidden(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(subject="my secret api key")
        with pytest.raises(WriteForbiddenError, match="forbidden"):
            wf.write_record(record)

    def test_hidden_reasoning_in_value_forbidden(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(canonical_value="this contains hidden reasoning about users")
        with pytest.raises(WriteForbiddenError, match="forbidden"):
            wf.write_record(record)

    def test_tool_instruction_in_value_forbidden(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(canonical_value="follow this tool instruction carefully")
        with pytest.raises(WriteForbiddenError, match="forbidden"):
            wf.write_record(record)

    def test_password_in_subject_forbidden(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(subject="password for admin")
        with pytest.raises(WriteForbiddenError, match="forbidden"):
            wf.write_record(record)

    def test_api_key_in_subject_forbidden(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(subject="api_key for service")
        with pytest.raises(WriteForbiddenError, match="forbidden"):
            wf.write_record(record)


# ---- Steward confirmation tests ----


class TestStewardConfirmation:
    def test_steward_confirms_candidate(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.PREFERENCE,
        )
        wf.write_record(record)

        dedup = DedupKey.from_record(record)
        steward_id = new_id()
        confirmed = wf.steward_confirm(dedup, steward_id)

        assert confirmed is not None
        assert confirmed.status == MemoryStatus.CONFIRMED
        assert confirmed.approver_ref == steward_id

    def test_steward_reject_candidate(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.DECISION,
        )
        wf.write_record(record)

        dedup = DedupKey.from_record(record)
        steward_id = new_id()
        rejected = wf.steward_reject(dedup, steward_id, reason="outdated convention")

        assert rejected is not None
        assert rejected.status == MemoryStatus.REVOKED
        assert rejected.revoked_reason == "outdated convention"

    def test_confirm_nonexistent_returns_none(self) -> None:
        wf = ConfirmationWorkflow()
        dedup = DedupKey(
            organization_id="x",
            workspace_id="y",
            scope="user",
            scope_subject_id="z",
            mem_type="preference",
            subject="s",
            normalized_key="k",
        )
        assert wf.steward_confirm(dedup, new_id()) is None

    def test_reject_nonexistent_returns_none(self) -> None:
        wf = ConfirmationWorkflow()
        dedup = DedupKey(
            organization_id="x",
            workspace_id="y",
            scope="user",
            scope_subject_id="z",
            mem_type="preference",
            subject="s",
            normalized_key="k",
        )
        assert wf.steward_reject(dedup, new_id(), "reason") is None


# ---- Audit log tests ----


class TestAuditLog:
    def test_confirm_logged(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(scope=MemoryScope.TEAM, mem_type=MemoryType.PREFERENCE)
        wf.write_record(record)

        steward_id = new_id()
        dedup = DedupKey.from_record(record)
        wf.steward_confirm(dedup, steward_id)

        log = wf.audit_log()
        assert len(log) == 1
        assert log[0].action == ConfirmationAction.CONFIRM
        assert log[0].actor_ref == steward_id

    def test_reject_logged(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(scope=MemoryScope.TEAM, mem_type=MemoryType.DECISION)
        wf.write_record(record)

        steward_id = new_id()
        dedup = DedupKey.from_record(record)
        wf.steward_reject(dedup, steward_id, reason="not aligned")

        log = wf.audit_log()
        assert len(log) == 1
        assert log[0].action == ConfirmationAction.REJECT
        assert log[0].reason == "not aligned"

    def test_audit_log_is_immutable_tuple(self) -> None:
        wf = ConfirmationWorkflow()
        log = wf.audit_log()
        assert isinstance(log, tuple)


# ---- needs_steward_confirmation tests ----


class TestNeedsStewardConfirmation:
    def test_team_record_needs_steward(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(scope=MemoryScope.TEAM, mem_type=MemoryType.PREFERENCE)
        assert wf.needs_steward_confirmation(record) is True

    def test_low_risk_user_preference_does_not_need_steward(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.USER,
            mem_type=MemoryType.PREFERENCE,
            sensitivity=SensitivityLevel.LOW,
        )
        assert wf.needs_steward_confirmation(record) is False

    def test_sensitive_user_preference_needs_steward(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.USER,
            mem_type=MemoryType.PREFERENCE,
            sensitivity=SensitivityLevel.MEDIUM,
        )
        assert wf.needs_steward_confirmation(record) is True

    def test_case_lesson_needs_steward(self) -> None:
        wf = ConfirmationWorkflow()
        record = _make_record(
            scope=MemoryScope.CASE,
            mem_type=MemoryType.LESSON,
        )
        assert wf.needs_steward_confirmation(record) is True


# ---- Dedup merge through workflow tests ----


class TestWorkflowDedup:
    def test_same_key_records_merge_in_workflow(self) -> None:
        wf = ConfirmationWorkflow()
        record_a = _make_record(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.PREFERENCE,
            source_refs=(SourceRef(source_id="src-1", source_type="run"),),
        )
        record_b = _make_record(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.PREFERENCE,
            source_refs=(SourceRef(source_id="src-2", source_type="run"),),
        )
        wf.write_record(record_a)
        wf.write_record(record_b)

        dedup = DedupKey.from_record(record_a)
        merged = wf.queue.get_record(dedup)
        assert merged is not None
        assert len(merged.source_refs) == 2
