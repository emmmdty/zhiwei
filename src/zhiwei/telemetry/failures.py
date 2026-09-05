"""S9-T6：failure taxonomy 的封闭 machine code 词汇（specs/s9 §6）。

与 runtime/failures.py（S2 的 FailureCategory，执行层重试语义）分工：本模块是
dashboard/observability 面向的机器码词汇表——dashboard 状态从 canonical/projection
按 code 构建，不从自由文本猜状态（classify 拒绝自由文本与非 mapping，fail closed）。
code 值形态冻结：大写蛇形、非空、纯字母数字下划线。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

# machine code 形态：大写开头的大写蛇形（也是「自由文本不是 code」的第一道判定——
# 「the model timed out」含空格/小写，直接拒绝，不做语义猜测）。
_MACHINE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class UnknownFailureCode(ValueError):
    """Raised when a failure payload carries no closed-vocabulary machine code."""


class FailureCode(StrEnum):
    """封闭失败码。新增码是显式的词汇扩展，不是自由字符串。"""

    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_REFUSAL = "MODEL_REFUSAL"
    POLICY_DENY = "POLICY_DENY"
    APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    TOOL_ERROR = "TOOL_ERROR"
    TOOL_DENIED = "TOOL_DENIED"
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    DELEGATION_CYCLE = "DELEGATION_CYCLE"
    WORKFLOW_ERROR = "WORKFLOW_ERROR"
    UNKNOWN = "UNKNOWN"


def classify_failure(failure: Mapping[str, Any]) -> FailureCode:
    """从失败载荷读取显式 machine code；无 code/未知 code/自由文本一律拒绝。"""
    if not isinstance(failure, Mapping):
        raise UnknownFailureCode("failure must be a mapping with an explicit machine code")
    code = failure.get("code")
    if not isinstance(code, str) or _MACHINE_CODE_RE.fullmatch(code) is None:
        raise UnknownFailureCode(f"failure code is not a machine code: {code!r}")
    try:
        return FailureCode(code)
    except ValueError as exc:
        raise UnknownFailureCode(f"unknown failure code: {code!r}") from exc
