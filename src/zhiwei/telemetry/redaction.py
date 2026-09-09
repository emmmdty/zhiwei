"""S9-T6：metadata-only 遥测视图与 no-secret/PII 扫描（specs/s9 §6）。

纪律：默认遥测只含 metadata/digest——正文键（prompt/result/…）在进入任何
span/日志/metric 之前被剥离；policy 显式声明的正文键按白名单放行（fail closed），
而不是「默认放行、发现泄漏再脱敏」。scan_no_secret 是回归探针：canary 哨兵
与 PII 模式命中即产出 finding，且 finding 只报告位置、不复制原文——扫描报告
本身不得成为泄露面。

与 persistence 层的 scrub_hidden_reasoning（S3，canonical 写入前销毁正文）分工：
那一层守护持久化事实源，本层守护可观测性出口；两层独立存在，缺一不可。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

# 与 runtime 事件/活动形状对齐的正文位（runtime/events.py 的 output_values/error、
# handlers/base.py 的 input_values、模型 I/O 的 prompt/messages/completion/response、
# 工具调用的 tool_args/tool_result、memory/策略的 reasoning/reason 自由文本）。
# 这些键在任何默认遥测视图中不存在——新增正文位必须显式进入本集合并说明来源。
DEFAULT_BODY_KEYS: frozenset[str] = frozenset(
    {
        "prompt",
        "messages",
        "result",
        "completion",
        "tool_args",
        "tool_result",
        "input_values",
        "output_values",
        "response",
        "reasoning",
        "error",
        "reason",
    }
)


class RedactionCode(StrEnum):
    """finding 的封闭分类；新增类别需扩展 no-secret 扫描的判定维度。"""

    SECRET_SENTINEL = "secret_sentinel"
    PII_SENTINEL = "pii_sentinel"


class RedactionFinding(BaseModel):
    """扫描命中项：只有类别与字段路径，没有原文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: RedactionCode
    field: str


def metadata_only_view(
    event: Mapping[str, Any],
    policy_enabled_bodies: Sequence[str] = (),
) -> dict[str, Any]:
    """返回剥离正文键后的视图；policy 白名单显式放行的正文键保留。

    递归作用于嵌套 mapping/list——正文藏在 attributes/output_values 内层时同样
    剥离。非 mapping 输入一律拒绝：本函数的输入形状是「事件」，不是任意值。
    """
    if not isinstance(event, Mapping):
        raise ValueError("metadata_only_view requires a mapping event")
    enabled = frozenset(policy_enabled_bodies)
    stripped = _strip_keys(event, enabled)
    return dict(stripped) if isinstance(stripped, dict) else {}


def scan_no_secret(
    *,
    payload: Any,
    sentinels: Sequence[str],
    pii_patterns: Sequence[str] = (),
) -> tuple[RedactionFinding, ...]:
    """递归扫描 payload，返回命中 finding（不含原文）。

    哨兵命中 → SECRET_SENTINEL；PII 模式命中 → PII_SENTINEL。空哨兵/空模式
    合法（跳过对应维度）。只扫字符串叶子：数值/布尔不构成泄露面。
    """
    active_sentinels = tuple(s for s in sentinels if s)
    patterns = tuple(re.compile(p) for p in pii_patterns if p)
    findings: list[RedactionFinding] = []
    _scan(payload, (), active_sentinels, patterns, findings)
    return tuple(findings)


def _strip_keys(value: Any, enabled: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _strip_keys(item, enabled)
            for key, item in value.items()
            if key not in DEFAULT_BODY_KEYS or key in enabled
        }
    if isinstance(value, (list, tuple)):
        return [_strip_keys(item, enabled) for item in value]
    return value


def _scan(
    value: Any,
    path: tuple[str, ...],
    sentinels: Sequence[str],
    patterns: Sequence[re.Pattern[str]],
    findings: list[RedactionFinding],
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _scan(item, (*path, str(key)), sentinels, patterns, findings)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan(item, (*path, str(index)), sentinels, patterns, findings)
        return
    if not isinstance(value, str):
        return
    field = ".".join(path)
    for sentinel in sentinels:
        if sentinel in value:
            findings.append(RedactionFinding(code=RedactionCode.SECRET_SENTINEL, field=field))
    for pattern in patterns:
        if pattern.search(value):
            findings.append(RedactionFinding(code=RedactionCode.PII_SENTINEL, field=field))
