"""S9 冻结契约：scorer 隔离与人评协议（A 档，S9-T2）。

scorer 输入面不得包含隐藏目标或被测系统生成器内部状态；确定性 scorer 必须是纯函数；
human/judge 协议必须保存 rubric/blinding/order/calibration/agreement，且仅用于
inference/utility 模式（human/live/shadow），不得进入内部冻结确认口径。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zhiwei.evals.domain import EvalMode, RegisteredUnit
from zhiwei.evals.scorers.base import ScorerInput, ScorerVerdict
from zhiwei.evals.scorers.generic import ExactMatchScorer
from zhiwei.evals.scorers.human import HumanJudgeProtocol, HumanJudgeRefused


def _unit(sample_id: str = "s-1", unit_id: str = "u-1") -> RegisteredUnit:
    return RegisteredUnit(sample_id=sample_id, unit_id=unit_id)


class TestScorerInputIsolation:
    def test_hidden_target_field_is_rejected(self) -> None:
        # 隐藏目标（数据集答案之外的生成器内部状态）不允许进入评分输入面。
        with pytest.raises(ValidationError):
            ScorerInput(
                unit=_unit(),
                output={"answer": "42"},
                reference={"answer": "42"},
                hidden_target="the-real-answer",
            )

    def test_generator_internals_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScorerInput(
                unit=_unit(),
                output={"answer": "42"},
                reference={"answer": "42"},
                generator_state={"temperature": 0.0},
            )

    def test_visible_surface_is_constructible(self) -> None:
        payload = ScorerInput(
            unit=_unit(),
            output={"answer": "42"},
            reference={"answer": "42"},
        )
        assert payload.unit.sample_id == "s-1"
        assert payload.context == {}


class TestExactMatchScorer:
    def test_deterministic_pure_function(self) -> None:
        scorer = ExactMatchScorer(output_field="answer", reference_field="answer")
        first = scorer.score(
            ScorerInput(
                unit=_unit(),
                output={"answer": "42"},
                reference={"answer": "42"},
            )
        )
        second = scorer.score(
            ScorerInput(
                unit=_unit(),
                output={"answer": "42"},
                reference={"answer": "42"},
            )
        )
        assert first == second
        assert first.passed is True

    def test_mismatch_fails(self) -> None:
        scorer = ExactMatchScorer(output_field="answer", reference_field="answer")
        verdict: ScorerVerdict = scorer.score(
            ScorerInput(
                unit=_unit(),
                output={"answer": "41"},
                reference={"answer": "42"},
            )
        )
        assert verdict.passed is False

    def test_missing_field_fails_closed(self) -> None:
        # 字段缺失按不通过处理（保守口径），不抛异常也不猜测。
        scorer = ExactMatchScorer(output_field="answer", reference_field="answer")
        verdict = scorer.score(
            ScorerInput(unit=_unit(), output={}, reference={"answer": "42"})
        )
        assert verdict.passed is False

    def test_verdict_is_immutable(self) -> None:
        verdict = ScorerVerdict(passed=True, score=1.0, details={})
        with pytest.raises(ValidationError):
            verdict.passed = False  # type: ignore[misc]


class TestHumanJudgeProtocol:
    def _protocol(self) -> HumanJudgeProtocol:
        return HumanJudgeProtocol(
            rubric={"accuracy": "答案与参考完全一致计 1 分"},
            blinding="double",
            order="fixed_seed_shuffle",
            calibration={"anchor-1": 1.0, "anchor-2": 0.0},
            agreement={"krippendorff_alpha": 0.82},
        )

    def test_missing_rubric_refused(self) -> None:
        with pytest.raises(HumanJudgeRefused):
            HumanJudgeProtocol(
                rubric={},
                blinding="double",
                order="fixed_seed_shuffle",
                calibration={"anchor-1": 1.0},
                agreement={"krippendorff_alpha": 0.8},
            )

    def test_missing_blinding_refused(self) -> None:
        with pytest.raises(HumanJudgeRefused):
            HumanJudgeProtocol(
                rubric={"accuracy": "1 分"},
                blinding="none",
                order="fixed_seed_shuffle",
                calibration={"anchor-1": 1.0},
                agreement={"krippendorff_alpha": 0.8},
            )

    def test_missing_calibration_refused(self) -> None:
        with pytest.raises(HumanJudgeRefused):
            HumanJudgeProtocol(
                rubric={"accuracy": "1 分"},
                blinding="double",
                order="fixed_seed_shuffle",
                calibration={},
                agreement={"krippendorff_alpha": 0.8},
            )

    def test_missing_agreement_refused(self) -> None:
        with pytest.raises(HumanJudgeRefused):
            HumanJudgeProtocol(
                rubric={"accuracy": "1 分"},
                blinding="double",
                order="fixed_seed_shuffle",
                calibration={"anchor-1": 1.0},
                agreement={},
            )

    def test_allowed_modes_exclude_offline_confirmation(self) -> None:
        # human/judge 仅用于 inference/utility（human/live/shadow），
        # 不得进入内部冻结（fixture/replay/offline）确认口径。
        allowed = self._protocol().allowed_modes
        assert allowed == frozenset({EvalMode.HUMAN, EvalMode.LIVE, EvalMode.SHADOW})

    def test_protocol_fields_are_preserved(self) -> None:
        protocol = self._protocol()
        assert protocol.rubric == {"accuracy": "答案与参考完全一致计 1 分"}
        assert protocol.order == "fixed_seed_shuffle"
