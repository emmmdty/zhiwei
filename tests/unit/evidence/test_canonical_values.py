"""S6-T1 RED: Canonical value types for Evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zhiwei.evidence.canonical_values import (
    CanonicalValue,
    CanonicalValueType,
    CopyFrozenMetadata,
    ReproducibilityLevel,
    make_canonical_bool,
    make_canonical_bytes,
    make_canonical_decimal,
    make_canonical_float,
    make_canonical_int,
    make_canonical_text,
)


class TestReproducibilityLevel:
    def test_members(self) -> None:
        assert ReproducibilityLevel.REPLAYABLE == "replayable"
        assert ReproducibilityLevel.COPY_FROZEN == "copy_frozen"
        assert ReproducibilityLevel.REFERENCE_ONLY == "reference_only"

    def test_all_three_members_exist(self) -> None:
        members = list(ReproducibilityLevel)
        assert len(members) == 3


class TestCopyFrozenMetadata:
    def test_valid(self) -> None:
        from datetime import UTC, datetime

        m = CopyFrozenMetadata(
            sql="SELECT 1",
            typed_params={"p": 1},
            schema_snapshot_digest="sha256:" + "a" * 64,
            executed_at=datetime(2025, 1, 1, tzinfo=UTC),
            result_copy_digest="sha256:" + "b" * 64,
            row_count=1,
        )
        assert m.sql == "SELECT 1"
        assert m.row_count == 1

    def test_empty_sql_rejected(self) -> None:
        from datetime import UTC, datetime

        with pytest.raises(ValidationError):
            CopyFrozenMetadata(
                sql="",
                schema_snapshot_digest="sha256:" + "a" * 64,
                executed_at=datetime(2025, 1, 1, tzinfo=UTC),
                result_copy_digest="sha256:" + "b" * 64,
                row_count=0,
            )

    def test_negative_row_count_rejected(self) -> None:
        from datetime import UTC, datetime

        with pytest.raises(ValidationError):
            CopyFrozenMetadata(
                sql="SELECT 1",
                schema_snapshot_digest="sha256:" + "a" * 64,
                executed_at=datetime(2025, 1, 1, tzinfo=UTC),
                result_copy_digest="sha256:" + "b" * 64,
                row_count=-1,
            )

    def test_digest_must_have_sha256_prefix(self) -> None:
        from datetime import UTC, datetime

        with pytest.raises(ValidationError):
            CopyFrozenMetadata(
                sql="SELECT 1",
                schema_snapshot_digest="md5:" + "a" * 32,
                executed_at=datetime(2025, 1, 1, tzinfo=UTC),
                result_copy_digest="sha256:" + "b" * 64,
                row_count=0,
            )

    def test_frozen(self) -> None:
        from datetime import UTC, datetime

        m = CopyFrozenMetadata(
            sql="SELECT 1",
            schema_snapshot_digest="sha256:" + "a" * 64,
            executed_at=datetime(2025, 1, 1, tzinfo=UTC),
            result_copy_digest="sha256:" + "b" * 64,
            row_count=0,
        )
        with pytest.raises(ValidationError):
            m.sql = "SELECT 2"  # type: ignore[misc]


class TestCanonicalValue:
    def test_bool(self) -> None:
        cv = make_canonical_bool(True)
        assert cv.type == CanonicalValueType.BOOL
        assert cv.value is True

    def test_int(self) -> None:
        cv = make_canonical_int(42)
        assert cv.type == CanonicalValueType.INT
        assert cv.value == 42

    def test_float(self) -> None:
        cv = make_canonical_float(3.14)
        assert cv.type == CanonicalValueType.FLOAT
        assert cv.value == 3.14

    def test_decimal(self) -> None:
        cv = make_canonical_decimal("3.14159")
        assert cv.type == CanonicalValueType.DECIMAL
        assert cv.value == "3.14159"

    def test_text(self) -> None:
        cv = make_canonical_text("hello")
        assert cv.type == CanonicalValueType.TEXT
        assert cv.value == "hello"

    def test_bytes(self) -> None:
        cv = make_canonical_bytes("aGVsbG8=")
        assert cv.type == CanonicalValueType.BYTES
        assert cv.value == "aGVsbG8="

    def test_bool_rejects_int(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalValue(type=CanonicalValueType.BOOL, value=1)

    def test_int_rejects_bool(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalValue(type=CanonicalValueType.INT, value=True)

    def test_text_rejects_int(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalValue(type=CanonicalValueType.TEXT, value=42)

    def test_frozen(self) -> None:
        cv = make_canonical_bool(True)
        with pytest.raises(ValidationError):
            cv.value = False  # type: ignore[misc]

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalValue(type=CanonicalValueType.BOOL, value=True, extra_field="nope")  # type: ignore[call-arg]
