"""开工前的环境基线断言。

这里只断言开发环境与配置档案库本身，不断言任何产品行为——产品行为一律由各阶段 spec 的 RED
测试定义。它的存在让 `pytest` 在 S0 第一个 Task 落地前就有确定的绿基线，CI 不会因为「零测试」
而返回 exit 5。

注意这里**不**断言 `OPENAI_BASE_URL` 必须出现在 endpoints.yaml 中：企业自部署的内部 LLM 由运维在
部署期接入，本仓库无从预先枚举，硬性要求登记会阻断最该被支持的部署形态。未登记 endpoint 的降级
处理（unverified 档）是运行时行为，由 S3 的 RED 覆盖。见 docs/DECISIONS.md ADR-011。
"""

import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS_CONFIG = REPO_ROOT / "config" / "providers" / "endpoints.yaml"

VALID_TRUST_TIERS = {"reviewed", "operator_declared", "unverified"}
VALID_NETWORK_ZONES = {"internal", "external", "unknown"}
# 由低到高，索引即严格程度
CLASSIFICATION_ORDER = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]


def _load_config() -> dict:
    return yaml.safe_load(ENDPOINTS_CONFIG.read_text(encoding="utf-8"))


def test_python_version_meets_floor() -> None:
    """pyproject 要求 >=3.11；低于此版本时大量 typing 与 asyncio 行为不一致。"""
    assert sys.version_info >= (3, 11), f"需要 Python 3.11+，当前 {sys.version_info}"


def test_default_endpoint_declares_openai_compatible_keys() -> None:
    """默认 endpoint 必须声明标准键名，否则 .env.example 与实际读取路径会漂移。"""
    config = _load_config()
    default_id = config.get("default_endpoint_id")
    assert default_id, "endpoints.yaml 必须声明 default_endpoint_id"

    default = next((e for e in config["endpoints"] if e["id"] == default_id), None)
    assert default is not None, f"default_endpoint_id={default_id!r} 在 endpoints 中不存在"
    assert default.get("credential_env") == "OPENAI_API_KEY"
    assert default.get("base_url_env") == "OPENAI_BASE_URL"
    assert default.get("model_env") == "OPENAI_MODEL"


def test_registered_defaults_are_well_formed() -> None:
    """已登记条目的共同属性必须齐全且取值合法，否则信任判定无从进行。"""
    defaults = _load_config().get("registered_defaults")
    assert defaults, "endpoints.yaml 必须声明 registered_defaults"

    assert defaults["trust_tier"] in VALID_TRUST_TIERS
    assert defaults["network_zone"] in VALID_NETWORK_ZONES
    assert defaults["classification_ceiling"] in CLASSIFICATION_ORDER


def test_runtime_registration_floor_stays_most_conservative() -> None:
    """未登记 endpoint 的起点是**下限**，不是可调默认值。

    这是本文件里唯一真正的安全不变量：接入不被阻断，但未经审查的 endpoint 必须从最保守的位置
    起步。任何把这个 floor 放宽的配置改动都应该在这里变红——提升只能由 Security Admin 针对
    具体 endpoint 显式做出，并写入 canonical event，不能通过改配置文件默认值悄悄完成。
    """
    floor = _load_config().get("runtime_registration_floor")
    assert floor, "endpoints.yaml 必须声明 runtime_registration_floor"

    assert floor["trust_tier"] == "unverified", "未登记 endpoint 不得以更高信任档起步"
    assert floor["network_zone"] == "unknown", "未声明网络区域不得假定为内网"
    assert floor["classification_ceiling"] == CLASSIFICATION_ORDER[0], (
        "未登记 endpoint 的数据分类上限必须是最保守的 PUBLIC"
    )
    assert floor["capabilities"] == "unknown", "未登记 endpoint 的能力不得预设，必须 probe"
    assert floor["eligible_for_public_claims"] is False, "未验证 endpoint 不得支撑对外能力声明"


def test_floor_is_not_looser_than_registered_defaults() -> None:
    """floor 必须严格不宽于已登记条目的默认值，否则「降级」二字失去意义。"""
    config = _load_config()
    floor_level = CLASSIFICATION_ORDER.index(config["runtime_registration_floor"]["classification_ceiling"])
    registered_level = CLASSIFICATION_ORDER.index(config["registered_defaults"]["classification_ceiling"])
    assert floor_level <= registered_level, "运行时注册下限不得宽于已登记条目的默认分类上限"


def test_test_run_is_not_in_live_mode() -> None:
    """常规测试不得处于 live 模式——live 只由 operator 显式触发。"""
    mode = os.environ.get("ZHIWEI_RELEASE_MODE", "fixture_only")
    assert mode != "live", "测试进程不得在 ZHIWEI_RELEASE_MODE=live 下运行"
