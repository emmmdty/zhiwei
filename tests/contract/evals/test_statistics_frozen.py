"""S9 冻结契约：统计方法与反自证分母（A 档，S9-T2）。

本文件是统计层的冻结契约：McNemar 精确双侧检验、独立性单位层的配对 bootstrap、Holm 校正、
完整失败分母（partial/error/refusal 全部入分母）。实现必须满足此处数值与故障注入断言；
GREEN 阶段不得修改、skip 或放宽本文件。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.statistics import (
    BootstrapDifferenceCI,
    DenominatorBreakdown,
    IndependenceUnitError,
    PreregistrationError,
    ProportionResult,
    UnitStructure,
    holm_correction,
    mcnemar_exact_two_sided,
    paired_bootstrap_difference_ci,
    success_rate_from_outcomes,
    terminal_denominator,
)


def _outcome(
    sample_id: str,
    unit_id: str,
    status: SampleStatus,
    *,
    passed: bool | None = None,
) -> SampleOutcome:
    result: dict[str, object] = {}
    if passed is not None:
        result["passed"] = passed
    return SampleOutcome(
        unit=RegisteredUnit(sample_id=sample_id, unit_id=unit_id),
        status=status,
        result=result,
    )


class TestMcNemarExact:
    def test_known_value_10_vs_2(self) -> None:
        # 2 * P(X <= 2 | B(12, 0.5)) = 2 * 79/4096；prereg 口径为 exact two-sided。
        assert mcnemar_exact_two_sided(10, 2) == pytest.approx(2 * 79 / 4096)

    def test_symmetric_in_discordant_pair(self) -> None:
        assert mcnemar_exact_two_sided(10, 2) == pytest.approx(
            mcnemar_exact_two_sided(2, 10)
        )

    def test_no_discordance_is_neutral(self) -> None:
        assert mcnemar_exact_two_sided(0, 0) == pytest.approx(1.0)

    def test_negative_counts_refused(self) -> None:
        with pytest.raises(ValueError):
            mcnemar_exact_two_sided(-1, 2)


class TestHolmCorrection:
    def test_known_adjustment_original_order(self) -> None:
        # 排序后 [0.01, 0.03, 0.04]：0.01*3=0.03；max(0.03*2, 0.03)=0.06；max(0.04*1, 0.06)=0.06。
        assert holm_correction([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])

    def test_result_is_monotone_in_sorted_p(self) -> None:
        adjusted = holm_correction([0.2, 0.004, 0.01, 0.04])
        ordered = sorted(adjusted)
        assert ordered == sorted(ordered)
        assert all(later >= earlier for earlier, later in pairwise(ordered))

    def test_empty_family(self) -> None:
        assert holm_correction([]) == ()

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            holm_correction([1.5])


class TestUnitStructure:
    def test_duplicate_member_across_groups_refused(self) -> None:
        # 「错误的独立性单位」故障注入：同一 unit 被两个独立性单位认领时必须拒绝，
        # 否则配对设计的自由度会被虚增。
        with pytest.raises(IndependenceUnitError):
            UnitStructure.from_groups(
                {
                    "chain-1": (("q-1", "u-1"), ("q-1", "u-2")),
                    "chain-2": (("q-1", "u-2"),),
                }
            )

    def test_empty_group_refused(self) -> None:
        with pytest.raises(IndependenceUnitError):
            UnitStructure.from_groups({"chain-1": ()})

    def test_duplicate_unit_within_group_refused(self) -> None:
        with pytest.raises(IndependenceUnitError):
            UnitStructure.from_groups(
                {"chain-1": (("q-1", "u-1"), ("q-1", "u-1"))}
            )


class TestPairedBootstrap:
    def _structure(self) -> UnitStructure:
        return UnitStructure.from_groups(
            {
                "g-1": (("q-1", "u-1"),),
                "g-2": (("q-2", "u-1"),),
                "g-3": (("q-3", "u-1"),),
                "g-4": (("q-4", "u-1"),),
            }
        )

    def test_identical_systems_yield_zero_width_ci(self) -> None:
        structure = self._structure()
        system_a = {("q-1", "u-1"): True, ("q-2", "u-1"): True, ("q-3", "u-1"): False, ("q-4", "u-1"): False}
        ci = paired_bootstrap_difference_ci(
            system_a=system_a,
            system_b=dict(system_a),
            structure=structure,
            resamples=200,
            seed=20260812,
        )
        assert ci.ci_low == pytest.approx(0.0)
        assert ci.ci_high == pytest.approx(0.0)
        assert ci.estimate == pytest.approx(0.0)

    def test_estimate_equals_full_sample_difference(self) -> None:
        structure = self._structure()
        system_a = {("q-1", "u-1"): True, ("q-2", "u-1"): True, ("q-3", "u-1"): True, ("q-4", "u-1"): False}
        system_b = {("q-1", "u-1"): True, ("q-2", "u-1"): False, ("q-3", "u-1"): True, ("q-4", "u-1"): False}
        ci = paired_bootstrap_difference_ci(
            system_a=system_a,
            system_b=system_b,
            structure=structure,
            resamples=200,
            seed=1,
        )
        # 全样本估计：a 成功率 3/4 - b 成功率 2/4 = 0.25。配对重采样不改变点估计。
        assert ci.estimate == pytest.approx(0.25)

    def test_same_seed_is_deterministic(self) -> None:
        structure = self._structure()
        system_a = {("q-1", "u-1"): True, ("q-2", "u-1"): False, ("q-3", "u-1"): True, ("q-4", "u-1"): True}
        system_b = {("q-1", "u-1"): False, ("q-2", "u-1"): True, ("q-3", "u-1"): True, ("q-4", "u-1"): False}
        first = paired_bootstrap_difference_ci(
            system_a=system_a,
            system_b=system_b,
            structure=structure,
            resamples=500,
            seed=20260812,
        )
        second = paired_bootstrap_difference_ci(
            system_a=system_a,
            system_b=system_b,
            structure=structure,
            resamples=500,
            seed=20260812,
        )
        assert first == second

    def test_missing_unit_against_structure_refused(self) -> None:
        structure = self._structure()
        system_a = {("q-1", "u-1"): True, ("q-2", "u-1"): True, ("q-3", "u-1"): True}
        with pytest.raises(IndependenceUnitError):
            paired_bootstrap_difference_ci(
                system_a=system_a,
                system_b=system_a,
                structure=structure,
                resamples=10,
                seed=1,
            )

    def test_extra_unit_not_in_structure_refused(self) -> None:
        structure = self._structure()
        system_a = {
            ("q-1", "u-1"): True,
            ("q-2", "u-1"): True,
            ("q-3", "u-1"): True,
            ("q-4", "u-1"): True,
            ("q-9", "u-1"): True,
        }
        with pytest.raises(IndependenceUnitError):
            paired_bootstrap_difference_ci(
                system_a=system_a,
                system_b=dict(system_a),
                structure=structure,
                resamples=10,
                seed=1,
            )

    def test_ci_brackets_estimate(self) -> None:
        structure = self._structure()
        system_a = {("q-1", "u-1"): True, ("q-2", "u-1"): True, ("q-3", "u-1"): True, ("q-4", "u-1"): False}
        system_b = {("q-1", "u-1"): False, ("q-2", "u-1"): False, ("q-3", "u-1"): False, ("q-4", "u-1"): False}
        ci: BootstrapDifferenceCI = paired_bootstrap_difference_ci(
            system_a=system_a,
            system_b=system_b,
            structure=structure,
            resamples=400,
            seed=7,
        )
        assert ci.ci_low <= ci.estimate <= ci.ci_high


class TestDenominator:
    def test_terminal_denominator_counts_all_terminal(self) -> None:
        outcomes = [
            _outcome("s-1", "u-1", SampleStatus.COMPLETED, passed=True),
            _outcome("s-2", "u-1", SampleStatus.FAILED),
            _outcome("s-3", "u-1", SampleStatus.REFUSED),
            _outcome("s-4", "u-1", SampleStatus.ERROR),
        ]
        breakdown: DenominatorBreakdown = terminal_denominator(outcomes)
        assert breakdown.n_total == 4
        assert breakdown.n_completed == 1
        assert breakdown.n_failed == 1
        assert breakdown.n_refused == 1
        assert breakdown.n_error == 1

    def test_non_terminal_outcome_refused(self) -> None:
        with pytest.raises(PreregistrationError):
            terminal_denominator([_outcome("s-1", "u-1", SampleStatus.REGISTERED)])

    def test_refusal_and_error_stay_in_denominator(self) -> None:
        outcomes = [
            _outcome("s-1", "u-1", SampleStatus.COMPLETED, passed=True),
            _outcome("s-2", "u-1", SampleStatus.REFUSED),
            _outcome("s-3", "u-1", SampleStatus.ERROR),
        ]
        proportion: ProportionResult = success_rate_from_outcomes(outcomes)
        # refused/error 不剔题：分母为 3，成功率 1/3。
        assert proportion.n == 3
        assert proportion.successes == 1
        assert proportion.estimate == pytest.approx(1 / 3)
        assert 0.0 < proportion.ci_low < proportion.estimate < proportion.ci_high < 1.0

    def test_empty_denominator_refused(self) -> None:
        # 不允许生成空成功报告：分母为空必须显式失败。
        with pytest.raises(PreregistrationError):
            success_rate_from_outcomes([])
