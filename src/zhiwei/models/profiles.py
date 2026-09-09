"""S3 Profile loaders: YAML config to typed domain objects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from zhiwei.models.attestations import AttestationRegistry
from zhiwei.models.contracts import (
    ClassificationCeiling,
    CredentialMode,
    EndpointProfile,
    ModelProfile,
    NetworkZone,
    TokenCountingLevel,
    TrustTier,
    WireProtocol,
)

# ADR-011 §4：endpoint 未显式声明 classification_ceiling 时，由 network_zone 决定下落点。
# 门禁的本质是「数据是否离开信任边界」，zone 是这一判断的来源，而不是 URL 是否登记。
_ZONE_CEILING_DEFAULTS: dict[NetworkZone, ClassificationCeiling] = {
    NetworkZone.INTERNAL: ClassificationCeiling.CONFIDENTIAL,
    NetworkZone.EXTERNAL: ClassificationCeiling.INTERNAL,
    NetworkZone.UNKNOWN: ClassificationCeiling.PUBLIC,
}


def _parse_classification_ceiling(value: Any, zone: NetworkZone) -> ClassificationCeiling:
    """显式声明优先；未声明时按 zone 默认档。未知取值 fail closed（ValueError 上抛）。"""
    if value is None:
        return _ZONE_CEILING_DEFAULTS[zone]
    # 档案库与知识层用大写（PUBLIC/INTERNAL/…），域枚举值为小写——归一后再解析。
    return ClassificationCeiling(str(value).lower())


def load_endpoint_profiles(path: Path) -> dict[str, EndpointProfile]:
    """Load endpoint profiles from YAML config file."""
    with open(path) as f:
        data = yaml.safe_load(f)

    defaults = data.get("registered_defaults", {})
    result: dict[str, EndpointProfile] = {}

    for ep_data in data.get("endpoints", []):
        ep_id = ep_data["id"]
        merged = {**defaults, **ep_data}
        zone = NetworkZone(merged.get("network_zone", "unknown"))
        result[ep_id] = EndpointProfile(
            id=ep_id,
            base_url=merged["base_url"],
            credential_mode=CredentialMode(merged.get("credential_mode", "bearer")),
            credential_env=merged.get("credential_env", ""),
            base_url_env=merged.get("base_url_env"),
            model_env=merged.get("model_env"),
            trust_tier=TrustTier(merged.get("trust_tier", "unverified")),
            network_zone=zone,
            classification_ceiling=_parse_classification_ceiling(
                merged.get("classification_ceiling"), zone
            ),
            allowed_paths=tuple(merged.get("allowed_paths", [])),
            billing_mode=merged.get("billing_mode", "unknown"),
            redirect_policy=merged.get("redirect_policy", "deny_cross_origin"),
            extra_spend_allowed=merged.get("extra_spend_allowed", False),
            batch_eval_allowed=merged.get("batch_eval_allowed"),
            project_live_policy=merged.get("project_live_policy", "deny"),
            docs_url=merged.get("docs_url"),
            docs_digest=merged.get("docs_digest"),
            terms_url=merged.get("terms_url"),
            terms_digest=merged.get("terms_digest"),
            terms_reviewed_at=_coerce_date_str(merged.get("terms_reviewed_at")),
            terms_review_due_at=_coerce_date_str(merged.get("terms_review_due_at")),
            privacy_url=merged.get("privacy_url"),
            privacy_digest=merged.get("privacy_digest"),
            risk_acceptance_artifact=merged.get("risk_acceptance_artifact"),
        )

    return result


def _coerce_date_str(value: Any) -> str | None:
    """Coerce date/datetime objects from YAML to ISO string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def load_model_profiles(
    path: Path,
    *,
    required_transport: str | None = None,
) -> dict[str, ModelProfile]:
    """Load model profiles from YAML config file."""
    with open(path) as f:
        data = yaml.safe_load(f)

    result: dict[str, ModelProfile] = {}

    for entry in data.get("profiles", []):
        mp = _parse_model_profile(entry)
        if required_transport and mp.wire_protocol.value != required_transport:
            raise ValueError(
                f"Model {mp.id} uses transport {mp.wire_protocol.value}, "
                f"expected {required_transport}"
            )
        result[mp.id] = mp

    return result


def _parse_model_profile(data: dict[str, Any]) -> ModelProfile:
    """Parse a single model profile entry from YAML data."""
    wire_protocol_str = data.get("transport", "openai_chat")
    wire_protocol = WireProtocol(wire_protocol_str)

    modalities = data.get("modalities", ["text"])
    profile_source = data.get("profile_source")
    if isinstance(profile_source, dict):
        profile_source = profile_source.get("url")

    return ModelProfile(
        id=data.get("stable_id") or data.get("id", ""),
        endpoint_id=data.get("endpoint_id", ""),
        model_name=data.get("display_id") or data.get("model_name", ""),
        display_name=data.get("display_id") or data.get("display_name", ""),
        wire_protocol=wire_protocol,
        api_path=data.get("request_path") or data.get("api_path", "/chat/completions"),
        context_window=data.get("context_window") or 128_000,
        max_output=data.get("max_output_tokens") or data.get("max_output") or 0,
        max_input=data.get("max_input_tokens") or data.get("max_input"),
        modalities=tuple(modalities),
        structured_output=data.get("structured_output") or "none",
        tool_choice=data.get("tool_choice") or "auto",
        reasoning_field=data.get("reasoning_field"),
        verification_level=data.get("verification_level") or "declared",
        token_counting_level=TokenCountingLevel(
            data.get("token_counting_level") or "calibrated_estimate"
        ),
        max_wire_body_bytes=data.get("max_wire_body_bytes") or 8_388_608,
        profile_source=profile_source,
    )


def load_endpoint_profiles_from_env(
    env_overrides: dict[str, str],
) -> EndpointProfile:
    """Create an endpoint profile from environment variable overrides (ADR-011).

    OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL override config defaults.
    """
    base_url = env_overrides.get("OPENAI_BASE_URL", "")

    if not base_url:
        raise ValueError("OPENAI_BASE_URL must be set for env override")

    return EndpointProfile(
        id="__env_override__",
        base_url=base_url,
        credential_env="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",
        model_env="OPENAI_MODEL",
        trust_tier=TrustTier.UNVERIFIED,
        network_zone=NetworkZone.UNKNOWN,
        classification_ceiling=ClassificationCeiling.PUBLIC,
        allowed_paths=("/chat/completions", "/responses", "/messages"),
        billing_mode="unknown",
    )


def resolve_default_endpoint(
    env_overrides: dict[str, str],
    endpoints_path: Path,
) -> EndpointProfile:
    """Resolve the deployment's default endpoint per ADR-011 §2 priority.

    env override（OPENAI_BASE_URL）高于配置文件的 default_endpoint_id；BASE_URL 是
    endpoint 身份的锚点，只有它出现才走 override 路径——只配 MODEL/API_KEY 时不与
    配置文件默认 endpoint 做局部合并，避免「这半份配置来自哪」无法回答。
    """
    if env_overrides.get("OPENAI_BASE_URL", ""):
        return load_endpoint_profiles_from_env(env_overrides)

    default_id = EndpointRegistry.find_default_endpoint_id(endpoints_path)
    endpoint = load_endpoint_profiles(endpoints_path).get(default_id)
    if endpoint is None:
        raise ValueError(
            f"default_endpoint_id '{default_id}' has no entry in {endpoints_path}"
        )
    return endpoint


class EndpointRegistry:
    """Registry of loaded endpoint profiles with lookup."""

    def __init__(self, endpoints: dict[str, EndpointProfile] | None = None) -> None:
        self._endpoints: dict[str, EndpointProfile] = endpoints or {}

    def get(self, endpoint_id: str) -> EndpointProfile | None:
        return self._endpoints.get(endpoint_id)

    def all(self) -> dict[str, EndpointProfile]:
        return dict(self._endpoints)

    @staticmethod
    def find_default_endpoint_id(path: Path) -> str:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data["default_endpoint_id"]

    @staticmethod
    def create_floor_endpoint(base_url: str) -> EndpointProfile:
        """Create a floor-trust endpoint for unregistered URLs (ADR-011)."""
        return EndpointProfile(
            id="__unregistered_floor__",
            base_url=base_url,
            credential_env="",
            trust_tier=TrustTier.UNVERIFIED,
            network_zone=NetworkZone.UNKNOWN,
            classification_ceiling=ClassificationCeiling.PUBLIC,
            allowed_paths=("/chat/completions", "/responses", "/messages"),
            billing_mode="unknown",
        )


class ModelRegistry:
    """Registry of loaded model profiles with effective capability lookup."""

    def __init__(self, profiles: dict[str, ModelProfile] | None = None) -> None:
        self._profiles: dict[str, ModelProfile] = profiles or {}

    def get(self, model_id: str) -> ModelProfile | None:
        return self._profiles.get(model_id)

    def all(self) -> dict[str, ModelProfile]:
        return dict(self._profiles)

    def effective_capability(
        self,
        model_id: str,
        endpoint_registry: EndpointRegistry,
        attestation_registry: AttestationRegistry,
    ) -> ModelProfile:
        """Resolve effective capability: static profile + latest valid attestation.

        Per docs/MODELS.md §3: effective_capability = immutable_profile_claim + latest_valid_attestation.
        If no valid attestation exists, the declared profile is returned as-is.
        """
        profile = self._profiles.get(model_id)
        if profile is None:
            raise KeyError(f"Model profile not found: {model_id}")

        attestation = attestation_registry.get_latest(
            profile.endpoint_id, profile.model_name
        )
        if attestation is None:
            return profile

        # Attestation can upgrade verification_level but not change profile fields
        if attestation.qualification_level != "declared":
            return profile.model_copy(
                update={"verification_level": attestation.qualification_level}
            )

        return profile
