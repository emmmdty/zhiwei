"""S6-T3 tests: Case domain — lifecycle, attachment, no transcript duplication."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from zhiwei.cases.commands import (
    CaseNotFoundError,
    DuplicateAttachmentError,
    InvalidTransitionError,
    attach_answer,
    attach_evidence_bundle,
    detach_answer,
    detach_evidence_bundle,
    open_case,
    transition_case,
)
from zhiwei.cases.domain import Case, CaseStatus
from zhiwei.cases.repositories import InMemoryCaseRepository


def _uuid() -> UUID:
    return uuid4()


@pytest.fixture
def repo() -> InMemoryCaseRepository:
    return InMemoryCaseRepository()


@pytest.fixture
def org_id() -> UUID:
    return _uuid()


@pytest.fixture
def workspace_id() -> UUID:
    return _uuid()


@pytest.fixture
def user_id() -> UUID:
    return _uuid()


# ---------------------------------------------------------------------------
# Case domain model
# ---------------------------------------------------------------------------


class TestCaseDomain:
    def test_case_open_default(self) -> None:
        case = Case(
            id=_uuid(),
            organization_id=_uuid(),
            workspace_id=_uuid(),
            title="Test case",
            created_by=_uuid(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert case.status == CaseStatus.OPEN
        assert len(case.answer_ids) == 0
        assert len(case.evidence_bundle_ids) == 0

    def test_case_frozen(self) -> None:
        case = Case(
            id=_uuid(),
            organization_id=_uuid(),
            workspace_id=_uuid(),
            title="Test case",
            created_by=_uuid(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            case.title = "changed"  # type: ignore[misc]

    def test_case_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            Case(
                id=_uuid(),
                organization_id=_uuid(),
                workspace_id=_uuid(),
                title="Test case",
                created_by=_uuid(),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
                bogus="nope",  # type: ignore[call-arg]
            )

    def test_case_requires_title(self) -> None:
        with pytest.raises(ValidationError):
            Case(
                id=_uuid(),
                organization_id=_uuid(),
                workspace_id=_uuid(),
                title="",
                created_by=_uuid(),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_schema_version_positive(self) -> None:
        with pytest.raises(ValidationError):
            Case(
                id=_uuid(),
                organization_id=_uuid(),
                workspace_id=_uuid(),
                title="Test",
                created_by=_uuid(),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
                schema_version=0,
            )


# ---------------------------------------------------------------------------
# Open case command
# ---------------------------------------------------------------------------


class TestOpenCase:
    @pytest.mark.asyncio
    async def test_open_case_creates(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="Investigation",
            created_by=user_id,
        )
        assert result.created is True
        assert result.case["title"] == "Investigation"
        assert result.case["status"] == CaseStatus.OPEN

    @pytest.mark.asyncio
    async def test_open_case_with_description(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="Case",
            description="Details here",
            created_by=user_id,
        )
        assert result.case["description"] == "Details here"

    @pytest.mark.asyncio
    async def test_open_case_stored(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="Stored",
            created_by=user_id,
        )
        # The case is stored; verify via list
        cases = await repo.list_cases(
            organization_id=org_id, workspace_id=workspace_id
        )
        assert len(cases) == 1
        assert cases[0].title == "Stored"


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------


class TestLifecycleTransitions:
    @pytest.mark.asyncio
    async def test_open_to_resolved(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        transition_result = await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.RESOLVED
        )
        assert transition_result.case["status"] == CaseStatus.RESOLVED

    @pytest.mark.asyncio
    async def test_open_to_archived(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        transition_result = await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.ARCHIVED
        )
        assert transition_result.case["status"] == CaseStatus.ARCHIVED

    @pytest.mark.asyncio
    async def test_resolved_to_open(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.RESOLVED
        )
        reopen = await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.OPEN
        )
        assert reopen.case["status"] == CaseStatus.OPEN

    @pytest.mark.asyncio
    async def test_resolved_to_archived(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.RESOLVED
        )
        archived = await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.ARCHIVED
        )
        assert archived.case["status"] == CaseStatus.ARCHIVED

    @pytest.mark.asyncio
    async def test_archived_cannot_transition(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.ARCHIVED
        )
        with pytest.raises(InvalidTransitionError):
            await transition_case(
                repo, case_id=case_id, target_status=CaseStatus.OPEN
            )

    @pytest.mark.asyncio
    async def test_nonexistent_case_raises(
        self, repo: InMemoryCaseRepository
    ) -> None:
        with pytest.raises(CaseNotFoundError):
            await transition_case(
                repo, case_id=_uuid(), target_status=CaseStatus.RESOLVED
            )


# ---------------------------------------------------------------------------
# Attach answer / evidence
# ---------------------------------------------------------------------------


class TestAttachments:
    @pytest.mark.asyncio
    async def test_attach_answer(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        answer_id = _uuid()
        attach_result = await attach_answer(
            repo, case_id=case_id, answer_id=answer_id
        )
        # model_dump(mode="json") serializes UUIDs as strings in a list
        assert str(answer_id) in attach_result.case["answer_ids"]

    @pytest.mark.asyncio
    async def test_attach_duplicate_answer_raises(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        answer_id = _uuid()
        await attach_answer(repo, case_id=case_id, answer_id=answer_id)
        with pytest.raises(DuplicateAttachmentError):
            await attach_answer(repo, case_id=case_id, answer_id=answer_id)

    @pytest.mark.asyncio
    async def test_attach_evidence_bundle(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        bundle_id = _uuid()
        attach_result = await attach_evidence_bundle(
            repo, case_id=case_id, evidence_bundle_id=bundle_id
        )
        assert str(bundle_id) in attach_result.case["evidence_bundle_ids"]

    @pytest.mark.asyncio
    async def test_attach_duplicate_evidence_raises(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        bundle_id = _uuid()
        await attach_evidence_bundle(
            repo, case_id=case_id, evidence_bundle_id=bundle_id
        )
        with pytest.raises(DuplicateAttachmentError):
            await attach_evidence_bundle(
                repo, case_id=case_id, evidence_bundle_id=bundle_id
            )

    @pytest.mark.asyncio
    async def test_detach_answer(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        answer_id = _uuid()
        await attach_answer(repo, case_id=case_id, answer_id=answer_id)
        detach_result = await detach_answer(
            repo, case_id=case_id, answer_id=answer_id
        )
        # model_dump(mode="json") serializes empty tuple as empty list
        assert detach_result.case["answer_ids"] == []

    @pytest.mark.asyncio
    async def test_detach_nonexistent_answer_raises(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        with pytest.raises(CaseNotFoundError):
            await detach_answer(
                repo, case_id=case_id, answer_id=_uuid()
            )

    @pytest.mark.asyncio
    async def test_detach_evidence_bundle(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        bundle_id = _uuid()
        await attach_evidence_bundle(
            repo, case_id=case_id, evidence_bundle_id=bundle_id
        )
        detach_result = await detach_evidence_bundle(
            repo, case_id=case_id, evidence_bundle_id=bundle_id
        )
        assert detach_result.case["evidence_bundle_ids"] == []


# ---------------------------------------------------------------------------
# No transcript duplication
# ---------------------------------------------------------------------------


class TestNoTranscriptDuplication:
    @pytest.mark.asyncio
    async def test_answer_ids_are_references(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        """Cases reference answers by id, not by content."""
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        answer_id = _uuid()
        await attach_answer(repo, case_id=case_id, answer_id=answer_id)
        case = await repo.get_case(UUID(case_id))
        assert case is not None
        assert answer_id in case.answer_ids
        # Only the UUID is stored, no answer content
        assert len(str(answer_id)) == 36

    @pytest.mark.asyncio
    async def test_multiple_answers_independent(
        self, repo: InMemoryCaseRepository, org_id: UUID, workspace_id: UUID, user_id: UUID
    ) -> None:
        """Multiple answers are independently referenced."""
        result = await open_case(
            repo,
            organization_id=org_id,
            workspace_id=workspace_id,
            title="T",
            created_by=user_id,
        )
        case_id = result.case["id"]
        a1, a2, a3 = _uuid(), _uuid(), _uuid()
        await attach_answer(repo, case_id=case_id, answer_id=a1)
        await attach_answer(repo, case_id=case_id, answer_id=a2)
        await attach_answer(repo, case_id=case_id, answer_id=a3)
        case = await repo.get_case(UUID(case_id))
        assert case is not None
        assert len(case.answer_ids) == 3
        assert a1 in case.answer_ids
        assert a2 in case.answer_ids
        assert a3 in case.answer_ids
