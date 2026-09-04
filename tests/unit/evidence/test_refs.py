"""S6-T1 RED: EvidenceRef tagged union."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from zhiwei.evidence.canonical_values import (
    CopyFrozenMetadata,
    ReproducibilityLevel,
)
from zhiwei.evidence.errors import (
    ClaimLevelViolationError,
    CopyFrozenBindingError,
    EvidenceRefValidationError,
)
from zhiwei.evidence.refs import (
    AgentRef,
    ApiRef,
    CellRef,
    CodeRef,
    DocRef,
    EvidenceRefType,
    GitHubRef,
    PatternRef,
    QueryReplayRef,
    validate_evidence_ref_level_supports_claim,
)


def _uuid() -> UUID:
    return uuid4()


def _sha256(suffix: str = "a") -> str:
    return "sha256:" + suffix * 64


def _make_copy_frozen() -> CopyFrozenMetadata:
    return CopyFrozenMetadata(
        sql="SELECT 1",
        typed_params={},
        schema_snapshot_digest=_sha256("a"),
        executed_at=datetime(2025, 1, 1, tzinfo=UTC),
        result_copy_digest=_sha256("b"),
        row_count=1,
    )


def _make_query_replay(**overrides: Any) -> QueryReplayRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "sql": "SELECT * FROM t",
        "params": {},
    }
    defaults.update(overrides)
    return QueryReplayRef(**defaults)


def _make_cell_ref(**overrides: Any) -> CellRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "table": "users",
        "column": "id",
        "row_locator": {"id": 1},
    }
    defaults.update(overrides)
    return CellRef(**defaults)


def _make_doc_ref(**overrides: Any) -> DocRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "document_uri": "file:///docs/test.md",
    }
    defaults.update(overrides)
    return DocRef(**defaults)


def _make_code_ref(**overrides: Any) -> CodeRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "file_path": "src/main.py",
        "line_start": 1,
        "line_end": 10,
        "code_digest": _sha256("c"),
    }
    defaults.update(overrides)
    return CodeRef(**defaults)


def _make_github_ref(**overrides: Any) -> GitHubRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "repository": "owner/repo",
    }
    defaults.update(overrides)
    return GitHubRef(**defaults)


def _make_api_ref(**overrides: Any) -> ApiRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "endpoint": "https://api.example.com/v1/data",
    }
    defaults.update(overrides)
    return ApiRef(**defaults)


def _make_agent_ref(**overrides: Any) -> AgentRef:
    defaults: dict[str, Any] = {
        "ref_id": _uuid(),
        "reproducibility_level": ReproducibilityLevel.REPLAYABLE,
        "source_id": _uuid(),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "agent_id": _uuid(),
    }
    defaults.update(overrides)
    return AgentRef(**defaults)


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


class TestQueryReplayRef:
    def test_valid_replayable(self) -> None:
        ref = _make_query_replay()
        assert ref.ref_type == EvidenceRefType.QUERY_REPLAY
        assert ref.reproducibility_level == ReproducibilityLevel.REPLAYABLE

    def test_rejects_copy_frozen(self) -> None:
        with pytest.raises(EvidenceRefValidationError):
            _make_query_replay(reproducibility_level=ReproducibilityLevel.COPY_FROZEN)

    def test_rejects_reference_only(self) -> None:
        with pytest.raises(EvidenceRefValidationError):
            _make_query_replay(reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY)


class TestCellRef:
    def test_valid_replayable(self) -> None:
        ref = _make_cell_ref()
        assert ref.ref_type == EvidenceRefType.CELL_REF

    def test_valid_copy_frozen(self) -> None:
        ref = _make_cell_ref(
            reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
            copy_frozen=_make_copy_frozen(),
        )
        assert ref.reproducibility_level == ReproducibilityLevel.COPY_FROZEN
        assert ref.copy_frozen is not None

    def test_copy_frozen_requires_metadata(self) -> None:
        with pytest.raises(CopyFrozenBindingError):
            _make_cell_ref(reproducibility_level=ReproducibilityLevel.COPY_FROZEN)

    def test_reference_only_rejects_copy_frozen(self) -> None:
        with pytest.raises(EvidenceRefValidationError):
            _make_cell_ref(
                reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY,
                copy_frozen=_make_copy_frozen(),
            )

    def test_reference_only_without_metadata(self) -> None:
        ref = _make_cell_ref(reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY)
        assert ref.reproducibility_level == ReproducibilityLevel.REFERENCE_ONLY
        assert ref.copy_frozen is None


class TestDocRef:
    def test_valid_replayable(self) -> None:
        ref = _make_doc_ref()
        assert ref.ref_type == EvidenceRefType.DOC_REF

    def test_valid_copy_frozen(self) -> None:
        ref = _make_doc_ref(
            reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
            copy_frozen=_make_copy_frozen(),
        )
        assert ref.reproducibility_level == ReproducibilityLevel.COPY_FROZEN

    def test_copy_frozen_requires_metadata(self) -> None:
        with pytest.raises(CopyFrozenBindingError):
            _make_doc_ref(reproducibility_level=ReproducibilityLevel.COPY_FROZEN)

    def test_reference_only_without_metadata(self) -> None:
        ref = _make_doc_ref(reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY)
        assert ref.reproducibility_level == ReproducibilityLevel.REFERENCE_ONLY


class TestCodeRef:
    def test_valid_replayable(self) -> None:
        ref = _make_code_ref()
        assert ref.ref_type == EvidenceRefType.CODE_REF

    def test_line_end_gte_start(self) -> None:
        with pytest.raises(ValidationError):
            _make_code_ref(line_start=10, line_end=5)

    def test_copy_frozen_requires_metadata(self) -> None:
        with pytest.raises(CopyFrozenBindingError):
            _make_code_ref(reproducibility_level=ReproducibilityLevel.COPY_FROZEN)


class TestGitHubRef:
    def test_valid_replayable(self) -> None:
        ref = _make_github_ref()
        assert ref.ref_type == EvidenceRefType.GITHUB_REF

    def test_valid_reference_only(self) -> None:
        ref = _make_github_ref(reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY)
        assert ref.reproducibility_level == ReproducibilityLevel.REFERENCE_ONLY

    def test_copy_frozen_requires_metadata(self) -> None:
        with pytest.raises(CopyFrozenBindingError):
            _make_github_ref(reproducibility_level=ReproducibilityLevel.COPY_FROZEN)


class TestApiRef:
    def test_valid_replayable(self) -> None:
        ref = _make_api_ref()
        assert ref.ref_type == EvidenceRefType.API_REF

    def test_default_method_is_get(self) -> None:
        ref = _make_api_ref()
        assert ref.method == "GET"

    def test_copy_frozen_requires_metadata(self) -> None:
        with pytest.raises(CopyFrozenBindingError):
            _make_api_ref(reproducibility_level=ReproducibilityLevel.COPY_FROZEN)


class TestAgentRef:
    def test_valid_replayable(self) -> None:
        ref = _make_agent_ref()
        assert ref.ref_type == EvidenceRefType.AGENT_REF

    def test_copy_frozen_requires_metadata(self) -> None:
        with pytest.raises(CopyFrozenBindingError):
            _make_agent_ref(reproducibility_level=ReproducibilityLevel.COPY_FROZEN)


class TestPatternRef:
    def test_valid_reference_only(self) -> None:
        ref = _make_pattern_ref()
        assert ref.ref_type == EvidenceRefType.PATTERN_REF
        assert ref.reproducibility_level == ReproducibilityLevel.REFERENCE_ONLY

    def test_default_version(self) -> None:
        ref = _make_pattern_ref()
        assert ref.pattern_version == "1"

    def test_copy_frozen_requires_metadata(self) -> None:
        with pytest.raises(CopyFrozenBindingError):
            _make_pattern_ref(
                reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
                pattern_name="test",
            )


class TestValidateEvidenceRefLevel:
    def test_fact_requires_replayable_or_copy_frozen(self) -> None:
        ref = _make_query_replay()
        validate_evidence_ref_level_supports_claim(ref, "Fact")  # no error

    def test_fact_rejects_reference_only(self) -> None:
        ref = _make_pattern_ref()
        with pytest.raises(ClaimLevelViolationError):
            validate_evidence_ref_level_supports_claim(ref, "Fact")

    def test_quote_requires_replayable_or_copy_frozen(self) -> None:
        ref = _make_query_replay()
        validate_evidence_ref_level_supports_claim(ref, "Quote")  # no error

    def test_quote_rejects_reference_only(self) -> None:
        ref = _make_pattern_ref()
        with pytest.raises(ClaimLevelViolationError):
            validate_evidence_ref_level_supports_claim(ref, "Quote")

    def test_inference_accepts_any_level(self) -> None:
        ref_replay = _make_query_replay()
        ref_ref_only = _make_pattern_ref()
        validate_evidence_ref_level_supports_claim(ref_replay, "Inference")
        validate_evidence_ref_level_supports_claim(ref_ref_only, "Inference")

    def test_recommendation_accepts_any_level(self) -> None:
        ref_replay = _make_query_replay()
        ref_ref_only = _make_pattern_ref()
        validate_evidence_ref_level_supports_claim(ref_replay, "Recommendation")
        validate_evidence_ref_level_supports_claim(ref_ref_only, "Recommendation")


class TestFrozen:
    def test_query_replay_frozen(self) -> None:
        ref = _make_query_replay()
        with pytest.raises(ValidationError):
            ref.sql = "SELECT 2"  # type: ignore[misc]

    def test_cell_ref_frozen(self) -> None:
        ref = _make_cell_ref()
        with pytest.raises(ValidationError):
            ref.table = "other"  # type: ignore[misc]


class TestRejectsUnknownFields:
    def test_query_replay_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            QueryReplayRef(
                ref_id=_uuid(),
                ref_type=EvidenceRefType.QUERY_REPLAY,
                reproducibility_level=ReproducibilityLevel.REPLAYABLE,
                source_id=_uuid(),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                sql="SELECT 1",
                unknown_field="nope",  # type: ignore[call-arg]
            )
