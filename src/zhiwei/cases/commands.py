"""S6 Case application commands.

Frozen lifecycle (spec s6 §4.1): created → active → triaged → resolved →
archived. Users can attach Answer/selected Evidence to a Case without
duplicating transcript. Every lifecycle mutation produces canonical lifecycle
events on the result; the caller is responsible for persisting them (same
pattern as eval seal events) — the InMemory repository is not an event log.

事实源：S6 spec §4、§4.1。
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

# spec §4.1 冻结状态机：created → active → triaged → resolved → archived。
# CREATED 严格按 spec 只允许前进到 ACTIVE（不允许静默跳变）；OPEN 及其放宽的
# 转移仅为 pre-S6 持久化 Case 的向后兼容保留，新代码不得产出。
_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.CREATED: frozenset({CaseStatus.ACTIVE}),
    CaseStatus.ACTIVE: frozenset({CaseStatus.TRIAGED}),
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
    """Result of a case mutation command.

    ``events`` 是本次命令产生的 canonical 生命周期事件（有序）；调用方负责
    落账（PG case 持久化尚未实现，见 S6 交付报告的实现缺口清单）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    created: bool
    case: dict[str, Any]
    events: tuple[dict[str, Any], ...] = ()


def _lifecycle_event(
    *,
    event_type: str,
    case: Case,
    to_status: CaseStatus | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": event_type,
        "case_id": str(case.id),
        "from_status": case.status.value,
        "to_status": to_status.value if to_status is not None else None,
        "occurred_at": utc_now().isoformat(),
    }
    if detail:
        payload["detail"] = detail
    return payload


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def create_case(
    repository: CaseRepositoryProtocol,
    *,
    case_id: UUID | None = None,
    organization_id: UUID,
    workspace_id: UUID,
    title: str,
    description: str = "",
    created_by: UUID,
) -> CaseCommandResult:
    """Create a new case in CREATED status (spec s6 §4.1 frozen machine)."""
    now = utc_now()
    case = Case(
        id=case_id or uuid4(),
        organization_id=organization_id,
        workspace_id=workspace_id,
        title=title,
        description=description,
        status=CaseStatus.CREATED,
        answer_ids=(),
        evidence_bundle_ids=(),
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    saved = await repository.save_case(case)
    return CaseCommandResult(
        created=True,
        case=saved.model_dump(mode="json"),
        events=(_lifecycle_event(event_type="case.created", case=saved),),
    )


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
    """Create a case in OPEN status（pre-S6 兼容入口；新代码用 create_case）。"""
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
    return CaseCommandResult(
        created=True,
        case=saved.model_dump(mode="json"),
        events=(_lifecycle_event(event_type="case.created", case=saved),),
    )


async def transition_case(
    repository: CaseRepositoryProtocol,
    *,
    case_id: UUID,
    target_status: CaseStatus,
) -> CaseCommandResult:
    """Transition a case to a new lifecycle status（落 canonical 事件，不静默跳变）。"""
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
    return CaseCommandResult(
        created=False,
        case=saved.model_dump(mode="json"),
        events=(
            _lifecycle_event(
                event_type="case.status_changed",
                case=case,
                to_status=target_status,
            ),
        ),
    )


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
    return CaseCommandResult(
        created=False,
        case=saved.model_dump(mode="json"),
        events=(
            _lifecycle_event(
                event_type="case.answer_attached",
                case=case,
                detail={"answer_id": str(answer_id)},
            ),
        ),
    )


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
    return CaseCommandResult(
        created=False,
        case=saved.model_dump(mode="json"),
        events=(
            _lifecycle_event(
                event_type="case.evidence_attached",
                case=case,
                detail={"evidence_bundle_id": str(evidence_bundle_id)},
            ),
        ),
    )


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
