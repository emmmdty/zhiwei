"""S3 §5 architecture：Core/transport 不含任何具体 endpoint 名称分支（ADR-010 §4）。

provider 中立的可迁移性主张必须可验证：新增 endpoint 只能通过新增 EndpointProfile +
attestation 分级完成，不改 Core/transport 代码。本测试遍历真实 src/zhiwei/models 与
src/zhiwei/context 的 .py 源码，断言不含具体 endpoint 实例的字面名称/origin。

判定规则：
- 禁止字面量 = 具体 endpoint 实例的名称/origin：聚合服务实例名（opencode）、
  厂商域名（openai.com、anthropic.com）、具体厂商名（deepseek）。任一出现在
  Core/transport 源码即意味着「换 provider 要改 Core 代码」，违反 ADR-010 §4。
- 白名单（不算 endpoint 实例名）：
  * OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL —— OpenAI 兼容 wire protocol
    的标准环境变量键名（ADR-010 §0：协议名，不是 endpoint 实例名）；
  * openai_chat / openai_responses / anthropic_messages —— WireProtocol 枚举值，
    wire protocol 名称而非 endpoint origin；
  * config/providers/*.yaml、docs/ 中的实例配置与 profiles 加载路径 —— 属部署
    配置层（composition root），不在本测试遍历范围（只遍历 models/context 源码）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_NEUTRAL_PACKAGES = ("zhiwei.models", "zhiwei.context")

# 具体 endpoint 实例名/origin（统一小写比较，防大小写变体绕过）
_BANNED_ENDPOINT_LITERALS = ("opencode", "openai.com", "anthropic.com", "deepseek")

_ZHIWEI_ROOT = Path(importlib.import_module("zhiwei").__file__ or "").parent


def _neutral_sources() -> list[Path]:
    sources: list[Path] = []
    for package_name in _NEUTRAL_PACKAGES:
        package = importlib.import_module(package_name)
        package_root = Path(package.__file__ or "").parent
        assert package_root.is_dir(), f"{package_name} 不是常规文件系统包"
        sources.extend(package_root.rglob("*.py"))
    assert sources, "待检源码清单不得为空（空清单使本测试恒真）"
    return sorted(set(sources))


_NEUTRAL_SOURCES = _neutral_sources()


def _source_id(path: Path) -> str:
    return str(path.relative_to(_ZHIWEI_ROOT))


@pytest.mark.parametrize("source_path", _NEUTRAL_SOURCES, ids=[_source_id(p) for p in _NEUTRAL_SOURCES])
class TestNoConcreteEndpointNames:
    def test_source_has_no_endpoint_instance_literal(self, source_path: Path) -> None:
        source = source_path.read_text(encoding="utf-8").lower()
        for literal in _BANNED_ENDPOINT_LITERALS:
            assert literal not in source, (
                f"{_source_id(source_path)} 含具体 endpoint 实例字面量 {literal!r}"
                "（ADR-010 §4：新增 provider 不得改 Core/transport 代码）"
            )
