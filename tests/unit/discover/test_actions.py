"""S8-T5 tests: Action types, ActionReceipt, and Discover Case integration.

覆盖 S8 spec §4、§5、§6:
- Feed shows status/owner/severity/supporting/contradicting/freshness/dedupe
- Triage → create Case → ask Ask for evidence → request tool action → approval → Resolution
- Resolution doesn't rewrite detector output
- Lesson candidate from resolution
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhiwei.contracts.identifiers import new_id
from zhiwei.discover.actions import (
    ActionManager,
    ActionReceipt,
    ActionRequest,
    ActionStatus,
    ActionType,
    LessonCandidateFromAction,
)
from zhiwei.discover.cases import (
    DiscoverCase,
    DiscoverCaseManager,
    DiscoverCaseStatus,
    DiscoverFeedEntry,
)
from zhiwei.discover.hypotheses import (
    EvidenceTag,
    HypothesisKind,
    HypothesisStatus,
    RiskHypothesis,
)
from zhiwei.discover.resolutions import (
    Resolution,
    ResolutionKind,
    create_resolution,
)
from zhiwei.discover.signals import SignalSeverity, Watermark

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hypothesis(**overrides: object) -> RiskHypothesis:
    now = datetime.now(UTC)
    defaults: dict[str, object] = {
        "id": new_id(),
        "signal_id": new_id(),
        "program_version_id": new_id(),
        "detector_pack_id": new_id(),
        "detector_pack_version": 1,
        "kind": HypothesisKind.SUPPORTING,
        "title": "Anomalous spending in vendor X",
        "description": "Vendor X spending increased 300% MoM",
        "affected_entities": ("vendor:acme-corp",),
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return RiskHypothesis(**defaults)  # type: ignore[arg-type]


def _make_watermark(**overrides: object) -> Watermark:
    defaults: dict[str, object] = {
        "source_id": new_id(),
        "field_name": "updated_at",
        "value": "2026-01-01T00:00:00Z",
        "captured_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Watermark(**defaults)  # type: ignore[arg-type]


def _make_evidence_tag(
    kind: HypothesisKind = HypothesisKind.SUPPORTING,
    **overrides: object,
) -> EvidenceTag:
    defaults: dict[str, object] = {
        "tag_id": new_id(),
        "kind": kind,
        "description": "Test evidence",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return EvidenceTag(**defaults)  # type: ignore[arg-type]


def _make_resolution(**overrides: object) -> Resolution:
    defaults: dict[str, object] = {
        "id": new_id(),
        "hypothesis_id": new_id(),
        "kind": ResolutionKind.ACCEPTED,
        "rationale": "Evidence confirms hypothesis",
        "resolved_by": "human-analyst",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Resolution(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ActionRequest
# ---------------------------------------------------------------------------


class TestActionRequest:
    def test_request_is_frozen(self) -> None:
        req = ActionRequest(
            id=new_id(),
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            req.rationale = "changed"  # type: ignore[misc]

    def test_request_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ActionRequest(
                id=new_id(),
                hypothesis_id=new_id(),
                action_type=ActionType.QUERY,
                tool_name="sql-query",
                rationale="Need data",
                requested_by="analyst",
                created_at=datetime.now(UTC),
                bogus=True,  # type: ignore[call-arg]
            )

    def test_request_default_status(self) -> None:
        req = ActionRequest(
            id=new_id(),
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
            created_at=datetime.now(UTC),
        )
        assert req.status == ActionStatus.PROPOSED

    def test_request_action_types(self) -> None:
        for at in ActionType:
            req = ActionRequest(
                id=new_id(),
                hypothesis_id=new_id(),
                action_type=at,
                tool_name="tool",
                rationale="r",
                requested_by="a",
                created_at=datetime.now(UTC),
            )
            assert req.action_type == at


# ---------------------------------------------------------------------------
# ActionReceipt
# ---------------------------------------------------------------------------


class TestActionReceipt:
    def test_receipt_is_frozen(self) -> None:
        receipt = ActionReceipt(
            id=new_id(),
            action_request_id=new_id(),
            hypothesis_id=new_id(),
            success=True,
            executed_by="service",
            executed_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            receipt.success = False  # type: ignore[misc]

    def test_receipt_records_output(self) -> None:
        receipt = ActionReceipt(
            id=new_id(),
            action_request_id=new_id(),
            hypothesis_id=new_id(),
            success=True,
            output={"rows": 42},
            executed_by="service",
            executed_at=datetime.now(UTC),
        )
        assert receipt.output["rows"] == 42

    def test_receipt_records_failure(self) -> None:
        receipt = ActionReceipt(
            id=new_id(),
            action_request_id=new_id(),
            hypothesis_id=new_id(),
            success=False,
            error_message="timeout",
            executed_by="service",
            executed_at=datetime.now(UTC),
        )
        assert receipt.success is False
        assert receipt.error_message == "timeout"


# ---------------------------------------------------------------------------
# ActionManager
# ---------------------------------------------------------------------------


class TestActionManager:
    def test_manager_starts_empty(self) -> None:
        mgr = ActionManager()
        assert len(mgr.requests) == 0
        assert len(mgr.receipts) == 0

    def test_create_request(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        assert req.status == ActionStatus.PROPOSED
        assert len(mgr.requests) == 1

    def test_submit_for_approval(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        submitted = mgr.submit_for_approval(req.id)
        assert submitted.status == ActionStatus.PENDING_APPROVAL

    def test_submit_rejects_non_proposed(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        mgr.submit_for_approval(req.id)
        with pytest.raises(ValueError, match="Cannot submit for approval"):
            mgr.submit_for_approval(req.id)

    def test_approve(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        mgr.submit_for_approval(req.id)
        approved = mgr.approve(req.id, approved_by="supervisor")
        assert approved.status == ActionStatus.APPROVED

    def test_approve_rejects_non_pending(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        with pytest.raises(ValueError, match="Cannot approve"):
            mgr.approve(req.id, approved_by="supervisor")

    def test_reject(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        mgr.submit_for_approval(req.id)
        rejected = mgr.reject(req.id)
        assert rejected.status == ActionStatus.REJECTED

    def test_reject_rejects_non_pending(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        with pytest.raises(ValueError, match="Cannot reject"):
            mgr.reject(req.id)

    def test_record_receipt_success(self) -> None:
        mgr = ActionManager()
        hyp_id = new_id()
        req = mgr.create_request(
            hypothesis_id=hyp_id,
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        mgr.submit_for_approval(req.id)
        mgr.approve(req.id, approved_by="supervisor")
        receipt = mgr.record_receipt(
            req.id,
            success=True,
            output={"rows": 10},
            executed_by="service",
        )
        assert receipt.success is True
        assert receipt.hypothesis_id == hyp_id
        assert len(mgr.receipts) == 1

    def test_record_receipt_failure(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        mgr.submit_for_approval(req.id)
        mgr.approve(req.id, approved_by="supervisor")
        receipt = mgr.record_receipt(
            req.id,
            success=False,
            error_message="connection refused",
            executed_by="service",
        )
        assert receipt.success is False
        updated_req = mgr._requests[req.id]
        assert updated_req.status == ActionStatus.FAILED

    def test_record_receipt_rejects_non_approved(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Need data",
            requested_by="analyst",
        )
        mgr.submit_for_approval(req.id)
        with pytest.raises(ValueError, match="Cannot record receipt"):
            mgr.record_receipt(req.id, success=True, executed_by="s")

    def test_full_lifecycle(self) -> None:
        mgr = ActionManager()
        req = mgr.create_request(
            hypothesis_id=new_id(),
            action_type=ActionType.CREATE,
            tool_name="case-create",
            rationale="Create a case",
            requested_by="analyst",
        )
        assert req.status == ActionStatus.PROPOSED
        req = mgr.submit_for_approval(req.id)
        assert req.status == ActionStatus.PENDING_APPROVAL
        req = mgr.approve(req.id, approved_by="supervisor")
        assert req.status == ActionStatus.APPROVED
        receipt = mgr.record_receipt(
            req.id, success=True, executed_by="service"
        )
        assert receipt.success is True

    def test_get_unknown_request_raises(self) -> None:
        mgr = ActionManager()
        with pytest.raises(ValueError, match="not found"):
            mgr._get_request(new_id())


# ---------------------------------------------------------------------------
# LessonCandidateFromAction
# ---------------------------------------------------------------------------


class TestLessonCandidateFromAction:
    def test_lesson_is_frozen(self) -> None:
        lesson = LessonCandidateFromAction(
            id=new_id(),
            action_receipt_id=new_id(),
            resolution_id=new_id(),
            hypothesis_id=new_id(),
            summary="Always verify before modifying",
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            lesson.summary = "changed"  # type: ignore[misc]

    def test_lesson_has_default_confidence(self) -> None:
        lesson = LessonCandidateFromAction(
            id=new_id(),
            action_receipt_id=new_id(),
            resolution_id=new_id(),
            hypothesis_id=new_id(),
            summary="Test lesson",
            created_at=datetime.now(UTC),
        )
        assert lesson.confidence == 0.5


# ---------------------------------------------------------------------------
# DiscoverCase
# ---------------------------------------------------------------------------


class TestDiscoverCase:
    def test_case_is_frozen(self) -> None:
        case = DiscoverCase(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            title="Test case",
            created_by="analyst",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            case.title = "changed"  # type: ignore[misc]

    def test_case_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            DiscoverCase(
                id=new_id(),
                organization_id=new_id(),
                workspace_id=new_id(),
                title="Test",
                created_by="a",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                bogus=True,  # type: ignore[call-arg]
            )

    def test_case_default_status(self) -> None:
        case = DiscoverCase(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            title="Test",
            created_by="a",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert case.status == DiscoverCaseStatus.OPEN

    def test_case_severity(self) -> None:
        case = DiscoverCase(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            title="Test",
            created_by="a",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            severity=SignalSeverity.HIGH,
        )
        assert case.severity == SignalSeverity.HIGH


# ---------------------------------------------------------------------------
# DiscoverFeedEntry
# ---------------------------------------------------------------------------


class TestDiscoverFeedEntry:
    def test_feed_entry_is_frozen(self) -> None:
        entry = DiscoverFeedEntry(
            id=new_id(),
            case_id=new_id(),
            hypothesis_id=new_id(),
            hypothesis_status=HypothesisStatus.PROPOSED,
            severity=SignalSeverity.INFO,
            title="Test",
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            entry.title = "changed"  # type: ignore[misc]

    def test_feed_entry_counts(self) -> None:
        entry = DiscoverFeedEntry(
            id=new_id(),
            case_id=new_id(),
            hypothesis_id=new_id(),
            hypothesis_status=HypothesisStatus.PROPOSED,
            severity=SignalSeverity.HIGH,
            title="Test",
            supporting_count=3,
            contradicting_count=1,
            missing_count=2,
            freshness_hours=12.5,
            dedup_key="fp-abc",
            created_at=datetime.now(UTC),
        )
        assert entry.supporting_count == 3
        assert entry.contradicting_count == 1
        assert entry.missing_count == 2
        assert entry.freshness_hours == 12.5
        assert entry.dedup_key == "fp-abc"


# ---------------------------------------------------------------------------
# DiscoverCaseManager
# ---------------------------------------------------------------------------


class TestDiscoverCaseManager:
    def test_manager_starts_empty(self) -> None:
        mgr = DiscoverCaseManager()
        assert len(mgr.cases) == 0
        assert len(mgr.feed) == 0

    def test_create_case(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        assert len(mgr.cases) == 1
        assert len(mgr.feed) == 1

    def test_create_case_custom_fields(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
            title="Custom title",
            description="Custom desc",
            severity=SignalSeverity.CRITICAL,
            owner="team-alpha",
            dedup_key="fp-123",
        )
        assert case.title == "Custom title"
        assert case.severity == SignalSeverity.CRITICAL
        assert case.owner == "team-alpha"
        assert case.dedup_key == "fp-123"

    def test_link_hypothesis(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        hyp2 = _make_hypothesis()
        updated = mgr.link_hypothesis(case.id, hyp2)
        assert len(updated.hypothesis_ids) == 2

    def test_link_hypothesis_no_duplicates(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        updated = mgr.link_hypothesis(case.id, hyp)
        assert len(updated.hypothesis_ids) == 1

    def test_link_action_request(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        ar_id = new_id()
        updated = mgr.link_action_request(case.id, ar_id)
        assert ar_id in updated.action_request_ids

    def test_link_action_receipt(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        ar_id = new_id()
        updated = mgr.link_action_receipt(case.id, ar_id)
        assert ar_id in updated.action_receipt_ids

    def test_record_resolution(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        resolution = _make_resolution(hypothesis_id=hyp.id)
        updated = mgr.record_resolution(case.id, resolution)
        assert resolution.id in updated.resolution_ids

    def test_record_resolution_no_duplicates(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        resolution = _make_resolution(hypothesis_id=hyp.id)
        mgr.record_resolution(case.id, resolution)
        updated = mgr.record_resolution(case.id, resolution)
        assert len(updated.resolution_ids) == 1

    def test_resolve_case(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        resolution = _make_resolution(hypothesis_id=hyp.id)
        resolved = mgr.resolve_case(case.id, resolution)
        assert resolved.status == DiscoverCaseStatus.RESOLVED

    def test_dismiss_case(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        dismissed = mgr.dismiss_case(case.id)
        assert dismissed.status == DiscoverCaseStatus.DISMISSED

    def test_get_case(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        fetched = mgr.get_case(case.id)
        assert fetched.id == case.id

    def test_get_unknown_case_raises(self) -> None:
        mgr = DiscoverCaseManager()
        with pytest.raises(ValueError, match="not found"):
            mgr.get_case(new_id())

    def test_refresh_feed_entry(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        case = mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        tag = _make_evidence_tag(kind=HypothesisKind.SUPPORTING)
        hyp_updated = hyp.model_copy(
            update={
                "evidence_tags": (tag,),
                "score": 0.8,
                "status": HypothesisStatus.IN_TRIAGE,
            }
        )
        entry = mgr.refresh_feed_entry(case.id, hyp_updated)
        assert entry.supporting_count == 1
        assert entry.score == 0.8
        assert entry.hypothesis_status == HypothesisStatus.IN_TRIAGE

    def test_feed_entry_freshness(self) -> None:
        mgr = DiscoverCaseManager()
        hyp = _make_hypothesis()
        mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
        )
        assert mgr.feed[0].freshness_hours >= 0.0


# ---------------------------------------------------------------------------
# Full workflow: Triage → Case → Action → Approval → Resolution
# ---------------------------------------------------------------------------


class TestFullWorkflow:
    def test_triage_to_resolution(self) -> None:
        # 1. Create hypothesis (triage-ready)
        hyp = _make_hypothesis(status=HypothesisStatus.IN_TRIAGE)

        # 2. Create Discover Case
        case_mgr = DiscoverCaseManager()
        case = case_mgr.create_case(
            hypothesis=hyp,
            organization_id=new_id(),
            workspace_id=new_id(),
            created_by="analyst",
            severity=SignalSeverity.HIGH,
        )
        assert case.status == DiscoverCaseStatus.OPEN

        # 3. Request tool action
        action_mgr = ActionManager()
        req = action_mgr.create_request(
            hypothesis_id=hyp.id,
            case_id=case.id,
            action_type=ActionType.QUERY,
            tool_name="sql-query",
            rationale="Verify spending data",
            requested_by="analyst",
        )

        # 4. Link action to case
        case_mgr.link_action_request(case.id, req.id)

        # 5. Submit for approval and approve
        req = action_mgr.submit_for_approval(req.id)
        req = action_mgr.approve(req.id, approved_by="supervisor")

        # 6. Execute and record receipt
        receipt = action_mgr.record_receipt(
            req.id,
            success=True,
            output={"verified": True},
            executed_by="service",
            approved_by="supervisor",
        )
        case_mgr.link_action_receipt(case.id, receipt.id)

        # 7. Create resolution (doesn't rewrite detector output)
        resolution = create_resolution(
            hypothesis_id=hyp.id,
            kind=ResolutionKind.ACCEPTED,
            rationale="Evidence confirms hypothesis after tool verification",
            resolved_by="human-analyst",
            case_id=case.id,
            evidence_refs=(f"receipt:{receipt.id}",),
        )

        # 8. Resolve case
        resolved = case_mgr.resolve_case(case.id, resolution)
        assert resolved.status == DiscoverCaseStatus.RESOLVED
        assert resolution.id in resolved.resolution_ids

        # 9. Verify original hypothesis is not modified
        assert hyp.status == HypothesisStatus.IN_TRIAGE
        assert len(hyp.evidence_tags) == 0

    def test_reject_action_request(self) -> None:
        hyp = _make_hypothesis()
        action_mgr = ActionManager()
        req = action_mgr.create_request(
            hypothesis_id=hyp.id,
            action_type=ActionType.DELETE,
            tool_name="delete-tool",
            rationale="Delete old records",
            requested_by="analyst",
        )
        req = action_mgr.submit_for_approval(req.id)
        rejected = action_mgr.reject(req.id)
        assert rejected.status == ActionStatus.REJECTED


# ---------------------------------------------------------------------------
# Resolution doesn't rewrite detector output
# ---------------------------------------------------------------------------


class TestResolutionNonRewrite:
    def test_resolution_preserves_original_hypothesis(self) -> None:
        hyp = _make_hypothesis(
            title="Original detector output",
            description="Spending anomaly detected by pattern pack",
        )
        original_title = hyp.title
        original_description = hyp.description

        resolution = create_resolution(
            hypothesis_id=hyp.id,
            kind=ResolutionKind.ACCEPTED,
            rationale="Confirmed",
            resolved_by="analyst",
        )
        # Resolution does not touch the hypothesis
        assert hyp.title == original_title
        assert hyp.description == original_description
        assert resolution.hypothesis_id == hyp.id

    def test_resolution_links_not_hypothesis_fields(self) -> None:
        hyp = _make_hypothesis()
        resolution = create_resolution(
            hypothesis_id=hyp.id,
            kind=ResolutionKind.DISMISSED,
            rationale="False positive",
            resolved_by="analyst",
            evidence_refs=("ref:abc",),
        )
        assert "ref:abc" in resolution.evidence_refs
        assert hyp.evidence_tags == ()
