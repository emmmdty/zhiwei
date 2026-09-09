"""S3-T1 RED: Profile loading from YAML config files."""

from __future__ import annotations

from pathlib import Path

import pytest

from zhiwei.models.profiles import (
    EndpointRegistry,
    ModelRegistry,
    load_endpoint_profiles,
    load_model_profiles,
)

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


class TestEndpointLoading:
    def test_loads_endpoints_from_yaml(self) -> None:
        endpoints = load_endpoint_profiles(CONFIG_DIR / "providers" / "endpoints.yaml")
        assert len(endpoints) >= 1
        default = endpoints["opencode-go"]
        assert default.base_url == "https://opencode.ai/zen/go/v1"

    def test_default_endpoint_flag(self) -> None:
        endpoints = load_endpoint_profiles(CONFIG_DIR / "providers" / "endpoints.yaml")
        default_id = EndpointRegistry.find_default_endpoint_id(
            CONFIG_DIR / "providers" / "endpoints.yaml"
        )
        assert default_id == "opencode-go"
        assert default_id in endpoints

    def test_unknown_endpoint_not_in_registry(self) -> None:
        endpoints = load_endpoint_profiles(CONFIG_DIR / "providers" / "endpoints.yaml")
        assert "nonexistent-endpoint" not in endpoints

    def test_registration_floor_endpoint(self) -> None:
        ep = EndpointRegistry.create_floor_endpoint("https://custom.internal/v1")
        assert ep.trust_tier.value == "unverified"
        assert ep.classification_ceiling.value == "public"
        assert ep.base_url == "https://custom.internal/v1"


class TestModelLoading:
    def test_loads_profiles_from_yaml(self) -> None:
        profiles = load_model_profiles(
            CONFIG_DIR / "models" / "opencode-go-profiles.yaml"
        )
        assert len(profiles) >= 1

    def test_profile_has_transport(self) -> None:
        profiles = load_model_profiles(
            CONFIG_DIR / "models" / "opencode-go-profiles.yaml"
        )
        for mp in profiles.values():
            assert mp.wire_protocol is not None

    def test_rejects_missing_transport(self) -> None:
        with pytest.raises(ValueError, match="transport"):
            load_model_profiles(
                CONFIG_DIR / "models" / "opencode-go-profiles.yaml",
                required_transport="nonexistent_protocol",
            )

    def test_context_window_is_positive(self) -> None:
        profiles = load_model_profiles(
            CONFIG_DIR / "models" / "opencode-go-profiles.yaml"
        )
        for mp in profiles.values():
            assert mp.context_window > 0

    def test_max_output_is_non_negative(self) -> None:
        profiles = load_model_profiles(
            CONFIG_DIR / "models" / "opencode-go-profiles.yaml"
        )
        for mp in profiles.values():
            assert mp.max_output >= 0

    def test_endpoint_id_matches_loaded_endpoint(self) -> None:
        profiles = load_model_profiles(
            CONFIG_DIR / "models" / "opencode-go-profiles.yaml"
        )
        for mp in profiles.values():
            assert mp.endpoint_id == "opencode-go"


class TestRegistry:
    def test_get_model_returns_none_for_missing(self) -> None:
        profiles = load_model_profiles(
            CONFIG_DIR / "models" / "opencode-go-profiles.yaml"
        )
        registry = ModelRegistry(profiles)
        assert registry.get("nonexistent-model") is None

    def test_get_endpoint_returns_none_for_missing(self) -> None:
        endpoints = load_endpoint_profiles(
            CONFIG_DIR / "providers" / "endpoints.yaml"
        )
        registry = EndpointRegistry(endpoints)
        assert registry.get("nonexistent") is None

    def test_effective_capability_merges_profile_and_attestation(self) -> None:
        from zhiwei.models.attestations import AttestationRegistry

        profiles = load_model_profiles(
            CONFIG_DIR / "models" / "opencode-go-profiles.yaml"
        )
        endpoints = load_endpoint_profiles(
            CONFIG_DIR / "providers" / "endpoints.yaml"
        )
        # No attestations loaded -> effective = declared profile only
        att_registry = AttestationRegistry()
        model_registry = ModelRegistry(profiles)
        endpoint_registry = EndpointRegistry(endpoints)

        first_model = next(iter(profiles.values()))
        effective = model_registry.effective_capability(
            first_model.id, endpoint_registry, att_registry
        )
        assert effective.model_name == first_model.model_name
