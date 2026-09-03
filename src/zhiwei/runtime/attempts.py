"""S2 runtime: attempt lifecycle management。

事实源：design doc §4.3、S2-T2 plan。

Manages the creation, commit, and abort of attempts within a task.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from zhiwei.contracts.identifiers import new_id


class AttemptError(RuntimeError):
    """Invalid attempt operation (e.g., commit after abort)."""


class AttemptRecord(BaseModel):
    """A single attempt within a task lifecycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    task_id: str
    attempt_number: int
    status: str = "pending"  # pending, committed, aborted


class AttemptManager:
    """Manages the lifecycle of attempts within tasks.

    Tracks pending, committed, and aborted states. Enforces valid transitions.
    """

    def __init__(self) -> None:
        self._attempts: dict[UUID, AttemptRecord] = {}

    def create(self, task_id: str, attempt_number: int) -> AttemptRecord:
        """Create a new attempt for a task."""
        attempt = AttemptRecord(
            id=new_id(),
            task_id=task_id,
            attempt_number=attempt_number,
            status="pending",
        )
        self._attempts[attempt.id] = attempt
        return attempt

    def get(self, attempt_id: UUID) -> AttemptRecord:
        """Get an attempt by ID."""
        if attempt_id not in self._attempts:
            raise AttemptError(f"Attempt {attempt_id} not found")
        return self._attempts[attempt_id]

    def commit(self, attempt_id: UUID) -> AttemptRecord:
        """Mark an attempt as committed (successfully completed)."""
        attempt = self.get(attempt_id)
        if attempt.status != "pending":
            raise AttemptError(
                f"Cannot commit attempt in status '{attempt.status}'; "
                f"only pending attempts can be committed"
            )
        updated = attempt.model_copy(update={"status": "committed"})
        self._attempts[attempt_id] = updated
        return updated

    def abort(self, attempt_id: UUID) -> AttemptRecord:
        """Mark an attempt as aborted (failed or cancelled)."""
        attempt = self.get(attempt_id)
        if attempt.status != "pending":
            raise AttemptError(
                f"Cannot abort attempt in status '{attempt.status}'; "
                f"only pending attempts can be aborted"
            )
        updated = attempt.model_copy(update={"status": "aborted"})
        self._attempts[attempt_id] = updated
        return updated

    def attempts_for_task(self, task_id: str) -> list[AttemptRecord]:
        """Return all attempts for a given task, in creation order."""
        return [
            a for a in self._attempts.values() if a.task_id == task_id
        ]
