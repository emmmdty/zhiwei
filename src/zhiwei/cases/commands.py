"""S6 Case application commands.

Case lifecycle: open → resolved → archived. Users can attach Answer/selected
Evidence to a Case without duplicating transcript.

事实源：S6 spec §4。
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from zhiwei.cases.domain import Case, CaseStatus
from zhiwei.contracts.time import utc_now


class CaseCommandError(RuntimeError):
    """Base class for case command errors."""


class CaseNotFoundError(CaseCommandError):
    """Case does not exist."""


class InvalidTransitionError(CaseCommandError):
    """Invalid case lifecycle transition."""


class DuplicateAttachmentError(CaseCommandError):
    """Answer or evidence bundle already attached to this case."""


class CaseRepositoryProtocol(Protocol):
    """Minimal repository protocol for case commands."""

    async def get_case(self, case_id: UUID) -> Case | None: ...

    async def save_case(self, case: Case) -> Case: ...

    async def list_cases(
        self, *, organization_id: UUID, workspace_id: UUID
    ) -> list[Case]: ...


# ---------------------------------------------------------------------------
# Valid lifecycle transitions
# ---------------------------------------------------------------------------

_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.ACTIVE: frozenset({CaseStatus.TRIAGED, CaseStatus.RESOLVED, CaseStatus.ARCHIVED}),
    CaseStatus.TRIAGED: frozenset({CaseStatus.RESOLVED, CaseStatus.ARCHIVED}),
    CaseStatus.OPEN: frozenset({CaseStatus.ACTIVE, CaseStatus.RESOLVED, CaseStatus.ARCHIVED}),
    CaseStatus.RESOLVED: frozenset({CaseStatus.OPEN, CaseStatus.ARCHIVED}),
    CaseStatus.ARCHIVED: frozenset(),
}


def _validate_transition(current: CaseStatus, target: CaseStatus) -> None:
    if target not in _TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"cannot transition from {current} to {target}"
        )


# ---------------------------------------------------------------------------
# Command result
# ---------------------------------------------------------------------------


class CaseCommandResult(BaseModel):
    """Result of a case mutation command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created: bool
    case: dict[str, Any]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def open_case(
    repository: CaseRepositoryProtocol,
    *,
    case_id: UUID | None = None,
    organization_id: UUID,
    workspace_id: UUID,
    title: str,
    description: str = "",
    created_by: UUID,
) -> CaseCommandResult:
    """Create a new case in OPEN status."""
    now = utc_now()
    case = Case(
        id=case_id or uuid4(),
        organization_id=organization_id,
        workspace_id=workspace_id,
        title=title,
        description=description,
        status=CaseStatus.OPEN,
        answer_ids=(),
        evidence_bundle_ids=(),
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    saved = await repository.save_case(case)
    return CaseCommandResult(created=True, case=saved.model_dump(mode="json"))


async def transition_case(
    repository: CaseRepositoryProtocol,
    *,
    case_id: UUID,
    target_status: CaseStatus,
) -> CaseCommandResult:
    """Transition a case to a new lifecycle status."""
    if isinstance(case_id, str):
        case_id = UUID(case_id)
    case = await repository.get_case(case_id)
    if case is None:
        raise CaseNotFoundError(f"case {case_id} not found")
    _validate_transition(case.status, target_status)
    updated = case.model_copy(update={
        "status": target_status,
        "updated_at": utc_now(),
    })
    saved = await repository.save_case(updated)
    return CaseCommandResult(created=False, case=saved.model_dump(mode="json"))


async def attach_answer(
    repository: CaseRepositoryProtocol,
    *,
    case_id: UUID,
    answer_id: UUID,
) -> CaseCommandResult:
    """Attach an answer to a case. No transcript duplication."""
    if isinstance(case_id, str):
        case_id = UUID(case_id)
    case = await repository.get_case(case_id)
    if case is None:
        raise CaseNotFoundError(f"case {case_id} not found")
    if answer_id in case.answer_ids:
        raise DuplicateAttachmentError(
            f"answer {answer_id} already attached to case {case_id}"
        )
    updated = case.model_copy(update={
        "answer_ids": (*case.answer_ids, answer_id),
        "updated_at": utc_now(),
    })
    saved = await repository.save_case(updated)
    return CaseCommandResult(created=False, case=saved.model_dump(mode="json"))


async def attach_evidence_bundle(
    repository: CaseRepositoryProtocol,
    *,
    case_id: UUID,
    evidence_bundle_id: UUID,
) -> CaseCommandResult:
    """Attach selected evidence to a case."""
    if isinstance(case_id, str):
        case_id = UUID(case_id)
    case = await repository.get_case(case_id)
    if case is None:
        raise CaseNotFoundError(f"case {case_id} not found")
    if evidence_bundle_id in case.evidence_bundle_ids:
        raise DuplicateAttachmentError(
            f"evidence bundle {evidence_bundle_id} already attached to case {case_id}"
        )
    updated = case.model_copy(update={
        "evidence_bundle_ids": (*case.evidence_bundle_ids, evidence_bundle_id),
        "updated_at": utc_now(),
    })
    saved = await repository.save_case(updated)
    return CaseCommandResult(created=False, case=saved.model_dump(mode="json"))


async def detach_answer(
    repository: CaseRepositoryProtocol,
    *,
    case_id: UUID,
    answer_id: UUID,
) -> CaseCommandResult:
    """Detach an answer from a case."""
    if isinstance(case_id, str):
        case_id = UUID(case_id)
    case = await repository.get_case(case_id)
    if case is None:
        raise CaseNotFoundError(f"case {case_id} not found")
    if answer_id not in case.answer_ids:
        raise CaseNotFoundError(f"answer {answer_id} not attached to case {case_id}")
    updated = case.model_copy(update={
        "answer_ids": tuple(a for a in case.answer_ids if a != answer_id),
        "updated_at": utc_now(),
    })
    saved = await repository.save_case(updated)
    return CaseCommandResult(created=False, case=saved.model_dump(mode="json"))


async def detach_evidence_bundle(
    repository: CaseRepositoryProtocol,
    *,
    case_id: UUID,
    evidence_bundle_id: UUID,
) -> CaseCommandResult:
    """Detach evidence from a case."""
    if isinstance(case_id, str):
        case_id = UUID(case_id)
    case = await repository.get_case(case_id)
    if case is None:
        raise CaseNotFoundError(f"case {case_id} not found")
    if evidence_bundle_id not in case.evidence_bundle_ids:
        raise CaseNotFoundError(
            f"evidence bundle {evidence_bundle_id} not attached to case {case_id}"
        )
    updated = case.model_copy(update={
        "evidence_bundle_ids": tuple(
            e for e in case.evidence_bundle_ids if e != evidence_bundle_id
        ),
        "updated_at": utc_now(),
    })
    saved = await repository.save_case(updated)
    return CaseCommandResult(created=False, case=saved.model_dump(mode="json"))
