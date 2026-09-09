"""S2 runtime: failure taxonomy, effect_unknown handling。

事实源：design doc §4.3、S2-T5 plan。

Typed failure reasons. effect_unknown: write event, do NOT auto-retry.
Cancellation stops new tasks, records in-flight effect state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.identifiers import new_id


class FailureCategory(StrEnum):
    """Typed failure reasons for the runtime."""

    TOOL_EXECUTION = "tool_execution"
    TIMEOUT = "timeout"
    EFFECT_UNKNOWN = "effect_unknown"
    APPROVAL_DENIED = "approval_denied"
    DELEGATION_EXCEEDED = "delegation_exceeded"
    CONTEXT_REFUSED = "context_refused"
    PROVIDER_ERROR = "provider_error"
    VALIDATION_ERROR = "validation_error"
    CANCELLED = "cancelled"


class FailureRecord(BaseModel):
    """A single failure record within a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    run_id: str
    task_id: str
    category: FailureCategory
    message: str
    auto_retry: bool = True
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FailureTaxonomy:
    """Manages failure recording and querying."""

    def __init__(self) -> None:
        self._records: list[FailureRecord] = []

    def record(
        self,
        *,
        run_id: str,
        task_id: str,
        category: FailureCategory,
        message: str,
    ) -> FailureRecord:
        """Record a failure. effect_unknown is never auto-retried."""
        auto_retry = category != FailureCategory.EFFECT_UNKNOWN
        rec = FailureRecord(
            id=new_id().hex,
            run_id=run_id,
            task_id=task_id,
            category=category,
            message=message,
            auto_retry=auto_retry,
        )
        self._records.append(rec)
        return rec

    def failures_for_task(self, task_id: str) -> list[FailureRecord]:
        """Return all failure records for a given task."""
        return [r for r in self._records if r.task_id == task_id]

    def failures_for_run(self, run_id: str) -> list[FailureRecord]:
        """Return all failure records for a given run."""
        return [r for r in self._records if r.run_id == run_id]
