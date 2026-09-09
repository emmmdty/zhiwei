"""S6-T2 tests: Evidence verifier — layered verification, tamper detection, exit codes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.canonical_values import (
    CopyFrozenMetadata,
    ReproducibilityLevel,
    make_canonical_int,
)
from zhiwei.evidence.claims import (
    FactClaim,
    InferenceClaim,
    RecommendationClaim,
)
from zhiwei.evidence.refs import (
    CellRef,
    PatternRef,
    QueryReplayRef,
)
from zhiwei.evidence.verifier import (
    VerifyExitCode,
    verify_bundle,
    verify_reference_only_not_fact,
)


def _uuid() -> UUID:
    return uuid4()


def _sha256(suffix: str = "a") -> str:
    return "sha256:" + suffix * 64


def _ts() -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC)


def _make_query_replay_ref(**overrides: Any) -> QueryReplayRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": _ts(),
        "sql": "SELECT 1",
    }
    defaults.update(overrides)
    return QueryReplayRef(**defaults)


def _make_copy_frozen_ref(**overrides: Any) -> CellRef:
    copy_meta = CopyFrozenMetadata(
        sql="SELECT count(*) FROM users",
        schema_snapshot_digest=_sha256("snap"),
        executed_at=_ts(),
        result_copy_digest=_sha256("result"),
        row_count=10,
    )
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.COPY_FROZEN,
        "source_id": _uuid(),
        "created_at": _ts(),
        "table": "users",
        "column": "count",
        "copy_frozen": copy_meta,
    }
    defaults.update(overrides)
    return CellRef(**defaults)


def _make_pattern_ref(**overrides: Any) -> PatternRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REFERENCE_ONLY,
        "source_id": _uuid(),
        "created_at": _ts(),
        "pattern_name": "test-pattern",
    }
    defaults.update(overrides)
    return PatternRef(**defaults)


def _make_fact_claim(refs: tuple, **overrides: Any) -> FactClaim:
    defaults: dict[str, Any] = {
        "claim_id": _uuid(),
        "answer_id": _uuid(),
        "evidence_refs": refs,
        "answer_digest": _sha256("ans"),
        "canonical_value": make_canonical_int(42),
        "created_at": _ts(),
        "updated_at": _ts(),
    }
    defaults.update(overrides)
    return FactClaim(**defaults)


def _make_inference_claim(**overrides: Any) -> InferenceClaim:
    ref = _make_pattern_ref()
    defaults: dict[str, Any] = {
        "claim_id": _uuid(),
        "answer_id": _uuid(),
        "evidence_refs": (),
        "supporting_inputs": (ref,),
        "contradicting_inputs": (),
        "created_at": _ts(),
        "updated_at": _ts(),
    }
    defaults.update(overrides)
    return InferenceClaim(**defaults)


def _make_bundle(
    refs: tuple = (), claims: tuple = (), **overrides: Any
) -> EvidenceBundle:
    defaults: dict[str, Any] = {
        "bundle_id": _uuid(),
        "answer_id": _uuid(),
        "evidence_refs": refs,
        "claims": claims,
        "created_at": _ts(),
        "schema_version": 1,
    }
    defaults.update(overrides)
    return EvidenceBundle(**defaults)


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------


class TestVerifyExitCodes:
    def test_success_returns_zero(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        assert result.exit_code == VerifyExitCode.SUCCESS
        assert result.ok is True

    def test_empty_refs_returns_input_schema(self) -> None:
        bundle = _make_bundle(refs=(), claims=())
        result = verify_bundle(bundle)
        assert result.exit_code == VerifyExitCode.INPUT_SCHEMA
        assert result.ok is False

    def test_invalid_schema_version_rejected_by_model(self) -> None:
        """schema_version=0 is rejected at pydantic validation, not by verifier."""
        with pytest.raises(ValidationError):
            _make_bundle(refs=(), claims=(), schema_version=0)


# ---------------------------------------------------------------------------
# Layer 1: schema / structure
# ---------------------------------------------------------------------------


class TestBundleStructure:
    def test_duplicate_ref_ids_detected(self) -> None:
        ref_id = _uuid()
        ref1 = _make_query_replay_ref(ref_id=ref_id)
        ref2 = _make_query_replay_ref(ref_id=ref_id)
        bundle = _make_bundle(refs=(ref1, ref2))
        result = verify_bundle(bundle)
        check = next(c for c in result.checks if c.check_id == "bundle_no_duplicate_refs")
        assert check.ok is False

    def test_claim_ref_orphan_detected(self) -> None:
        ref = _make_query_replay_ref()
        # Orphan ref_id not in bundle
        orphan_ref = _make_query_replay_ref()
        claim_with_orphan = _make_fact_claim((ref, orphan_ref))
        bundle = _make_bundle(refs=(ref,), claims=(claim_with_orphan,))
        result = verify_bundle(bundle)
        check = next(c for c in result.checks if c.check_id == "bundle_claim_refs_exist")
        assert check.ok is False


# ---------------------------------------------------------------------------
# Layer 2-4: source / snapshot / replay / copy_frozen
# ---------------------------------------------------------------------------


class TestSourceSnapshot:
    def test_replay_ref_query_present(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        query_checks = [c for c in result.checks if "query_present" in c.check_id]
        assert len(query_checks) == 1
        assert query_checks[0].ok is True

    def test_copy_frozen_binding_verified(self) -> None:
        ref = _make_copy_frozen_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        # The check id is ref_{id}_copy_frozen_binding
        binding_checks = [
            c for c in result.checks
            if "copy_frozen_binding" in c.check_id
        ]
        assert len(binding_checks) == 1
        assert binding_checks[0].ok is True

    def test_copy_frozen_digest_verified(self) -> None:
        ref = _make_copy_frozen_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        digest_checks = [c for c in result.checks if "result_copy_digest" in c.check_id]
        assert len(digest_checks) == 1
        assert digest_checks[0].ok is True

    def test_copy_frozen_digest_mismatch(self) -> None:
        ref = _make_copy_frozen_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        expected_digests = {str(ref.ref_id): _sha256("wrong")}
        result = verify_bundle(
            bundle,
            expected_result_copy_digests=expected_digests,
        )
        match_checks = [
            c for c in result.checks if "copy_frozen_digest_match" in c.check_id
        ]
        assert len(match_checks) == 1
        assert match_checks[0].ok is False


# ---------------------------------------------------------------------------
# Layer 5: claim span / level
# ---------------------------------------------------------------------------


class TestClaimVerification:
    def test_fact_claim_answer_digest_valid(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        digest_checks = [c for c in result.checks if "answer_digest" in c.check_id]
        assert len(digest_checks) == 1
        assert digest_checks[0].ok is True

    def test_fact_claim_code_span_order(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim(
            (ref,),
            code_span={"file_path": "src/main.py", "line_start": 1, "line_end": 10},
        )
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        span_checks = [c for c in result.checks if "span_order" in c.check_id]
        assert len(span_checks) == 1
        assert span_checks[0].ok is True

    def test_inference_claim_needs_supporting(self) -> None:
        ref = _make_pattern_ref()
        claim = _make_inference_claim(supporting_inputs=(ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        supporting_checks = [c for c in result.checks if "has_supporting" in c.check_id]
        assert len(supporting_checks) == 1
        assert supporting_checks[0].ok is True

    def test_fact_claim_rejects_reference_only_at_construction(self) -> None:
        """FactClaim pydantic model rejects reference_only refs at construction."""
        ref = _make_pattern_ref()
        with pytest.raises(Exception, match="reference_only"):
            _make_fact_claim((ref,))


# ---------------------------------------------------------------------------
# Layer 6: tamper detection
# ---------------------------------------------------------------------------


class TestTamperDetection:
    def test_tamper_answer_id_detected(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        # Reference bundle with different answer_id but same bundle_id for lookup
        ref_bundle = _make_bundle(
            refs=(ref,),
            claims=(claim,),
            bundle_id=bundle.bundle_id,
            answer_id=_uuid(),
        )
        result = verify_bundle(
            bundle,
            reference_bundles={str(bundle.bundle_id): ref_bundle},
        )
        answer_checks = [c for c in result.checks if "tamper_answer_id" in c.check_id]
        assert len(answer_checks) == 1
        assert answer_checks[0].ok is False
        assert result.exit_code == VerifyExitCode.DIGEST_ARTIFACT

    def test_tamper_ref_count_detected(self) -> None:
        ref1 = _make_query_replay_ref()
        ref2 = _make_query_replay_ref()
        claim = _make_fact_claim((ref1,))
        bundle = _make_bundle(refs=(ref1,), claims=(claim,))
        ref_bundle = _make_bundle(
            refs=(ref1, ref2), claims=(claim,), bundle_id=bundle.bundle_id
        )
        result = verify_bundle(
            bundle,
            reference_bundles={str(bundle.bundle_id): ref_bundle},
        )
        count_checks = [c for c in result.checks if "tamper_ref_count" in c.check_id]
        assert len(count_checks) == 1
        assert count_checks[0].ok is False

    def test_tamper_claim_count_detected(self) -> None:
        ref = _make_query_replay_ref()
        claim1 = _make_fact_claim((ref,))
        claim2 = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim1,))
        ref_bundle = _make_bundle(
            refs=(ref,), claims=(claim1, claim2), bundle_id=bundle.bundle_id
        )
        result = verify_bundle(
            bundle,
            reference_bundles={str(bundle.bundle_id): ref_bundle},
        )
        count_checks = [c for c in result.checks if "tamper_claim_count" in c.check_id]
        assert len(count_checks) == 1
        assert count_checks[0].ok is False

    def test_missing_reference_bundle_detected(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        fake_id = str(_uuid())
        result = verify_bundle(
            bundle,
            reference_bundles={fake_id: _make_bundle()},
        )
        ref_checks = [c for c in result.checks if "tamper_reference_present" in c.check_id]
        assert len(ref_checks) == 1
        assert ref_checks[0].ok is False

    def test_no_reference_bundles_skips_tamper(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        tamper_checks = [c for c in result.checks if "tamper" in c.check_id]
        assert len(tamper_checks) == 0


# ---------------------------------------------------------------------------
# Reference-only verification
# ---------------------------------------------------------------------------


class TestReferenceOnlyVerification:
    def test_inference_not_reproducible_is_ok(self) -> None:
        claim = _make_inference_claim()
        result = verify_reference_only_not_fact(claim)
        assert result.ok is True
        assert result.exit_code == VerifyExitCode.SUCCESS

    def test_fact_with_ref_only_rejected_at_construction(self) -> None:
        """FactClaim rejects reference_only at pydantic model level."""
        ref = _make_pattern_ref()
        with pytest.raises(Exception, match="reference_only"):
            _make_fact_claim((ref,))

    def test_fact_with_replayable_is_ok(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        result = verify_reference_only_not_fact(claim)
        assert result.ok is True

    def test_recommendation_not_reproducible_is_ok(self) -> None:
        ref = _make_pattern_ref()
        claim = RecommendationClaim(
            claim_id=_uuid(),
            answer_id=_uuid(),
            evidence_refs=(),
            supporting_inputs=(ref,),
            contradicting_inputs=(),
            created_at=_ts(),
            updated_at=_ts(),
        )
        result = verify_reference_only_not_fact(claim)
        assert result.ok is True


# ---------------------------------------------------------------------------
# Canonical value digest
# ---------------------------------------------------------------------------


class TestCanonicalValueDigest:
    def test_canonical_digest_computable(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        digest_checks = [
            c for c in result.checks if "canonical_value_integrity" in c.check_id
        ]
        assert len(digest_checks) == 1
        assert digest_checks[0].ok is True


# ---------------------------------------------------------------------------
# VerifyResult API
# ---------------------------------------------------------------------------


class TestVerifyResultAPI:
    def test_as_dict_structure(self) -> None:
        ref = _make_query_replay_ref()
        claim = _make_fact_claim((ref,))
        bundle = _make_bundle(refs=(ref,), claims=(claim,))
        result = verify_bundle(bundle)
        d = result.as_dict()
        assert "ok" in d
        assert "exit_code" in d
        assert "checks" in d
        assert isinstance(d["checks"], list)
        assert all(isinstance(c, dict) for c in d["checks"])

    def test_worst_exit_code_wins(self) -> None:
        """If multiple layers fail, the highest exit code is used."""
        ref = _make_query_replay_ref()
        orphan_ref = _make_query_replay_ref()
        # Create a claim that references an orphan ref not in the bundle
        claim_with_orphan = _make_fact_claim((ref, orphan_ref))
        bundle = _make_bundle(refs=(ref,), claims=(claim_with_orphan,))
        result = verify_bundle(bundle)
        # orphan check is INPUT_SCHEMA (2)
        assert int(result.exit_code) >= 2
