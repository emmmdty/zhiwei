"""S3-T1 RED: Model/Endpoint/Profile/Attestation core contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.models.contracts import (
    AttestationStatus,
    CapabilityAttestation,
    ClassificationCeiling,
    CredentialMode,
    EndpointProfile,
    ModelProfile,
    NetworkZone,
    TrustTier,
    WireProtocol,
)

# ---- Factories ----


def _make_endpoint_profile(**overrides: Any) -> EndpointProfile:
    defaults: dict[str, Any] = {
        "id": "test-endpoint",
        "base_url": "https://api.test.example.com/v1",
        "credential_mode": CredentialMode.BEARER,
        "credential_env": "TEST_API_KEY",
        "trust_tier": TrustTier.REVIEWED,
        "network_zone": NetworkZone.EXTERNAL,
        "classification_ceiling": ClassificationCeiling.INTERNAL,
        "allowed_paths": ("/chat/completions", "/models"),
        "billing_mode": "subscription_allowance",
    }
    defaults.update(overrides)
    return EndpointProfile(**defaults)


def _make_model_profile(**overrides: Any) -> ModelProfile:
    defaults: dict[str, Any] = {
        "id": "test-model",
        "endpoint_id": "test-endpoint",
        "model_name": "test-model-v1",
        "display_name": "Test Model V1",
        "wire_protocol": WireProtocol.OPENAI_CHAT,
        "api_path": "/chat/completions",
        "context_window": 128000,
        "max_output": 8192,
        "modalities": ("text",),
        "structured_output": "none",
        "tool_choice": "auto",
        "token_counting_level": "calibrated_estimate",
        "max_wire_body_bytes": 8_388_608,
    }
    defaults.update(overrides)
    return ModelProfile(**defaults)


def _make_attestation(**overrides: Any) -> CapabilityAttestation:
    now = datetime.now(tz=UTC)
    defaults: dict[str, Any] = {
        "id": str(new_id()),
        "endpoint_id": "test-endpoint",
        "model_name": "test-model-v1",
        "probed_at": now,
        "valid_from": now,
        "valid_until": now + timedelta(days=30),
        "status": AttestationStatus.VALID,
        "qualification_level": "transport_verified",
        "probed_capabilities": {"tool_use": True, "structured_output": True},
        "source_profile_digest": digest_bytes(canonical_json({"test": True})),
    }
    defaults.update(overrides)
    return CapabilityAttestation(**defaults)


# ---- EndpointProfile tests ----


class TestEndpointProfile:
    def test_creation_with_valid_fields(self) -> None:
        ep = _make_endpoint_profile()
        assert ep.id == "test-endpoint"
        assert ep.trust_tier == TrustTier.REVIEWED
        assert ep.network_zone == NetworkZone.EXTERNAL

    def test_frozen_model_rejects_field_mutation(self) -> None:
        ep = _make_endpoint_profile()
        with pytest.raises(ValidationError):
            ep.id = "changed"  # type: ignore[misc]

    def test_rejects_empty_id(self) -> None:
        with pytest.raises(ValidationError):
            _make_endpoint_profile(id="")

    def test_rejects_empty_base_url(self) -> None:
        with pytest.raises(ValidationError):
            _make_endpoint_profile(base_url="")

    def test_rejects_empty_allowed_paths(self) -> None:
        with pytest.raises(ValidationError):
            _make_endpoint_profile(allowed_paths=())

    def test_classification_ceiling_ordering(self) -> None:
        assert ClassificationCeiling.PUBLIC < ClassificationCeiling.INTERNAL
        assert ClassificationCeiling.INTERNAL < ClassificationCeiling.CONFIDENTIAL
        assert ClassificationCeiling.CONFIDENTIAL < ClassificationCeiling.RESTRICTED

    def test_trust_tier_ranking(self) -> None:
        assert TrustTier.UNVERIFIED < TrustTier.OPERATOR_DECLARED
        assert TrustTier.OPERATOR_DECLARED < TrustTier.REVIEWED

    def test_runtime_registration_floor_is_lowest(self) -> None:
        floor = EndpointProfile.runtime_registration_floor()
        assert floor.trust_tier == TrustTier.UNVERIFIED
        assert floor.network_zone == NetworkZone.UNKNOWN
        assert floor.classification_ceiling == ClassificationCeiling.PUBLIC
        assert floor.allowed_paths == ("/chat/completions",)


# ---- ModelProfile tests ----


class TestModelProfile:
    def test_creation_with_valid_fields(self) -> None:
        mp = _make_model_profile()
        assert mp.model_name == "test-model-v1"
        assert mp.wire_protocol == WireProtocol.OPENAI_CHAT

    def test_frozen_model_rejects_field_mutation(self) -> None:
        mp = _make_model_profile()
        with pytest.raises(ValidationError):
            mp.model_name = "changed"  # type: ignore[misc]

    def test_rejects_empty_model_name(self) -> None:
        with pytest.raises(ValidationError):
            _make_model_profile(model_name="")

    def test_rejects_empty_endpoint_id(self) -> None:
        with pytest.raises(ValidationError):
            _make_model_profile(endpoint_id="")

    def test_rejects_zero_context_window(self) -> None:
        with pytest.raises(ValidationError):
            _make_model_profile(context_window=0)

    def test_rejects_negative_max_output(self) -> None:
        with pytest.raises(ValidationError):
            _make_model_profile(max_output=-1)

    def test_rejects_empty_modalities(self) -> None:
        with pytest.raises(ValidationError):
            _make_model_profile(modalities=())

    def test_rejects_empty_api_path(self) -> None:
        with pytest.raises(ValidationError):
            _make_model_profile(api_path="")

    def test_rejects_invalid_wire_protocol(self) -> None:
        with pytest.raises(ValidationError):
            _make_model_profile(wire_protocol="invalid_protocol")  # type: ignore[arg-type]

    def test_digest_is_computed(self) -> None:
        mp = _make_model_profile()
        assert mp.profile_digest is not None
        assert mp.profile_digest.startswith("sha256:")

    def test_digest_is_deterministic(self) -> None:
        mp_a = _make_model_profile()
        mp_b = _make_model_profile()
        assert mp_a.profile_digest == mp_b.profile_digest

    def test_digest_changes_with_content(self) -> None:
        mp = _make_model_profile()
        mp_changed = _make_model_profile(model_name="different-model")
        assert mp.profile_digest != mp_changed.profile_digest


# ---- CapabilityAttestation tests ----


class TestCapabilityAttestation:
    def test_creation_with_valid_fields(self) -> None:
        att = _make_attestation()
        assert att.status == AttestationStatus.VALID
        assert att.qualification_level == "transport_verified"

    def test_frozen_model_rejects_field_mutation(self) -> None:
        att = _make_attestation()
        with pytest.raises(ValidationError):
            att.status = AttestationStatus.EXPIRED  # type: ignore[misc]

    def test_expired_attestation_detected(self) -> None:
        now = datetime.now(tz=UTC)
        att = _make_attestation(
            valid_from=now - timedelta(days=60),
            valid_until=now - timedelta(days=1),
        )
        assert att.is_expired

    def test_valid_attestation_not_expired(self) -> None:
        att = _make_attestation()
        assert not att.is_expired

    def test_rejects_empty_endpoint_id(self) -> None:
        with pytest.raises(ValidationError):
            _make_attestation(endpoint_id="")

    def test_rejects_empty_model_name(self) -> None:
        with pytest.raises(ValidationError):
            _make_attestation(model_name="")

    def test_rejects_empty_probed_capabilities(self) -> None:
        with pytest.raises(ValidationError):
            _make_attestation(probed_capabilities={})

    def test_rejects_valid_until_before_valid_from(self) -> None:
        now = datetime.now(tz=UTC)
        with pytest.raises(ValidationError):
            _make_attestation(
                valid_from=now,
                valid_until=now - timedelta(days=1),
            )

    def test_source_profile_digest_is_required(self) -> None:
        with pytest.raises(ValidationError):
            _make_attestation(source_profile_digest="")

    def test_digest_matches_profile(self) -> None:
        profile_content = {"model": "test", "version": 1}
        expected_digest = digest_bytes(canonical_json(profile_content))
        att = _make_attestation(source_profile_digest=expected_digest)
        assert att.source_profile_digest == expected_digest
