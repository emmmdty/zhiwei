"""S3 Security: model egress — endpoint trust tier and data classification gates.

验证 ADR-011 的分级信任与数据门禁走真实生产代码路径（profiles 加载器 / EndpointRegistry /
ModelRouter / CaptureTransport pre-send gate），全程 loopback + mock transport，不发真实请求：

1. 未登记 endpoint 落入 unverified 信任档（ceiling=PUBLIC、zone=unknown、无 attestation 可声称）
2. network_zone 决定 classification_ceiling：internal 严格高于 external
3. pre-send 分类门禁：context 实际分类超过 endpoint ceiling 时拒绝发送（见 test_presend_classification.py）
4. env override（OPENAI_BASE_URL 等）优先级高于 endpoints.yaml default_endpoint_id
"""

from __future__ import annotations

from pathlib import Path

import yaml

from zhiwei.models.attestations import AttestationRegistry
from zhiwei.models.contracts import (
    ClassificationCeiling,
    NetworkZone,
    TrustTier,
    WireProtocol,
)
from zhiwei.models.profiles import (
    EndpointRegistry,
    ModelProfile,
    load_endpoint_profiles,
    load_endpoint_profiles_from_env,
    resolve_default_endpoint,
)
from zhiwei.models.router import ModelRouter, RoutingRequest

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

# loopback 端口 9（discard）只作为 URL 标识使用，测试不真正连接。
_UNREGISTERED_URL = "http://127.0.0.1:9/v1"


def _make_model(endpoint_id: str, model_id: str = "m1") -> ModelProfile:
    return ModelProfile(
        id=model_id,
        endpoint_id=endpoint_id,
        model_name="test-model",
        wire_protocol=WireProtocol.OPENAI_CHAT,
        api_path="/chat/completions",
        context_window=128_000,
    )


def _write_endpoints_yaml(path: Path, endpoints: list[dict]) -> Path:
    """写一份最小档案库配置：zone 决定 ceiling 的行为只能用测试配置验证，
    因为已冻结的 config/providers/endpoints.yaml 不允许改动。"""
    data = {
        "default_endpoint_id": endpoints[0]["id"],
        "registered_defaults": {"trust_tier": "reviewed"},
        "endpoints": endpoints,
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- unverified tier


class TestUnregisteredEndpointTrustTier:
    """未登记 base_url 可接入，但起点必须是 runtime_registration_floor。"""

    def test_floor_endpoint_is_unverified_public_unknown(self) -> None:
        ep = EndpointRegistry.create_floor_endpoint(_UNREGISTERED_URL)
        assert ep.trust_tier == TrustTier.UNVERIFIED
        assert ep.classification_ceiling == ClassificationCeiling.PUBLIC
        assert ep.network_zone == NetworkZone.UNKNOWN

    def test_env_override_endpoint_lands_at_floor(self) -> None:
        """经 env 引入的未登记 endpoint 走同一 floor，不能因「运维配了」就获得信任档。"""
        ep = load_endpoint_profiles_from_env({
            "OPENAI_BASE_URL": _UNREGISTERED_URL,
            "OPENAI_MODEL": "qwen3-32b",
            "OPENAI_API_KEY": "test-key",
        })
        assert ep.base_url == _UNREGISTERED_URL
        assert ep.trust_tier == TrustTier.UNVERIFIED
        assert ep.classification_ceiling == ClassificationCeiling.PUBLIC
        assert ep.network_zone == NetworkZone.UNKNOWN

    def test_no_attestation_can_exist_for_unregistered_endpoint(self) -> None:
        """能力全 unknown：无 probe 就无 attestation，任何能力声明都无从支撑。"""
        ep = EndpointRegistry.create_floor_endpoint(_UNREGISTERED_URL)
        registry = AttestationRegistry()
        assert registry.get_latest(ep.id, "any-model") is None

    def test_router_rejects_unverified_endpoint_for_any_data(self) -> None:
        """不可支撑对外能力/数据外发的代码级体现：data_compliance 硬门禁直接拒绝。"""
        ep = EndpointRegistry.create_floor_endpoint(_UNREGISTERED_URL)
        model = _make_model(ep.id)
        decision = ModelRouter().route(RoutingRequest(
            candidates=[model],
            endpoints={ep.id: ep},
            quality_scores={model.id: 1.0},
            data_classification="public",
        ))
        assert decision.is_rejected
        assert "unverified" in decision.rejection_reason.lower()


# --------------------------------------------------------------------------- zone → ceiling


class TestNetworkZoneDeterminesCeiling:
    """ADR-011 §4：真正的数据门禁是 network_zone × classification_ceiling。"""

    def test_internal_zone_ceiling_strictly_above_external(self, tmp_path: Path) -> None:
        path = _write_endpoints_yaml(tmp_path / "endpoints.yaml", [
            {
                "id": "internal-llm",
                "base_url": _UNREGISTERED_URL,
                "credential_env": "OPENAI_API_KEY",
                "allowed_paths": ["/chat/completions"],
                "network_zone": "internal",
            },
            {
                "id": "external-saas",
                "base_url": "https://external.example.com/v1",
                "credential_env": "EXT_API_KEY",
                "allowed_paths": ["/chat/completions"],
                "network_zone": "external",
            },
        ])
        endpoints = load_endpoint_profiles(path)
        internal = endpoints["internal-llm"].classification_ceiling
        external = endpoints["external-saas"].classification_ceiling
        assert internal > external

    def test_explicit_ceiling_declaration_wins_over_zone_default(self, tmp_path: Path) -> None:
        """组织可针对具体 endpoint 显式收紧/放宽，zone 默认值只是未声明时的下落点。"""
        path = _write_endpoints_yaml(tmp_path / "endpoints.yaml", [
            {
                "id": "external-strict",
                "base_url": "https://external.example.com/v1",
                "credential_env": "EXT_API_KEY",
                "allowed_paths": ["/chat/completions"],
                "network_zone": "external",
                "classification_ceiling": "PUBLIC",
            },
        ])
        ep = load_endpoint_profiles(path)["external-strict"]
        assert ep.classification_ceiling == ClassificationCeiling.PUBLIC

    def test_registered_defaults_ceiling_is_not_dropped(self) -> None:
        """endpoints.yaml 的 registered_defaults 声明了 classification_ceiling，
        加载器必须保留该治理属性，而不是静默回落到 PUBLIC。"""
        endpoints = load_endpoint_profiles(CONFIG_DIR / "providers" / "endpoints.yaml")
        default_ep = endpoints[EndpointRegistry.find_default_endpoint_id(
            CONFIG_DIR / "providers" / "endpoints.yaml"
        )]
        assert default_ep.classification_ceiling == ClassificationCeiling.INTERNAL


# --------------------------------------------------------------------------- env override


class TestEnvOverridePriority:
    """ADR-011 §2：OPENAI_BASE_URL 等 env override 优先于 endpoints.yaml 的 default_endpoint_id。"""

    def test_env_base_url_wins_over_config_default(self) -> None:
        resolved = resolve_default_endpoint(
            {
                "OPENAI_BASE_URL": _UNREGISTERED_URL,
                "OPENAI_MODEL": "qwen3-32b",
                "OPENAI_API_KEY": "test-key",
            },
            CONFIG_DIR / "providers" / "endpoints.yaml",
        )
        assert resolved.base_url == _UNREGISTERED_URL
        assert resolved.base_url != "https://opencode.ai/zen/go/v1"

    def test_without_env_falls_back_to_default_endpoint_id(self) -> None:
        resolved = resolve_default_endpoint({}, CONFIG_DIR / "providers" / "endpoints.yaml")
        assert resolved.id == EndpointRegistry.find_default_endpoint_id(
            CONFIG_DIR / "providers" / "endpoints.yaml"
        )
        assert resolved.base_url == "https://opencode.ai/zen/go/v1"
