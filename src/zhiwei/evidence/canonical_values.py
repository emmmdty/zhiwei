"""S6 canonical value types for Evidence.

Encodes the canonical representation of evidence values and reproducibility
levels per ADR-003. copy_frozen binds query metadata; reference_only carries
only a locator.

事实源：S6 spec §3、ADR-003。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhiwei.contracts.time import ensure_utc


class ReproducibilityLevel(StrEnum):
    """Evidence reproducibility levels (ADR-003).

    replayable:   re-execute on original snapshot → byte-identical result.
    copy_frozen:  result copy frozen with digest; query metadata bound.
    reference_only: locator only, content not frozen; supports Inference/Recommendation only.
    """

    REPLAYABLE = "replayable"
    COPY_FROZEN = "copy_frozen"
    REFERENCE_ONLY = "reference_only"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("executed_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class CopyFrozenMetadata(_FrozenModel):
    """Binding fields for copy_frozen reproducibility (ADR-003 §3).

    Binds: sql, typed_params, schema_snapshot_digest, executed_at,
    result_copy_digest, row_count.
    """

    sql: str = Field(min_length=1)
    typed_params: dict[str, Any] = Field(default_factory=dict)
    schema_snapshot_digest: str = Field(min_length=1)
    executed_at: datetime
    result_copy_digest: str = Field(min_length=1)
    row_count: int = Field(ge=0)

    @field_validator("result_copy_digest", "schema_snapshot_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError("digest must use sha256: prefix")
        return value


# ---------------------------------------------------------------------------
# Canonical value encoding helpers
# ---------------------------------------------------------------------------

class CanonicalValueType(StrEnum):
    """Type tag for canonical evidence values."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    DECIMAL = "decimal"
    TEXT = "text"
    BYTES = "bytes"


class CanonicalValue(_FrozenModel):
    """A typed canonical value bound to evidence.

    Carries a type tag and the raw python value (serialized via canonical_json
    for digest computation). The value is validated against the declared type.
    """

    type: CanonicalValueType
    value: bool | int | float | str | bytes | None

    @model_validator(mode="after")
    def _type_value_consistency(self) -> CanonicalValue:
        if self.type == CanonicalValueType.BOOL:
            if not isinstance(self.value, bool):
                raise ValueError(f"value must be bool for type={self.type}")
        elif self.type == CanonicalValueType.INT:
            if not isinstance(self.value, int) or isinstance(self.value, bool):
                raise ValueError(f"value must be int for type={self.type}")
        elif self.type == CanonicalValueType.FLOAT:
            if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
                raise ValueError(f"value must be numeric for type={self.type}")
        elif self.type == CanonicalValueType.DECIMAL:
            if not isinstance(self.value, str):
                raise ValueError(f"value must be str (decimal notation) for type={self.type}")
        elif self.type == CanonicalValueType.TEXT:
            if not isinstance(self.value, str):
                raise ValueError(f"value must be str for type={self.type}")
        elif self.type == CanonicalValueType.BYTES and not isinstance(self.value, str):
            raise ValueError(f"value must be base64url str for type={self.type}")
        return self


def make_canonical_bool(value: bool) -> CanonicalValue:
    """Create a boolean canonical value."""
    return CanonicalValue(type=CanonicalValueType.BOOL, value=value)


def make_canonical_int(value: int) -> CanonicalValue:
    """Create an integer canonical value."""
    return CanonicalValue(type=CanonicalValueType.INT, value=value)


def make_canonical_float(value: float) -> CanonicalValue:
    """Create a float canonical value."""
    return CanonicalValue(type=CanonicalValueType.FLOAT, value=value)


def make_canonical_decimal(value: str) -> CanonicalValue:
    """Create a decimal canonical value from string representation."""
    return CanonicalValue(type=CanonicalValueType.DECIMAL, value=value)


def make_canonical_text(value: str) -> CanonicalValue:
    """Create a text canonical value."""
    return CanonicalValue(type=CanonicalValueType.TEXT, value=value)


def make_canonical_bytes(value: str) -> CanonicalValue:
    """Create a bytes canonical value from base64url string."""
    return CanonicalValue(type=CanonicalValueType.BYTES, value=value)
