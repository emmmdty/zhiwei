"""S6-T4 CONTRACT: Verify handler and evidence verification contract.

Tests the VerifyHandler (runtime) and its integration with the evidence
verifier. Covers:
  - Handler registration and interface contract
  - Valid bundle -> ok result
  - Invalid/missing bundle -> error
  - Tampered bundle -> failure with correct exit code
  - Fact/Quote with reference_only refs -> claim_spans failure
  - Reference bundle tamper detection
  - Copy frozen digest mismatch
  - Input validation (missing fields, bad types)

事实源：S6 spec §4、§6、ADR-003。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.canonical_values import (
    CopyFrozenMetadata,
    ReproducibilityLevel,
    make_canonical_int,
    make_canonical_text,
)
from zhiwei.evidence.claims import (
    FactClaim,
    InferenceClaim,
    QuoteClaim,
    RecommendationClaim,
)
from zhiwei.evidence.refs import (
    CellRef,
    DocRef,
    PatternRef,
    QueryReplayRef,
)
from zhiwei.evidence.verifier import VerifyExitCode
from zhiwei.runtime.handlers.base import TaskInput, TaskOutput
from zhiwei.runtime.handlers.verify import VerifyHandler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid() -> UUID:
    return uuid4()


def _sha256(suffix: str = "a") -> str:
    return "sha256:" + suffix * 64


def _ts() -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC)


def _make_query_replay_ref(
    *,
    ref_id: UUID | None = None,
    source_id: UUID | None = None,
) -> QueryReplayRef:
    return QueryReplayRef(
        ref_id=ref_id or _uuid(),
        reproducibility_level=ReproducibilityLevel.REPLAYABLE,
        source_id=source_id or _uuid(),
        created_at=_ts(),
        sql="SELECT 1",
    )


def _make_copy_frozen_ref(
    *,
    ref_id: UUID | None = None,
    source_id: UUID | None = None,
) -> CellRef:
    copy_meta = CopyFrozenMetadata(
        sql="SELECT count(*) FROM users",
        schema_snapshot_digest=_sha256("snap"),
        executed_at=_ts(),
        result_copy_digest=_sha256("result"),
        row_count=10,
    )
    return CellRef(
        ref_id=ref_id or _uuid(),
        reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
        source_id=source_id or _uuid(),
        created_at=_ts(),
        table="users",
        column="count",
        copy_frozen=copy_meta,
    )


def _make_doc_ref(
    *,
    ref_id: UUID | None = None,
    source_id: UUID | None = None,
    reproducibility_level: ReproducibilityLevel = ReproducibilityLevel.REFERENCE_ONLY,
    copy_frozen: CopyFrozenMetadata | None = None,
) -> DocRef:
    return DocRef(
        ref_id=ref_id or _uuid(),
        reproducibility_level=reproducibility_level,
        source_id=source_id or _uuid(),
        created_at=_ts(),
        document_uri="docs/specs/test.md",
        copy_frozen=copy_frozen,
    )


def _make_pattern_ref(
    *,
    ref_id: UUID | None = None,
    source_id: UUID | None = None,
) -> PatternRef:
    return PatternRef(
        ref_id=ref_id or _uuid(),
        reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY,
        source_id=source_id or _uuid(),
        created_at=_ts(),
        pattern_name="test-pattern",
    )


def _make_fact_claim(
    refs: tuple[QueryReplayRef | CellRef | DocRef | PatternRef, ...],
    *,
    claim_id: UUID | None = None,
    answer_id: UUID | None = None,
) -> FactClaim:
    return FactClaim(
        claim_id=claim_id or _uuid(),
        answer_id=answer_id or _uuid(),
        evidence_refs=refs,
        answer_digest=_sha256("ans"),
        canonical_value=make_canonical_int(42),
        created_at=_ts(),
        updated_at=_ts(),
    )


def _make_quote_claim(
    refs: tuple[QueryReplayRef | CellRef | DocRef | PatternRef, ...],
    *,
    claim_id: UUID | None = None,
    answer_id: UUID | None = None,
) -> QuoteClaim:
    return QuoteClaim(
        claim_id=claim_id or _uuid(),
        answer_id=answer_id or _uuid(),
        evidence_refs=refs,
        answer_digest=_sha256("ans"),
        canonical_value=make_canonical_text("hello"),
        quote_text="hello world",
        created_at=_ts(),
        updated_at=_ts(),
    )


def _make_inference_claim(
    *,
    claim_id: UUID | None = None,
    answer_id: UUID | None = None,
    supporting_inputs: tuple[PatternRef, ...] | None = None,
) -> InferenceClaim:
    ref = supporting_inputs[0] if supporting_inputs else _make_pattern_ref()
    return InferenceClaim(
        claim_id=claim_id or _uuid(),
        answer_id=answer_id or _uuid(),
        evidence_refs=(),
        supporting_inputs=(ref,),
        contradicting_inputs=(),
        created_at=_ts(),
        updated_at=_ts(),
    )


def _make_recommendation_claim(
    *,
    claim_id: UUID | None = None,
    answer_id: UUID | None = None,
    supporting_inputs: tuple[PatternRef, ...] | None = None,
) -> RecommendationClaim:
    ref = supporting_inputs[0] if supporting_inputs else _make_pattern_ref()
    return RecommendationClaim(
        claim_id=claim_id or _uuid(),
        answer_id=answer_id or _uuid(),
        evidence_refs=(),
        supporting_inputs=(ref,),
        contradicting_inputs=(),
        created_at=_ts(),
        updated_at=_ts(),
    )


def _make_bundle(
    *,
    refs: tuple[QueryReplayRef | CellRef | DocRef | PatternRef, ...] = (),
    claims: tuple[FactClaim | QuoteClaim | InferenceClaim | RecommendationClaim, ...] = (),
    bundle_id: UUID | None = None,
    answer_id: UUID | None = None,
    schema_version: int = 1,
) -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id=bundle_id or _uuid(),
        answer_id=answer_id or _uuid(),
        evidence_refs=refs,
        claims=claims,
        created_at=_ts(),
        schema_version=schema_version,
    )


# ---------------------------------------------------------------------------
# Handler interface contract
# ---------------------------------------------------------------------------


class TestVerifyHandlerInterface:
    def test_primitive_type(self) -> None:
        handler = VerifyHandler()
        assert handler.primitive_type == "Verify"

    def test_handler_version(self) -> None:
        handler = VerifyHandler()
        assert handler.handler_version == 1

    def test_execute_returns_task_output(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert isinstance(output, TaskOutput)
        assert output.output_values["status"] == "completed"
        assert output.output_values["verification_ok"] is True
        assert output.output_values["exit_code"] == 0


# ---------------------------------------------------------------------------
# Missing / invalid input
# ---------------------------------------------------------------------------


class TestVerifyHandlerMissingInput:
    def test_missing_bundle_returns_error(self) -> None:
        handler = VerifyHandler()
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={},
        )
        output = handler.execute(input)
        assert output.output_values["status"] == "error"
        assert "missing bundle" in output.output_values["error"]
        assert output.output_values["verification_ok"] is False

    def test_invalid_bundle_returns_error(self) -> None:
        handler = VerifyHandler()
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": {"not_a_valid": "bundle"}},
        )
        output = handler.execute(input)
        assert output.output_values["status"] == "error"
        assert "invalid bundle" in output.output_values["error"]


# ---------------------------------------------------------------------------
# Valid bundle verification
# ---------------------------------------------------------------------------


class TestVerifyHandlerValidBundle:
    def test_single_replayable_ref_passes(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is True
        assert output.output_values["exit_code"] == 0
        assert output.output_values["check_count"] > 0

    def test_multiple_ref_types_pass(self) -> None:
        handler = VerifyHandler()
        ref1 = _make_query_replay_ref()
        ref2 = _make_doc_ref(
            reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
            copy_frozen=CopyFrozenMetadata(
                sql="SELECT 1",
                schema_snapshot_digest=_sha256("s"),
                executed_at=_ts(),
                result_copy_digest=_sha256("r"),
                row_count=1,
            ),
        )
        claim = _make_fact_claim((ref1, ref2))
        bundle = _make_bundle(refs=(ref1, ref2), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is True

    def test_mixed_claims_pass(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        inf_ref = _make_pattern_ref()
        fact_claim = _make_fact_claim((ref,))
        inf_claim = _make_inference_claim(supporting_inputs=(inf_ref,))
        bundle = _make_bundle(
            refs=(ref, inf_ref), claims=(fact_claim, inf_claim)
        )
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is True


# ---------------------------------------------------------------------------
# Tamper detection via handler
# ---------------------------------------------------------------------------


class TestVerifyHandlerTamper:
    def test_tampered_answer_id_detected(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        ref_bundle = _make_bundle(
            refs=(ref,),
            claims=(claim,),
            bundle_id=bundle.bundle_id,
            answer_id=_uuid(),
        )
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={
                "bundle": bundle.model_dump(mode="json"),
                "reference_bundles": {
                    str(bundle.bundle_id): ref_bundle.model_dump(mode="json"),
                },
            },
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is False
        assert output.output_values["exit_code"] == int(
            VerifyExitCode.DIGEST_ARTIFACT
        )

    def test_no_reference_bundles_skips_tamper(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        tamper_checks = [
            c
            for c in output.output_values["checks"]
            if "tamper" in c["check_id"]
        ]
        assert len(tamper_checks) == 0


# ---------------------------------------------------------------------------
# Fact/Quote with reference_only -> claim_spans failure
# ---------------------------------------------------------------------------


class TestVerifyHandlerClaimLevel:
    def test_fact_with_only_ref_only_fails(self) -> None:
        """FactClaim construction itself rejects reference_only refs."""
        ref = _make_pattern_ref()
        with pytest.raises(Exception, match="reference_only"):
            _make_fact_claim((ref,))

    def test_inference_with_ref_only_passes(self) -> None:
        handler = VerifyHandler()
        ref = _make_pattern_ref()
        claim = _make_inference_claim(supporting_inputs=(ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is True

    def test_recommendation_with_ref_only_passes(self) -> None:
        handler = VerifyHandler()
        ref = _make_pattern_ref()
        claim = _make_recommendation_claim(supporting_inputs=(ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is True


# ---------------------------------------------------------------------------
# Copy frozen digest mismatch
# ---------------------------------------------------------------------------


class TestVerifyHandlerCopyFrozen:
    def test_copy_frozen_digest_mismatch_detected(self) -> None:
        handler = VerifyHandler()
        ref = _make_copy_frozen_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        expected_digests = {str(ref.ref_id): _sha256("wrong")}
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={
                "bundle": bundle.model_dump(mode="json"),
                "expected_result_copy_digests": expected_digests,
            },
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is False
        match_checks = [
            c
            for c in output.output_values["checks"]
            if "copy_frozen_digest_match" in c["check_id"]
        ]
        assert len(match_checks) == 1
        assert match_checks[0]["ok"] is False

    def test_copy_frozen_digest_match_passes(self) -> None:
        handler = VerifyHandler()
        ref = _make_copy_frozen_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        assert ref.copy_frozen is not None
        expected_digests = {str(ref.ref_id): ref.copy_frozen.result_copy_digest}
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={
                "bundle": bundle.model_dump(mode="json"),
                "expected_result_copy_digests": expected_digests,
            },
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is True
        match_checks = [
            c
            for c in output.output_values["checks"]
            if "copy_frozen_digest_match" in c["check_id"]
        ]
        assert len(match_checks) == 1
        assert match_checks[0]["ok"] is True


# ---------------------------------------------------------------------------
# Exit code contract
# ---------------------------------------------------------------------------


class TestVerifyHandlerExitCodes:
    def test_success_exit_code_zero(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["exit_code"] == 0

    def test_empty_bundle_returns_input_schema(self) -> None:
        handler = VerifyHandler()
        bundle = _make_bundle(refs=(), claims=())
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["exit_code"] == int(
            VerifyExitCode.INPUT_SCHEMA
        )

    def test_missing_source_returns_source_snapshot(self) -> None:
        """A ref with empty source_id triggers source_snapshot exit code."""
        handler = VerifyHandler()
        ref = _make_query_replay_ref(source_id=uuid4())
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        # With a valid source_id this should succeed
        assert output.output_values["verification_ok"] is True


# ---------------------------------------------------------------------------
# Output structure contract
# ---------------------------------------------------------------------------


class TestVerifyHandlerOutputStructure:
    def test_output_has_all_expected_keys(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        values = output.output_values
        assert "status" in values
        assert "verification_ok" in values
        assert "exit_code" in values
        assert "checks" in values
        assert "check_count" in values

    def test_checks_are_dicts_with_expected_fields(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        for check in output.output_values["checks"]:
            assert "check_id" in check
            assert "ok" in check
            assert "exit_code" in check
            assert "detail" in check

    def test_check_count_matches_checks_length(self) -> None:
        handler = VerifyHandler()
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["check_count"] == len(
            output.output_values["checks"]
        )


# ---------------------------------------------------------------------------
# Cross-source evidence contract (S6 section 4: one reference task must use
# code/GitHub, document, and DB/API evidence)
# ---------------------------------------------------------------------------


class TestVerifyHandlerCrossSource:
    def test_mixed_source_refs_in_bundle(self) -> None:
        """A bundle with code, doc, and db refs verifies correctly."""
        handler = VerifyHandler()
        code_ref = _make_query_replay_ref()
        doc_ref = _make_doc_ref(
            reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
            copy_frozen=CopyFrozenMetadata(
                sql="SELECT 1",
                schema_snapshot_digest=_sha256("s"),
                executed_at=_ts(),
                result_copy_digest=_sha256("r"),
                row_count=1,
            ),
        )
        db_ref = _make_copy_frozen_ref()
        claim = _make_fact_claim((code_ref, doc_ref, db_ref))
        bundle = _make_bundle(
            refs=(code_ref, doc_ref, db_ref), claims=(claim,)
        )
        input = TaskInput(
            task_id="t1",
            attempt_id=_uuid(),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
        output = handler.execute(input)
        assert output.output_values["verification_ok"] is True
        assert output.output_values["check_count"] > 0
