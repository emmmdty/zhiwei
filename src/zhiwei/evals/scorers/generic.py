"""确定性 scorer：ExactMatch 是最小可用实现，缺字段一律不通过。

缺失字段 fail closed（不抛异常、不猜测、判不通过）：参考答案或被评输出缺失时，
任何「通过」结论都不可信，保守口径是唯一安全选择。
"""

from __future__ import annotations

from zhiwei.evals.scorers.base import ScorerInput, ScorerVerdict


class ExactMatchScorer:
    """output[output_field] == reference[reference_field] 的纯函数判定。"""

    def __init__(self, *, output_field: str, reference_field: str) -> None:
        self._output_field = output_field
        self._reference_field = reference_field

    @property
    def output_field(self) -> str:
        return self._output_field

    @property
    def reference_field(self) -> str:
        return self._reference_field

    def score(self, payload: ScorerInput) -> ScorerVerdict:
        output_value = payload.output.get(self._output_field)
        if output_value is None:
            return ScorerVerdict(
                passed=False,
                score=0.0,
                details={"reason": "missing_output_field", "field": self._output_field},
            )
        reference_value = payload.reference.get(self._reference_field)
        if reference_value is None:
            return ScorerVerdict(
                passed=False,
                score=0.0,
                details={"reason": "missing_reference_field", "field": self._reference_field},
            )
        matched = output_value == reference_value
        return ScorerVerdict(
            passed=matched,
            score=1.0 if matched else 0.0,
            details={"matched": matched},
        )
