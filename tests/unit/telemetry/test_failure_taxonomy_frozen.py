"""S9 冻结契约：failure taxonomy 固定 machine code，禁止字符串猜测（A 档，S9-T6）。

dashboard 状态从 canonical/projection 构建，不从自由文本日志猜状态：
classify 只接受显式 machine code，未知 code/自由文本一律拒绝（fail closed）。
"""

from __future__ import annotations

import pytest

from zhiwei.telemetry.failures import FailureCode, UnknownFailureCode, classify_failure


class TestClosedEnum:
    def test_known_code_maps(self) -> None:
        assert classify_failure({"code": "MODEL_TIMEOUT"}) is FailureCode.MODEL_TIMEOUT

    def test_unknown_code_refused(self) -> None:
        with pytest.raises(UnknownFailureCode):
            classify_failure({"code": "MODEL_TIMED_OUT_EVENTUALLY"})

    def test_free_text_is_not_a_code(self) -> None:
        # 「the model timed out」不是 machine code——不得从字符串猜状态。
        with pytest.raises(UnknownFailureCode):
            classify_failure({"code": "the model timed out"})

    def test_missing_code_refused(self) -> None:
        with pytest.raises(UnknownFailureCode):
            classify_failure({"detail": "no code here"})

    def test_non_mapping_refused(self) -> None:
        with pytest.raises(UnknownFailureCode):
            classify_failure("MODEL_TIMEOUT")  # type: ignore[arg-type]

    def test_code_values_are_machine_shape(self) -> None:
        # machine code 形态冻结：大写蛇形，非空。
        for code in FailureCode:
            assert code.value
            assert code.value == code.value.upper()
            assert " " not in code.value
            assert code.value.replace("_", "").isalnum()
