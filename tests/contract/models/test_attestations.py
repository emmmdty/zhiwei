"""S3-T1 RED: Attestation contract and registry tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.models.attestations import AttestationRegistry
from zhiwei.models.contracts import AttestationStatus, CapabilityAttestation


def _make_attestation(
    model_name: str = "test-model",
    *,
    expired: bool = False,
    **overrides: Any,
) -> CapabilityAttestation:
    now = datetime.now(tz=UTC)
    if expired:
        valid_from = now - timedelta(days=60)
        valid_until = now - timedelta(days=1)
    else:
        valid_from = now
        valid_until = now + timedelta(days=30)
    defaults: dict[str, Any] = {
        "id": str(new_id()),
        "endpoint_id": "test-endpoint",
        "model_name": model_name,
        "probed_at": now,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "status": AttestationStatus.VALID,
        "qualification_level": "transport_verified",
        "probed_capabilities": {"tool_use": True},
        "source_profile_digest": digest_bytes(canonical_json({"test": True})),
    }
    defaults.update(overrides)
    return CapabilityAttestation(**defaults)


class TestAttestationRegistry:
    def test_register_and_get_latest(self) -> None:
        reg = AttestationRegistry()
        att = _make_attestation()
        reg.register(att)
        latest = reg.get_latest(att.endpoint_id, att.model_name)
        assert latest is not None
        assert latest.id == att.id

    def test_latest_is_most_recent(self) -> None:
        reg = AttestationRegistry()
        now = datetime.now(tz=UTC)
        att_old = _make_attestation(
            probed_at=now - timedelta(days=10),
            valid_from=now - timedelta(days=10),
            valid_until=now + timedelta(days=20),
        )
        att_new = _make_attestation(
            probed_at=now,
            valid_from=now,
            valid_until=now + timedelta(days=30),
        )
        reg.register(att_old)
        reg.register(att_new)
        latest = reg.get_latest(att_old.endpoint_id, att_old.model_name)
        assert latest is not None
        assert latest.probed_at > att_old.probed_at

    def test_expired_not_returned_as_latest(self) -> None:
        reg = AttestationRegistry()
        att = _make_attestation(expired=True)
        reg.register(att)
        latest = reg.get_latest(att.endpoint_id, att.model_name)
        assert latest is None

    def test_empty_registry_returns_none(self) -> None:
        reg = AttestationRegistry()
        assert reg.get_latest("any", "any") is None

    def test_filter_by_model_name(self) -> None:
        reg = AttestationRegistry()
        att_a = _make_attestation(model_name="model-a")
        att_b = _make_attestation(model_name="model-b")
        reg.register(att_a)
        reg.register(att_b)
        assert reg.get_latest("test-endpoint", "model-a") is not None
        assert reg.get_latest("test-endpoint", "model-b") is not None
        assert reg.get_latest("test-endpoint", "model-c") is None

    def test_attestation_digest_is_computed(self) -> None:
        att = _make_attestation()
        assert att.attestation_digest is not None
        assert att.attestation_digest.startswith("sha256:")

    def test_digest_deterministic(self) -> None:
        fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
        att_a = _make_attestation(probed_at=fixed_now, valid_from=fixed_now, valid_until=fixed_now + timedelta(days=30))
        att_b = _make_attestation(probed_at=fixed_now, valid_from=fixed_now, valid_until=fixed_now + timedelta(days=30))
        assert att_a.attestation_digest == att_b.attestation_digest

    def test_digest_changes_with_content(self) -> None:
        att1 = _make_attestation(probed_capabilities={"tool_use": True})
        att2 = _make_attestation(probed_capabilities={"tool_use": False})
        assert att1.attestation_digest != att2.attestation_digest

    def test_expired_status_detection(self) -> None:
        att = _make_attestation(expired=True)
        assert att.is_expired
        # Status remains VALID; is_expired is a time-based check, not a status change
        assert att.status == AttestationStatus.VALID

    def test_valid_status_not_expired(self) -> None:
        att = _make_attestation()
        assert not att.is_expired
