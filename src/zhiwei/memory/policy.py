"""S7 Memory write policy.

Determines whether a memory write should be auto-confirmed, remain a candidate,
or be forbidden based on scope, type, sensitivity, and profile policy.

事实源：S7 spec §3（write policy rules）、ADR-009。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
)


class WritePolicyDecision(StrEnum):
    """Outcome of a memory write policy evaluation."""

    AUTO_CONFIRM = "auto_confirm"
    CANDIDATE = "candidate"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class WritePolicyResult:
    """Result of evaluating a memory write policy.

    decision: what to do with the write
    reason: human-readable explanation for audit
    """

    decision: WritePolicyDecision
    reason: str


# Forbidden content patterns — secrets, hidden reasoning, tool instructions, unauthorized PII
_FORBIDDEN_SUBJECT_PATTERNS: tuple[str, ...] = (
    "secret",
    "password",
    "api_key",
    "token",
    "credential",
)

_FORBIDDEN_VALUE_PATTERNS: tuple[str, ...] = (
    "system prompt",
    "hidden reasoning",
    "tool instruction",
    "retrieval instruction",
)


def evaluate_write_policy(
    *,
    scope: MemoryScope,
    mem_type: MemoryType,
    sensitivity: SensitivityLevel,
    subject: str = "",
    canonical_value: str = "",
    is_team_steward: bool = False,
) -> WritePolicyResult:
    """Evaluate whether a memory write should be auto-confirmed, queued, or rejected.

    Rules (S7 spec §3):
    - secret / hidden reasoning / tool instruction / unauthorized PII → forbidden
    - user + low-risk preference → auto_confirm
    - user + sensitive/derived → candidate
    - team + convention/decision/lesson → candidate (needs Steward confirm)
    - case + timeline/evidence/action/resolution → auto_confirm
    - case + lesson → candidate
    """
    # Security check: forbidden content
    subject_lower = subject.lower()
    value_lower = canonical_value.lower()
    for pattern in _FORBIDDEN_SUBJECT_PATTERNS:
        if pattern in subject_lower:
            return WritePolicyResult(
                decision=WritePolicyDecision.FORBIDDEN,
                reason=f"subject contains forbidden pattern: {pattern}",
            )
    for pattern in _FORBIDDEN_VALUE_PATTERNS:
        if pattern in value_lower:
            return WritePolicyResult(
                decision=WritePolicyDecision.FORBIDDEN,
                reason=f"canonical_value contains forbidden pattern: {pattern}",
            )

    # Case memory: timeline/evidence/action/resolution auto-record
    if scope == MemoryScope.CASE:
        if mem_type in (
            MemoryType.EPISODE,
            MemoryType.FACT,
            MemoryType.PREFERENCE,
            MemoryType.DECISION,
        ):
            return WritePolicyResult(
                decision=WritePolicyDecision.AUTO_CONFIRM,
                reason="case memory auto-records timeline/evidence/action/resolution",
            )
        # case + lesson → candidate
        return WritePolicyResult(
            decision=WritePolicyDecision.CANDIDATE,
            reason="case lesson requires Steward confirmation",
        )

    # Team memory: convention/decision/lesson must be confirmed
    if scope == MemoryScope.TEAM:
        return WritePolicyResult(
            decision=WritePolicyDecision.CANDIDATE,
            reason="team memory requires Memory Steward confirmation",
        )

    # User memory
    if scope == MemoryScope.USER:
        # Low-risk preference can be auto-confirmed
        if mem_type == MemoryType.PREFERENCE and sensitivity == SensitivityLevel.LOW:
            return WritePolicyResult(
                decision=WritePolicyDecision.AUTO_CONFIRM,
                reason="low-risk user preference auto-confirmed by profile policy",
            )
        # Sensitive or derived → candidate
        if sensitivity in (SensitivityLevel.MEDIUM, SensitivityLevel.HIGH):
            return WritePolicyResult(
                decision=WritePolicyDecision.CANDIDATE,
                reason=f"user memory with {sensitivity.value} sensitivity requires confirmation",
            )
        # Default: candidate for anything else
        return WritePolicyResult(
            decision=WritePolicyDecision.CANDIDATE,
            reason="user memory requires confirmation",
        )

    # Unknown scope: fail closed
    return WritePolicyResult(
        decision=WritePolicyDecision.FORBIDDEN,
        reason=f"unknown scope: {scope.value}",
    )


def apply_write_policy(record: MemoryRecord) -> MemoryRecord:
    """Apply write policy to a MemoryRecord, returning it with the appropriate status.

    Auto-confirmed records get status CONFIRMED; candidates stay CANDIDATE.
    """
    result = evaluate_write_policy(
        scope=record.scope,
        mem_type=record.type,
        sensitivity=record.sensitivity,
        subject=record.subject,
        canonical_value=record.canonical_value,
    )
    if result.decision == WritePolicyDecision.AUTO_CONFIRM:
        return record.model_copy(update={"status": MemoryStatus.CONFIRMED})
    if result.decision == WritePolicyDecision.FORBIDDEN:
        raise WriteForbiddenError(result.reason)
    return record


class WriteForbiddenError(Exception):
    """Raised when a memory write is rejected by policy."""
