"""S3 Attestation types and registry."""

from __future__ import annotations

from datetime import UTC, datetime

from zhiwei.models.contracts import (
    CapabilityAttestation,
)


class AttestationRegistry:
    """Registry of capability attestations with lookup by endpoint+model."""

    def __init__(self) -> None:
        self._attestations: list[CapabilityAttestation] = []

    def register(self, attestation: CapabilityAttestation) -> None:
        """Register an attestation."""
        self._attestations.append(attestation)

    def get_latest(
        self, endpoint_id: str, model_name: str
    ) -> CapabilityAttestation | None:
        """Get the most recent non-expired attestation for an endpoint+model pair."""
        now = datetime.now(tz=UTC)
        candidates = [
            a
            for a in self._attestations
            if a.endpoint_id == endpoint_id
            and a.model_name == model_name
            and a.valid_until > now
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.probed_at)

    def all_for_model(
        self, endpoint_id: str, model_name: str
    ) -> list[CapabilityAttestation]:
        """Get all attestations for a specific endpoint+model, newest first."""
        candidates = [
            a
            for a in self._attestations
            if a.endpoint_id == endpoint_id and a.model_name == model_name
        ]
        return sorted(candidates, key=lambda a: a.probed_at, reverse=True)
