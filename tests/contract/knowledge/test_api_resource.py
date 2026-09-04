"""S5-T4 RED: API/MCP resource connector contract tests.

Tests cover:
- reproducibility_level declaration at connection time (ADR-003)
- Observation must enter Source Ledger before becoming Evidence
- Canonical content digest computation
- Source Ledger integration (versions, locator)
- Connection lifecycle
"""

from __future__ import annotations

from typing import Any

import pytest

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.connectors.api_resource import (
    ApiResourceConnector,
    Observation,
    ObservationResult,
    ReproducibilityLevel,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_connector(
    *,
    endpoint: str = "https://api.example.com",
    reproducibility_level: ReproducibilityLevel = ReproducibilityLevel.REFERENCE_ONLY,
) -> ApiResourceConnector:
    return ApiResourceConnector(
        endpoint=endpoint,
        organization_id=new_id(),
        workspace_id=new_id(),
        reproducibility_level=reproducibility_level,
    )


def _make_observation(**overrides: Any) -> Observation:
    defaults: dict[str, Any] = {
        "resource_uri": "/v1/users",
        "method": "GET",
        "params": {"limit": 10},
    }
    defaults.update(overrides)
    return Observation(**defaults)


# ---------------------------------------------------------------------------
# ReproducibilityLevel tests
# ---------------------------------------------------------------------------


class TestReproducibilityLevel:
    def test_replayable_value(self) -> None:
        assert ReproducibilityLevel.REPLAYABLE == "replayable"

    def test_copy_frozen_value(self) -> None:
        assert ReproducibilityLevel.COPY_FROZEN == "copy_frozen"

    def test_reference_only_value(self) -> None:
        assert ReproducibilityLevel.REFERENCE_ONLY == "reference_only"

    def test_all_levels_covered(self) -> None:
        levels = set(ReproducibilityLevel)
        assert len(levels) == 3


# ---------------------------------------------------------------------------
# Connection lifecycle tests
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    def test_connect_and_disconnect(self) -> None:
        conn = _make_connector()
        conn.connect()
        conn.disconnect()

    def test_operations_when_disconnected_raise(self) -> None:
        conn = _make_connector()
        with pytest.raises(RuntimeError, match="not connected"):
            conn.record_observation(_make_observation())

    def test_operations_after_disconnect_raise(self) -> None:
        conn = _make_connector()
        conn.connect()
        conn.disconnect()
        with pytest.raises(RuntimeError, match="not connected"):
            conn.record_observation(_make_observation())

    def test_reproducibility_level_declared_at_init(self) -> None:
        for level in ReproducibilityLevel:
            conn = _make_connector(reproducibility_level=level)
            assert conn.reproducibility_level == level


# ---------------------------------------------------------------------------
# Endpoint validation tests
# ---------------------------------------------------------------------------


class TestEndpointValidation:
    def test_blank_endpoint_rejected(self) -> None:
        with pytest.raises(ValueError, match="endpoint must not be blank"):
            ApiResourceConnector(
                endpoint="  ",
                organization_id=new_id(),
                workspace_id=new_id(),
            )


# ---------------------------------------------------------------------------
# Observation recording tests
# ---------------------------------------------------------------------------


class TestObservationRecording:
    def test_record_observation_returns_result(self) -> None:
        conn = _make_connector()
        conn.connect()
        obs = _make_observation()
        result = conn.record_observation(obs)

        assert isinstance(result, ObservationResult)
        assert result.observation == obs
        assert result.content_digest.startswith("sha256:")
        assert result.source_version.locator.connector == "api_resource"

    def test_observation_enters_source_ledger(self) -> None:
        conn = _make_connector()
        conn.connect()
        obs = _make_observation()
        result = conn.record_observation(obs)

        retrieved = conn.get_version(result.source_version.id)
        assert retrieved.id == result.source_version.id

    def test_observation_locator_contains_endpoint(self) -> None:
        conn = _make_connector(endpoint="https://api.example.com")
        conn.connect()
        obs = _make_observation(resource_uri="/v1/users")
        result = conn.record_observation(obs)

        assert "https://api.example.com" in result.source_version.locator.uri
        assert "/v1/users" in result.source_version.locator.uri

    def test_observation_content_digest_deterministic(self) -> None:
        from zhiwei.knowledge.ledger import SourceLedger

        conn = _make_connector()
        conn.connect()
        obs = _make_observation()
        r1 = conn.record_observation(obs)
        conn._ledger = SourceLedger()
        r2 = conn.record_observation(obs)
        assert r1.content_digest == r2.content_digest

    def test_observation_with_different_params_different_digest(self) -> None:
        from zhiwei.knowledge.ledger import SourceLedger

        conn = _make_connector()
        conn.connect()
        obs1 = _make_observation(params={"limit": 10})
        obs2 = _make_observation(params={"limit": 20})
        r1 = conn.record_observation(obs1)
        conn._ledger = SourceLedger()
        r2 = conn.record_observation(obs2)
        assert r1.content_digest != r2.content_digest

    def test_observation_records_reproducibility_level(self) -> None:
        conn = _make_connector(reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY)
        conn.connect()
        obs = _make_observation()
        result = conn.record_observation(obs)

        assert result.source_version.metadata["reproducibility_level"] == "reference_only"


# ---------------------------------------------------------------------------
# Multiple observations tests
# ---------------------------------------------------------------------------


class TestMultipleObservations:
    def test_multiple_observations_create_separate_versions(self) -> None:
        conn = _make_connector()
        conn.connect()
        obs1 = _make_observation(resource_uri="/v1/users")
        obs2 = _make_observation(resource_uri="/v1/orders")
        r1 = conn.record_observation(obs1)
        r2 = conn.record_observation(obs2)
        assert r1.source_version.id != r2.source_version.id

    def test_list_versions(self) -> None:
        conn = _make_connector()
        conn.connect()
        obs = _make_observation()
        result = conn.record_observation(obs)
        versions = conn.list_versions(result.source_version.source_object_id)
        assert len(versions) == 1
        assert versions[0].id == result.source_version.id

    def test_list_versions_unknown_object_raises(self) -> None:
        from zhiwei.knowledge.ledger import ObjectNotFoundError

        conn = _make_connector()
        conn.connect()
        with pytest.raises(ObjectNotFoundError):
            conn.list_versions(new_id())


# ---------------------------------------------------------------------------
# Observation model tests
# ---------------------------------------------------------------------------


class TestObservationModel:
    def test_observation_frozen(self) -> None:
        from pydantic import ValidationError

        obs = _make_observation()
        with pytest.raises(ValidationError):
            obs.resource_uri = "/changed"  # type: ignore[misc]

    def test_observation_blank_resource_uri_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Observation(resource_uri="", method="GET")

    def test_observation_blank_method_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Observation(resource_uri="/test", method="")

    def test_observation_default_values(self) -> None:
        obs = Observation(resource_uri="/test", method="GET")
        assert obs.params == {}
        assert obs.headers == {}
        assert obs.observed_at is not None


# ---------------------------------------------------------------------------
# Content digest tests
# ---------------------------------------------------------------------------


class TestContentDigest:
    def test_digest_uses_canonical_json(self) -> None:
        conn = _make_connector()
        conn.connect()
        obs = _make_observation()
        result = conn.record_observation(obs)

        expected_content = canonical_json(
            {
                "resource_uri": obs.resource_uri,
                "method": obs.method,
                "params": obs.params,
                "observed_at": obs.observed_at.isoformat(),
            }
        )
        expected_digest = digest_bytes(expected_content)
        assert result.content_digest == expected_digest

    def test_digest_format(self) -> None:
        conn = _make_connector()
        conn.connect()
        obs = _make_observation()
        result = conn.record_observation(obs)

        assert result.content_digest.startswith("sha256:")
        assert len(result.content_digest) == 71
