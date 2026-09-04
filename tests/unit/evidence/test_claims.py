"""S6-T1 RED: Claim types (Fact, Quote, Inference, Recommendation)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from zhiwei.evidence.canonical_values import (
    ReproducibilityLevel,
    make_canonical_int,
    make_canonical_text,
)
from zhiwei.evidence.claims import (
    ClaimStatus,
    ClaimType,
    CodeSpan,
    FactClaim,
    InferenceClaim,
    QuoteClaim,
    RecommendationClaim,
)
from zhiwei.evidence.errors import ClaimLevelViolationError
from zhiwei.evidence.refs import (
    PatternRef,
    QueryReplayRef,
)


def _uuid() -> UUID:
    return uuid4()


def _sha256(suffix: str = "a") -> str:
    return "sha256:" + suffix * 64


def _make_query_replay_ref(**overrides: Any) -> QueryReplayRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "sql": "SELECT 1",
    }
    defaults.update(overrides)
    return QueryReplayRef(**defaults)


def _make_pattern_ref(**overrides: Any) -> PatternRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REFERENCE_ONLY,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "pattern_name": "test-pattern",
    }
    defaults.update(overrides)
    return PatternRef(**defaults)


class TestCodeSpan:
    def test_valid(self) -> None:
        cs = CodeSpan(file_path="src/main.py", line_start=1, line_end=10)
        assert cs.line_start == 1
        assert cs.line_end == 10

    def test_line_end_must_be_gte_start(self) -> None:
        with pytest.raises(ValidationError):
            CodeSpan(file_path="src/main.py", line_start=10, line_end=5)

    def test_frozen(self) -> None:
        cs = CodeSpan(file_path="src/main.py", line_start=1, line_end=10)
        with pytest.raises(ValidationError):
            cs.line_start = 20  # type: ignore[misc]


class TestFactClaim:
    def test_valid(self) -> None:
        ref = _make_query_replay_ref()
        claim = FactClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(ref,),
            answer_digest=_sha256("a"),
            canonical_value=make_canonical_int(42),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert claim.claim_type == ClaimType.FACT
        assert claim.status == ClaimStatus.DRAFT
        assert len(claim.evidence_refs) == 1

    def test_requires_evidence(self) -> None:
        with pytest.raises(ValidationError, match="at least one evidence ref"):
            FactClaim(
                claim_id=_uuid(),
                answer_id=_uuid(),
                evidence_refs=(),
                answer_digest=_sha256("a"),
                canonical_value=make_canonical_int(42),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_rejects_reference_only_evidence(self) -> None:
        ref = _make_pattern_ref()
        with pytest.raises(ClaimLevelViolationError):
            FactClaim(
                claim_id=_uuid(),
                answer_id=_uuid(),
                evidence_refs=(ref,),
                answer_digest=_sha256("a"),
                canonical_value=make_canonical_int(42),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_accepts_copy_frozen_evidence(self) -> None:
        from datetime import UTC, datetime

        from zhiwei.evidence.canonical_values import CopyFrozenMetadata
        from zhiwei.evidence.refs import CellRef

        copy_meta = CopyFrozenMetadata(
            sql="SELECT 1",
            schema_snapshot_digest=_sha256("a"),
            executed_at=datetime(2025, 1, 1, tzinfo=UTC),
            result_copy_digest=_sha256("b"),
            row_count=1,
        )
        ref = CellRef(
            ref_id=_uuid(),
            reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
            source_id=_uuid(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            table="t",
            column="c",
            copy_frozen=copy_meta,
        )
        claim = FactClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(ref,),
            answer_digest=_sha256("a"),
            canonical_value=make_canonical_int(42),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert claim.claim_type == ClaimType.FACT

    def test_with_code_span(self) -> None:
        ref = _make_query_replay_ref()
        claim = FactClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(ref,),
            answer_digest=_sha256("a"),
            code_span=CodeSpan(file_path="src/main.py", line_start=1, line_end=5),
            code_digest=_sha256("c"),
            canonical_value=make_canonical_text("hello"),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert claim.code_span is not None
        assert claim.code_span.file_path == "src/main.py"

    def test_frozen(self) -> None:
        ref = _make_query_replay_ref()
        claim = FactClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(ref,),
            answer_digest=_sha256("a"),
            canonical_value=make_canonical_int(42),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            claim.answer_digest = "changed"  # type: ignore[misc]


class TestQuoteClaim:
    def test_valid(self) -> None:
        ref = _make_query_replay_ref()
        claim = QuoteClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(ref,),
            answer_digest=_sha256("a"),
            canonical_value=make_canonical_text("quoted text"),
            quote_text="the actual quote",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert claim.claim_type == ClaimType.QUOTE
        assert claim.quote_text == "the actual quote"

    def test_requires_evidence(self) -> None:
        with pytest.raises(ValidationError, match="at least one evidence ref"):
            QuoteClaim(
                claim_id=_uuid(),
                answer_id=_uuid(),
                evidence_refs=(),
                answer_digest=_sha256("a"),
                canonical_value=make_canonical_text("text"),
                quote_text="quote",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_rejects_reference_only_evidence(self) -> None:
        ref = _make_pattern_ref()
        with pytest.raises(ClaimLevelViolationError):
            QuoteClaim(
                claim_id=_uuid(),
                answer_id=_uuid(),
                evidence_refs=(ref,),
                answer_digest=_sha256("a"),
                canonical_value=make_canonical_text("text"),
                quote_text="quote",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_empty_quote_text_rejected(self) -> None:
        ref = _make_query_replay_ref()
        with pytest.raises(ValidationError):
            QuoteClaim(
                claim_id=_uuid(),
                answer_id=_uuid(),
                evidence_refs=(ref,),
                answer_digest=_sha256("a"),
                canonical_value=make_canonical_text("text"),
                quote_text="",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )


class TestInferenceClaim:
    def test_valid(self) -> None:
        ref = _make_pattern_ref()
        claim = InferenceClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(),
            supporting_inputs=(ref,),
            contradicting_inputs=(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert claim.claim_type == ClaimType.INFERENCE

    def test_requires_supporting_inputs(self) -> None:
        with pytest.raises(ValidationError, match="at least one supporting input"):
            InferenceClaim(
                claim_id=_uuid(),
                answer_id=_uuid(),
                evidence_refs=(),
                supporting_inputs=(),
                contradicting_inputs=(),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_accepts_reference_only_supporting(self) -> None:
        ref = _make_pattern_ref()
        claim = InferenceClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(),
            supporting_inputs=(ref,),
            contradicting_inputs=(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert len(claim.supporting_inputs) == 1

    def test_can_have_contradicting_inputs(self) -> None:
        ref1 = _make_pattern_ref()
        ref2 = _make_query_replay_ref()
        claim = InferenceClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(),
            supporting_inputs=(ref1,),
            contradicting_inputs=(ref2,),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert len(claim.contradicting_inputs) == 1


class TestRecommendationClaim:
    def test_valid(self) -> None:
        ref = _make_pattern_ref()
        claim = RecommendationClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(),
            supporting_inputs=(ref,),
            contradicting_inputs=(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert claim.claim_type == ClaimType.RECOMMENDATION

    def test_requires_supporting_inputs(self) -> None:
        with pytest.raises(ValidationError, match="at least one supporting input"):
            RecommendationClaim(
                claim_id=_uuid(),
                answer_id=_uuid(),
                evidence_refs=(),
                supporting_inputs=(),
                contradicting_inputs=(),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_accepts_reference_only_supporting(self) -> None:
        ref = _make_pattern_ref()
        claim = RecommendationClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(),
            supporting_inputs=(ref,),
            contradicting_inputs=(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert len(claim.supporting_inputs) == 1


class TestClaimStatus:
    def test_draft_default(self) -> None:
        ref = _make_query_replay_ref()
        claim = FactClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(ref,),
            answer_digest=_sha256("a"),
            canonical_value=make_canonical_int(1),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert claim.status == ClaimStatus.DRAFT

    def test_final(self) -> None:
        ref = _make_query_replay_ref()
        claim = FactClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(ref,),
            answer_digest=_sha256("a"),
            canonical_value=make_canonical_int(1),
            status=ClaimStatus.FINAL,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert claim.status == ClaimStatus.FINAL


class TestRejectsUnknownFields:
    def test_fact_rejects_unknown(self) -> None:
        ref = _make_query_replay_ref()
        with pytest.raises(ValidationError):
            FactClaim(
                claim_id=_uuid(),
                answer_id=_uuid(),
                evidence_refs=(ref,),
                answer_digest=_sha256("a"),
                canonical_value=make_canonical_int(1),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
                bogus="nope",  # type: ignore[call-arg]
            )
