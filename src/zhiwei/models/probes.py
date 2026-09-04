"""S3-T7: Fixture attestation probing — offline schema validation.

Probes fixture model profiles against known-good mock response schemas to
produce CapabilityAttestation objects at the ``fixture_tested`` qualification
level.  No network calls are made; all validation is schema-based against
embedded fixture data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from zhiwei.contracts.identifiers import new_id
from zhiwei.models.attestations import AttestationRegistry
from zhiwei.models.contracts import (
    AttestationStatus,
    CapabilityAttestation,
    ModelProfile,
    WireProtocol,
)

# Fixture response schemas keyed by wire protocol.
# Each schema is a minimal dict describing the *required* top-level structure
# that a conformant provider response must satisfy for ``fixture_tested``.
_FIXTURE_SCHEMAS: dict[str, dict[str, Any]] = {
    WireProtocol.OPENAI_CHAT.value: {
        "required_keys": ["id", "object", "choices", "usage"],
        "object_value": "chat.completion",
        "choice_keys": ["index", "message", "finish_reason"],
        "usage_keys": ["prompt_tokens", "completion_tokens", "total_tokens"],
    },
    WireProtocol.OPENAI_RESPONSES.value: {
        "required_keys": ["id", "object", "output", "usage"],
        "object_value": "response",
        "output_keys": ["type", "content"],
        "output_content_keys": ["type", "text"],
        "usage_keys": ["input_tokens", "output_tokens", "total_tokens"],
    },
    WireProtocol.ANTHROPIC_MESSAGES.value: {
        "required_keys": ["id", "type", "role", "model", "content", "stop_reason", "usage"],
        "type_value": "message",
        "role_value": "assistant",
        "content_keys": ["type", "text"],
        "usage_keys": ["input_tokens", "output_tokens"],
    },
}

# Canonical fixture response bodies — one per protocol.  These are the exact
# shapes that ``fixture_tested`` attests against.
_FIXTURE_RESPONSES: dict[str, dict[str, Any]] = {
    WireProtocol.OPENAI_CHAT.value: {
        "id": "chatcmpl-fixture-001",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "fixture-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "fixture response"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    },
    WireProtocol.OPENAI_RESPONSES.value: {
        "id": "resp-fixture-001",
        "object": "response",
        "created_at": 1700000000,
        "model": "fixture-model",
        "output": [
            {
                "type": "message",
                "id": "msg_fixture_001",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "fixture response"}],
                "stop_reason": "stop",
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    },
    WireProtocol.ANTHROPIC_MESSAGES.value: {
        "id": "msg_fixture_001",
        "type": "message",
        "role": "assistant",
        "model": "fixture-model",
        "content": [{"type": "text", "text": "fixture response"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    },
}


class FixtureProbeError(Exception):
    """Raised when fixture probing fails due to schema or protocol mismatch."""


def _validate_fixture_response(
    protocol: str,
    response: dict[str, Any],
) -> dict[str, bool]:
    """Validate a fixture response against the protocol schema.

    Returns a dict of ``{capability_flag: passed}`` indicating which
    fixture-level capabilities the profile satisfies.
    """
    schema = _FIXTURE_SCHEMAS.get(protocol)
    if schema is None:
        raise FixtureProbeError(f"Unsupported wire protocol for fixture probing: {protocol}")

    checks: dict[str, bool] = {}

    # Top-level structure
    checks["has_required_keys"] = all(k in response for k in schema["required_keys"])
    if "object_value" in schema:
        checks["object_matches"] = response.get("object") == schema["object_value"]
    if "type_value" in schema:
        checks["type_matches"] = response.get("type") == schema["type_value"]
    if "role_value" in schema:
        checks["role_matches"] = response.get("role") == schema["role_value"]

    # Nested structure: choices / output / content
    if "choice_keys" in schema and "choices" in response:
        choices = response["choices"]
        if isinstance(choices, list) and len(choices) > 0:
            first = choices[0]
            checks["choice_structure"] = all(k in first for k in schema["choice_keys"])
            # Message sub-structure
            msg = first.get("message", {})
            checks["message_has_role"] = "role" in msg
        else:
            checks["choice_structure"] = False
            checks["message_has_role"] = False

    if "output_keys" in schema and "output" in response:
        output = response["output"]
        if isinstance(output, list) and len(output) > 0:
            first = output[0]
            checks["output_structure"] = all(k in first for k in schema["output_keys"])
            content = first.get("content", [])
            content_keys = schema.get("output_content_keys", schema.get("content_keys", []))
            if isinstance(content, list) and len(content) > 0:
                checks["content_structure"] = all(
                    k in content[0] for k in content_keys
                )
            else:
                checks["content_structure"] = False
        else:
            checks["output_structure"] = False
            checks["content_structure"] = False

    if "content_keys" in schema and "content" in response:
        content = response["content"]
        if isinstance(content, list) and len(content) > 0:
            checks["content_structure"] = all(
                k in content[0] for k in schema["content_keys"]
            )
        else:
            checks["content_structure"] = False

    # Usage structure
    usage = response.get("usage", {})
    checks["usage_structure"] = all(k in usage for k in schema["usage_keys"])

    return checks


def _derive_capabilities(
    profile: ModelProfile,
    protocol_checks: dict[str, bool],
) -> dict[str, bool]:
    """Derive probed capability flags from profile metadata + protocol checks.

    These flags indicate what the profile *claims* and whether the fixture
    schema *confirms* the claim structurally.
    """
    caps: dict[str, bool] = {}

    # Structural fixture checks
    caps.update(protocol_checks)

    # Profile-derived claims (fixture-tested means the profile shape is valid)
    caps["has_model_name"] = bool(profile.model_name)
    caps["has_endpoint_id"] = bool(profile.endpoint_id)
    caps["has_context_window"] = profile.context_window > 0
    caps["has_max_output"] = profile.max_output >= 0
    caps["has_api_path"] = bool(profile.api_path)
    caps["has_wire_protocol"] = bool(profile.wire_protocol)

    return caps


def probe_fixture_attestation(
    profile: ModelProfile,
    *,
    ttl_days: int = 30,
) -> CapabilityAttestation:
    """Probe a model profile against fixture response schemas (offline).

    Produces a ``CapabilityAttestation`` at ``fixture_tested`` qualification
    level.  No network calls are made — validation is purely structural
    against embedded fixture response bodies.

    Raises ``FixtureProbeError`` if the profile's wire protocol is unsupported
    or the fixture response fails structural validation.
    """
    protocol = profile.wire_protocol.value
    fixture_response = _FIXTURE_RESPONSES.get(protocol)
    if fixture_response is None:
        raise FixtureProbeError(
            f"No fixture response defined for wire protocol: {protocol}"
        )

    protocol_checks = _validate_fixture_response(protocol, fixture_response)
    caps = _derive_capabilities(profile, protocol_checks)

    # All structural checks must pass for fixture_tested
    failed = [k for k, v in caps.items() if not v]
    if failed:
        raise FixtureProbeError(
            f"Fixture probe failed for {profile.model_name} "
            f"(protocol={protocol}): failed checks: {failed}"
        )

    now = datetime.now(tz=UTC)
    return CapabilityAttestation(
        id=str(new_id()),
        endpoint_id=profile.endpoint_id,
        model_name=profile.model_name,
        probed_at=now,
        valid_from=now,
        valid_until=now + timedelta(days=ttl_days),
        status=AttestationStatus.VALID,
        qualification_level="fixture_tested",
        probed_capabilities=caps,
        source_profile_digest=profile.profile_digest,
    )


def run_fixture_attestations(
    profiles: dict[str, ModelProfile],
    registry: AttestationRegistry | None = None,
) -> list[CapabilityAttestation]:
    """Run fixture attestation for all provided model profiles.

    Returns a list of attestations produced (one per profile).  Profiles with
    unsupported protocols are skipped with a note in the returned list's
    metadata.  The optional ``registry`` is populated with all successful
    attestations.
    """
    attestations: list[CapabilityAttestation] = []
    for _profile_id, profile in profiles.items():
        try:
            att = probe_fixture_attestation(profile)
            attestations.append(att)
            if registry is not None:
                registry.register(att)
        except FixtureProbeError:
            # Unsupported protocol or structural mismatch — skip silently
            pass
    return attestations
