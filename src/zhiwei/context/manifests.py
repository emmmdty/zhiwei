"""S3-T5 Context manifest schemas.

ContextManifest binds a wire capture to the context state it was sent with.
TransitionManifest records the before→after transition of a context state projection.
Both use the Envelope pattern from contracts/envelope.py.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ContextManifest(BaseModel):
    """Immutable record binding a wire body to its context state at send time.

    The body_sha256 is computed from the raw bytes at the httpx transport layer,
    before any SDK mutation. The inventory_digest covers the source inventory
    snapshot that was active when the wire was captured.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(min_length=1)
    body_sha256: str
    body_len: int = Field(ge=0)
    url: str = Field(min_length=1)
    method: str = Field(min_length=1)
    redacted_headers: dict[str, str] = Field(default_factory=dict)
    header_names: tuple[str, ...] = ()
    source_inventory_digest: str | None = None
    target_profile_digest: str | None = None
    ir_digest: str | None = None
    captured_at: str = Field(min_length=1)
    sequence_no: int = Field(ge=0)

    @field_validator("body_sha256")
    @classmethod
    def validate_body_sha256(cls, value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("body_sha256 must be lowercase SHA-256 with sha256: prefix")
        return value

    @field_validator("source_inventory_digest")
    @classmethod
    def validate_inventory_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("source_inventory_digest must be lowercase SHA-256")
        return value

    @field_validator("target_profile_digest")
    @classmethod
    def validate_profile_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("target_profile_digest must be lowercase SHA-256")
        return value

    @field_validator("ir_digest")
    @classmethod
    def validate_ir_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("ir_digest must be lowercase SHA-256")
        return value


class TransitionManifest(BaseModel):
    """Immutable record of a context state transition.

    Records the before→after state digest change, the transition type,
    and the identity of the wire body that triggered the transition.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(min_length=1)
    before_state_digest: str | None = None
    after_state_digest: str | None = None
    transition_type: str = Field(min_length=1)
    wire_body_digest: str | None = None
    ir_digest: str | None = None
    items_added: int = Field(ge=0)
    items_removed: int = Field(ge=0)
    items_unchanged: int = Field(ge=0)
    triggered_by_manifest_id: str | None = None
    occurred_at: str = Field(min_length=1)

    @field_validator("before_state_digest", "after_state_digest", "wire_body_digest", "ir_digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("digest fields must be lowercase SHA-256 with sha256: prefix")
        return value
