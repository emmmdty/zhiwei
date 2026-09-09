"""S3 Model contracts: core types for Endpoint, Model profiles and Attestations."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field, model_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes


class WireProtocol(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class TrustTier(StrEnum):
    UNVERIFIED = "unverified"
    OPERATOR_DECLARED = "operator_declared"
    REVIEWED = "reviewed"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, TrustTier):
            return NotImplemented
        _order = list(TrustTier)
        return _order.index(self) < _order.index(other)

    def __le__(self, other: object) -> bool:
        if not isinstance(other, TrustTier):
            return NotImplemented
        _order = list(TrustTier)
        return _order.index(self) <= _order.index(other)


class NetworkZone(StrEnum):
    UNKNOWN = "unknown"
    EXTERNAL = "external"
    INTERNAL = "internal"


class ClassificationCeiling(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ClassificationCeiling):
            return NotImplemented
        _order = list(ClassificationCeiling)
        return _order.index(self) < _order.index(other)

    def __le__(self, other: object) -> bool:
        if not isinstance(other, ClassificationCeiling):
            return NotImplemented
        _order = list(ClassificationCeiling)
        return _order.index(self) <= _order.index(other)

    # 只定义 __lt__/__le__ 时，`a > b` 会落到 str 的字典序比较（"internal" > "public"
    # 按字母序为 False）——分类门禁必须用档位序，因此补齐反向运算符。
    def __gt__(self, other: object) -> bool:
        if not isinstance(other, ClassificationCeiling):
            return NotImplemented
        _order = list(ClassificationCeiling)
        return _order.index(self) > _order.index(other)

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, ClassificationCeiling):
            return NotImplemented
        _order = list(ClassificationCeiling)
        return _order.index(self) >= _order.index(other)


class CredentialMode(StrEnum):
    BEARER = "bearer"
    API_KEY = "api_key"


class AttestationStatus(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"


class TokenCountingLevel(StrEnum):
    AUTHORITATIVE = "authoritative_count"
    VERIFIED_LOCAL = "verified_local_count"
    CALIBRATED = "calibrated_estimate"


class EndpointProfile(BaseModel):
    """An endpoint that hosts one or more model profiles."""

    model_config = {"frozen": True}

    id: str
    base_url: str
    credential_mode: CredentialMode = CredentialMode.BEARER
    credential_env: str
    base_url_env: str | None = None
    model_env: str | None = None
    trust_tier: TrustTier = TrustTier.UNVERIFIED
    network_zone: NetworkZone = NetworkZone.UNKNOWN
    classification_ceiling: ClassificationCeiling = ClassificationCeiling.PUBLIC
    allowed_paths: tuple[str, ...] = ()
    billing_mode: str = "unknown"
    redirect_policy: str = "deny_cross_origin"
    extra_spend_allowed: bool = False
    batch_eval_allowed: bool | None = None
    project_live_policy: str = "deny"
    docs_url: str | None = None
    docs_digest: str | None = None
    terms_url: str | None = None
    terms_digest: str | None = None
    terms_reviewed_at: str | None = None
    terms_review_due_at: str | None = None
    privacy_url: str | None = None
    privacy_digest: str | None = None
    risk_acceptance_artifact: str | None = None

    _FLOOR_VALUES: ClassVar[dict[str, str]] = {
        "trust_tier": "unverified",
        "network_zone": "unknown",
        "classification_ceiling": "public",
    }

    @classmethod
    def runtime_registration_floor(cls) -> EndpointProfile:
        """Create the minimum-trust endpoint for unregistered URLs (ADR-011)."""
        return cls(
            id="__unregistered_floor__",
            base_url="https://__unregistered_floor__",
            credential_env="",
            trust_tier=TrustTier.UNVERIFIED,
            network_zone=NetworkZone.UNKNOWN,
            classification_ceiling=ClassificationCeiling.PUBLIC,
            allowed_paths=("/chat/completions",),
        )

    @model_validator(mode="after")
    def _validate_id_not_empty(self) -> EndpointProfile:
        if not self.id:
            raise ValueError("endpoint id must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_base_url_not_empty(self) -> EndpointProfile:
        if not self.base_url:
            raise ValueError("base_url must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_allowed_paths_not_empty(self) -> EndpointProfile:
        if not self.allowed_paths:
            raise ValueError("allowed_paths must not be empty")
        return self


class ModelProfile(BaseModel):
    """A specific model served by an endpoint."""

    model_config = {"frozen": True}

    id: str
    endpoint_id: str
    model_name: str
    display_name: str = ""
    wire_protocol: WireProtocol
    api_path: str
    context_window: int
    max_output: int = 0
    max_input: int | None = None
    modalities: tuple[str, ...] = ("text",)
    structured_output: str = "none"
    tool_choice: str = "auto"
    reasoning_field: str | None = None
    verification_level: str = "declared"
    token_counting_level: TokenCountingLevel = TokenCountingLevel.CALIBRATED
    max_wire_body_bytes: int = 8_388_608  # 8 MiB default
    profile_source: str | None = None

    @model_validator(mode="after")
    def _validate_model_name_not_empty(self) -> ModelProfile:
        if not self.model_name:
            raise ValueError("model_name must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_endpoint_id_not_empty(self) -> ModelProfile:
        if not self.endpoint_id:
            raise ValueError("endpoint_id must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_api_path_not_empty(self) -> ModelProfile:
        if not self.api_path:
            raise ValueError("api_path must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_context_window_positive(self) -> ModelProfile:
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        return self

    @model_validator(mode="after")
    def _validate_max_output_non_negative(self) -> ModelProfile:
        if self.max_output < 0:
            raise ValueError("max_output must be non-negative")
        return self

    @model_validator(mode="after")
    def _validate_modalities_not_empty(self) -> ModelProfile:
        if not self.modalities:
            raise ValueError("modalities must not be empty")
        return self

    @property
    def profile_digest(self) -> str:
        """SHA-256 digest of the canonical profile content."""
        content = {
            "id": self.id,
            "endpoint_id": self.endpoint_id,
            "model_name": self.model_name,
            "wire_protocol": self.wire_protocol,
            "api_path": self.api_path,
            "context_window": self.context_window,
            "max_output": self.max_output,
            "modalities": list(self.modalities),
            "structured_output": self.structured_output,
            "tool_choice": self.tool_choice,
        }
        return digest_bytes(canonical_json(content))


class CapabilityAttestation(BaseModel):
    """Result of probing a model's actual capabilities at an endpoint."""

    model_config = {"frozen": True}

    id: str
    endpoint_id: str
    model_name: str
    probed_at: datetime
    valid_from: datetime
    valid_until: datetime
    status: AttestationStatus = AttestationStatus.VALID
    qualification_level: str = "declared"
    probed_capabilities: dict[str, bool] = Field(min_length=1)
    source_profile_digest: str

    @model_validator(mode="after")
    def _validate_endpoint_id_not_empty(self) -> CapabilityAttestation:
        if not self.endpoint_id:
            raise ValueError("endpoint_id must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_model_name_not_empty(self) -> CapabilityAttestation:
        if not self.model_name:
            raise ValueError("model_name must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_time_window(self) -> CapabilityAttestation:
        if self.valid_until < self.valid_from:
            raise ValueError("valid_until must not be before valid_from")
        return self

    @model_validator(mode="after")
    def _validate_source_profile_digest_not_empty(self) -> CapabilityAttestation:
        if not self.source_profile_digest:
            raise ValueError("source_profile_digest must not be empty")
        return self

    @property
    def is_expired(self) -> bool:
        """Check if attestation has expired based on current time."""
        now = datetime.now(tz=UTC)
        return now > self.valid_until

    @property
    def attestation_digest(self) -> str:
        """SHA-256 digest of the canonical attestation content."""
        content = {
            "endpoint_id": self.endpoint_id,
            "model_name": self.model_name,
            "probed_at": self.probed_at.isoformat(),
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "status": self.status,
            "qualification_level": self.qualification_level,
            "probed_capabilities": self.probed_capabilities,
            "source_profile_digest": self.source_profile_digest,
        }
        return digest_bytes(canonical_json(content))
