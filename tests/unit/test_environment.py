"""开工前的环境基线断言。

这里只断言开发环境本身，不断言任何产品行为——产品行为一律由各阶段 spec 的 RED 测试定义。
它的存在让 `pytest` 在 S0 第一个 Task 落地前就有确定的绿基线，CI 不会因为「零测试」而返回 exit 5。
"""

import sys


def test_python_version_meets_floor() -> None:
    """pyproject 要求 >=3.11；低于此版本时大量 typing 与 asyncio 行为不一致。"""
    assert sys.version_info >= (3, 11), f"需要 Python 3.11+，当前 {sys.version_info}"


# 本项目自己定义的 endpoint 凭据键，来源为 .env.example。
# 只检查这个集合：开发机上与本项目无关的第三方 key（编辑器、MCP 服务等）不在管辖范围内。
PROJECT_CREDENTIAL_KEYS = frozenset(
    {
        "OPENCODE_GO_API_KEY",
        "MINIMAX_API_KEY",
        "KIMI_API_KEY",
        "GLM_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "ALIBABA_TOKEN_PLAN_API_KEY",
        "VOLCANO_CODING_PLAN_API_KEY",
    }
)

# ADR-010：通用 OpenAI-compatible 凭据会绕过 endpoint allowlist、数据分类与预算账本，一律禁止。
FORBIDDEN_GENERIC_KEYS = frozenset({"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"})


def test_no_live_provider_credentials_in_process_env() -> None:
    """常规测试进程不得携带本项目的 live 凭据——live 只能由 operator 显式触发。"""
    import os

    present = sorted(key for key in PROJECT_CREDENTIAL_KEYS if os.environ.get(key))
    assert not present, f"测试进程不应携带本项目 endpoint 凭据: {present}"


def test_generic_openai_credentials_are_not_used() -> None:
    """通用 OPENAI_* 键被 ADR-010 禁止：它无法表达 endpoint 身份，会绕过 allowlist 与预算账本。"""
    import os

    present = sorted(key for key in FORBIDDEN_GENERIC_KEYS if os.environ.get(key))
    assert not present, (
        f"检测到被禁止的通用凭据 {present}；请改用 .env.example 中的 per-endpoint 键名"
    )
