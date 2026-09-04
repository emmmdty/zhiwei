"""S7 Non-symmetric write policy — confirmation workflow.

Coordinates the lifecycle from write → auto-confirm / candidate / steward-confirm.
Implements the confirmation pipeline that bridges policy.py decisions to CandidateQueue state.

事实源：S7 spec §3（write policy rules）、ADR-009。
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
    MemoryStatus,
)
from zhiwei.memory.policy import (
    WriteForbiddenError,
    WritePolicyDecision,
    evaluate_write_policy,
)


class ConfirmationAction(StrEnum):
    """Actions a user or steward can take on a candidate record."""

    CONFIRM = "confirm"
    REJECT = "reject"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class ConfirmationRecord:
    """Audit entry for a confirmation action."""

    record_id: UUID
    action: ConfirmationAction
    actor_ref: UUID
    reason: str = ""
    performed_at: datetime = field(default_factory=lambda: ensure_utc(datetime.now(tz=UTC)))


@dataclass(slots=True)
class ConfirmationWorkflow:
    """Orchestrates the non-symmetric write → confirm pipeline.

    - Auto-confirm: low-risk user preferences, case timeline/evidence/action/resolution.
    - Candidate: sensitive/derived user habits, team convention/decision/lesson, case lessons.
    - Steward-only confirm: team memory and case lessons.
    """

    queue: CandidateQueue = field(default_factory=CandidateQueue)
    _audit_log: list[ConfirmationRecord] = field(default_factory=list)

    def write_record(self, record: MemoryRecord) -> MemoryRecord:
        """Evaluate write policy and route the record accordingly.

        Returns the record with its final status (CONFIRMED for auto-confirm,
        CANDIDATE for steward-required). Raises WriteForbiddenError on forbidden content.
        """
        result = evaluate_write_policy(
            scope=record.scope,
            mem_type=record.type,
            sensitivity=record.sensitivity,
            subject=record.subject,
            canonical_value=record.canonical_value,
        )

        if result.decision == WritePolicyDecision.FORBIDDEN:
            raise WriteForbiddenError(result.reason)

        if result.decision == WritePolicyDecision.AUTO_CONFIRM:
            confirmed = record.model_copy(update={"status": MemoryStatus.CONFIRMED})
            dedup = DedupKey.from_record(confirmed)
            self.queue.records[dedup.as_tuple()] = confirmed
            return confirmed

        # CANDIDATE: add to queue (merges evidence if same dedup key)
        return self.queue.add_candidate(record)

    def steward_confirm(
        self,
        dedup_key: DedupKey,
        steward_id: UUID,
        *,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        """Confirm a candidate that requires Memory Steward approval.

        Returns None if the record is not found or not a candidate.
        """
        confirmed = self.queue.confirm_candidate(dedup_key, steward_id, now=now)
        if confirmed is not None:
            self._audit_log.append(
                ConfirmationRecord(
                    record_id=confirmed.id,
                    action=ConfirmationAction.CONFIRM,
                    actor_ref=steward_id,
                    performed_at=ensure_utc(now) if now else ensure_utc(datetime.now(tz=UTC)),
                )
            )
        return confirmed

    def steward_reject(
        self,
        dedup_key: DedupKey,
        steward_id: UUID,
        reason: str = "",
        *,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        """Reject a candidate by revoking it with steward's reason."""
        revoked = self.queue.revoke_record(dedup_key, reason, now=now)
        if revoked is not None:
            self._audit_log.append(
                ConfirmationRecord(
                    record_id=revoked.id,
                    action=ConfirmationAction.REJECT,
                    actor_ref=steward_id,
                    reason=reason,
                    performed_at=ensure_utc(now) if now else ensure_utc(datetime.now(tz=UTC)),
                )
            )
        return revoked

    def needs_steward_confirmation(self, record: MemoryRecord) -> bool:
        """Check whether a record requires steward confirmation."""
        result = evaluate_write_policy(
            scope=record.scope,
            mem_type=record.type,
            sensitivity=record.sensitivity,
            subject=record.subject,
            canonical_value=record.canonical_value,
        )
        return result.decision == WritePolicyDecision.CANDIDATE

    def audit_log(self) -> tuple[ConfirmationRecord, ...]:
        """Return the immutable audit trail."""
        return tuple(self._audit_log)
