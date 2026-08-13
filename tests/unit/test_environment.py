"""开工前的环境基线断言。

这里只断言开发环境本身，不断言任何产品行为——产品行为一律由各阶段 spec 的 RED 测试定义。
它的存在让 `pytest` 在 S0 第一个 Task 落地前就有确定的绿基线，CI 不会因为「零测试」而返回 exit 5。
"""

import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS_CONFIG = REPO_ROOT / "config" / "providers" / "endpoints.yaml"


def _normalize_base_url(raw: str) -> str:
    """按 endpoints.yaml 的 path_normalization 规则做最小规范化。

    完整规则由 S3 的 endpoint allowlist 实现；此处只覆盖比对 base_url 所需的部分，
    足以让「配错 endpoint」在开发环境就暴露。
    """
    value = raw.strip().rstrip("/")
    scheme, sep, rest = value.partition("://")
    if not sep:
        return value.lower()
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


def _registered_base_urls() -> set[str]:
    config = yaml.safe_load(ENDPOINTS_CONFIG.read_text(encoding="utf-8"))
    return {
        _normalize_base_url(endpoint["base_url"])
        for endpoint in config["endpoints"]
        if endpoint.get("base_url")
    }


def test_python_version_meets_floor() -> None:
    """pyproject 要求 >=3.11；低于此版本时大量 typing 与 asyncio 行为不一致。"""
    assert sys.version_info >= (3, 11), f"需要 Python 3.11+，当前 {sys.version_info}"


def test_openai_base_url_must_be_a_registered_endpoint() -> None:
    """键名通用，值必须已登记。

    Agent 运行时统一使用 OpenAI 兼容 provider，因此 `OPENAI_BASE_URL` 是标准键名。但它指向哪里
    不能自由——必须是 `config/providers/endpoints.yaml` 中已登记的 endpoint，否则 allowlist、
    数据分类与用量账本都会被绕过。见 docs/DECISIONS.md ADR-010。
    """
    configured = os.environ.get("OPENAI_BASE_URL")
    if not configured:
        return  # 未配置时不适用；fixture-only 是默认运行方式

    registered = _registered_base_urls()
    assert _normalize_base_url(configured) in registered, (
        f"OPENAI_BASE_URL={configured!r} 不在已登记 endpoint 中。\n"
        f"已登记：{sorted(registered)}\n"
        f"新增 endpoint 必须先写入 {ENDPOINTS_CONFIG.relative_to(REPO_ROOT)} 并通过策略复核。"
    )


def test_default_endpoint_declares_openai_compatible_keys() -> None:
    """默认 endpoint 必须声明标准键名，否则 .env.example 与实际读取路径会漂移。"""
    config = yaml.safe_load(ENDPOINTS_CONFIG.read_text(encoding="utf-8"))
    default_id = config.get("default_endpoint_id")
    assert default_id, "endpoints.yaml 必须声明 default_endpoint_id"

    default = next((e for e in config["endpoints"] if e["id"] == default_id), None)
    assert default is not None, f"default_endpoint_id={default_id!r} 在 endpoints 中不存在"
    assert default.get("credential_env") == "OPENAI_API_KEY"
    assert default.get("base_url_env") == "OPENAI_BASE_URL"
    assert default.get("model_env") == "OPENAI_MODEL"


def test_test_run_is_not_in_live_mode() -> None:
    """常规测试不得处于 live 模式——live 只由 operator 显式触发。"""
    mode = os.environ.get("ZHIWEI_RELEASE_MODE", "fixture_only")
    assert mode != "live", "测试进程不得在 ZHIWEI_RELEASE_MODE=live 下运行"
