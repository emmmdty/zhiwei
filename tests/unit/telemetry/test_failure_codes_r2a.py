"""R2-A：failure taxonomy 词汇扩展（design §12 缺口补齐）。

既有封闭枚举缺少 5 个 design-§12 机器码；补齐后 closed-enum 形态契约
（test_failure_taxonomy_frozen.py，遍历全部成员）保持不变。新码必须可分类、
未知码仍然拒绝（fail closed 不放宽）。
"""

from __future__ import annotations

import pytest

from zhiwei.telemetry.failures import FailureCode, UnknownFailureCode, classify_failure

R2A_CODES = (
    "EFFECT_UNKNOWN",
    "ARTIFACT_CORRUPT",
    "DEPENDENCY_UNAVAILABLE",
    "KNOWLEDGE_STALE",
    "KNOWLEDGE_ACL_DENY",
)


class TestR2AVocabulary:
    @pytest.mark.parametrize("code", R2A_CODES)
    def test_new_code_classifies(self, code: str) -> None:
        assert classify_failure({"code": code}) is FailureCode(code)

    @pytest.mark.parametrize("code", R2A_CODES)
    def test_new_code_is_enum_member(self, code: str) -> None:
        assert code in {member.value for member in FailureCode}

    def test_unknown_still_refused(self) -> None:
        # 词汇扩展不是自由字符串化：非成员大写蛇形仍然拒绝。
        with pytest.raises(UnknownFailureCode):
            classify_failure({"code": "KNOWLEDGE_STALE_ISH"})
        with pytest.raises(UnknownFailureCode):
            classify_failure({"code": "MODEL_TIMEOUTX"})
