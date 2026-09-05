"""S9-T2 RED: 统计层实现级契约（冻结契约之外的实现行为锁定）。

冻结契约（tests/contract/evals/test_statistics_frozen.py）定义数值与故障注入口径；
本文件锁定实现级行为：Wilson 区间端点、缺失字段口径、参数拒绝、结构保真。
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

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
    result: dict[str, object] | None = None,
) -> SampleOutcome:
    return SampleOutcome(
        unit=RegisteredUnit(sample_id=sample_id, unit_id=unit_id),
        status=status,
        result=result or {},
    )


class TestMcNemarExactValues:
    def test_matches_manual_binomial_sum(self) -> None:
        # (5,1)：n=6，P(X<=1)=（C(6,0)+C(6,1)）/64 = 7/64，双侧 *2。
        assert mcnemar_exact_two_sided(5, 1) == pytest.approx(2 * 7 / 64)

    def test_large_imbalance_caps_at_one(self) -> None:
        # n=2 全 discordant：2 * P(X<=0) = 2 * 0.25 = 0.5；不到 1，验证 cap 不误伤。
        assert mcnemar_exact_two_sided(2, 0) == pytest.approx(0.5)

    def test_single_discordant_pair(self) -> None:
        # n=1：2 * 0.5 = 1.0，恰好触 cap。
        assert mcnemar_exact_two_sided(1, 0) == pytest.approx(1.0)


class TestHolmImplementation:
    def test_single_family_is_identity(self) -> None:
        assert holm_correction([0.7]) == pytest.approx((0.7,))

    def test_adjustment_caps_at_one(self) -> None:
        # 2 * 0.9 = 1.8 → cap 到 1.0，且后续单调不降。
        assert holm_correction([0.9, 0.9]) == pytest.approx((1.0, 1.0))

    def test_negative_p_refused(self) -> None:
        with pytest.raises(ValueError):
            holm_correction([-0.1])


class TestUnitStructureFidelity:
    def test_happy_path_preserves_groups_and_units(self) -> None:
        structure = UnitStructure.from_groups(
            {
                "chain-b": (("q-2", "u-1"), ("q-2", "u-2")),
                "chain-a": (("q-1", "u-1"),),
            }
        )
        # 组序与单位序必须确定化：不依赖调用方传入顺序。
        assert structure.group_ids == ("chain-a", "chain-b")
        assert structure.units == (("q-1", "u-1"), ("q-2", "u-1"), ("q-2", "u-2"))
        assert structure.group_of[("q-2", "u-2")] == "chain-b"

    def test_empty_mapping_refused(self) -> None:
        with pytest.raises(IndependenceUnitError):
            UnitStructure.from_groups({})

    def test_unit_key_type_refused(self) -> None:
        with pytest.raises(IndependenceUnitError):
            UnitStructure.from_groups({"chain-1": (("q-1",),)})  # type: ignore[list-item]


class TestPairedBootstrapGuards:
    def _structure(self) -> UnitStructure:
        return UnitStructure.from_groups(
            {
                "g-1": (("q-1", "u-1"), ("q-1", "u-2")),
                "g-2": (("q-2", "u-1"),),
            }
        )

    def _systems(self) -> tuple[dict[tuple[str, str], bool], dict[tuple[str, str], bool]]:
        system_a = {("q-1", "u-1"): True, ("q-1", "u-2"): False, ("q-2", "u-1"): True}
        system_b = {("q-1", "u-1"): False, ("q-1", "u-2"): False, ("q-2", "u-1"): True}
        return system_a, system_b

    def test_missing_unit_in_system_b_refused(self) -> None:
        structure = self._structure()
        system_a, system_b = self._systems()
        del system_b[("q-2", "u-1")]
        with pytest.raises(IndependenceUnitError):
            paired_bootstrap_difference_ci(
                system_a=system_a,
                system_b=system_b,
                structure=structure,
                resamples=10,
                seed=1,
            )

    def test_non_bool_value_refused(self) -> None:
        structure = self._structure()
        system_a, system_b = self._systems()
        system_a[("q-1", "u-1")] = "yes"  # type: ignore[assignment]
        with pytest.raises(ValueError):
            paired_bootstrap_difference_ci(
                system_a=system_a,
                system_b=system_b,
                structure=structure,
                resamples=10,
                seed=1,
            )

    def test_invalid_resamples_refused(self) -> None:
        structure = self._structure()
        system_a, system_b = self._systems()
        with pytest.raises(ValueError):
            paired_bootstrap_difference_ci(
                system_a=system_a,
                system_b=system_b,
                structure=structure,
                resamples=0,
                seed=1,
            )

    def test_invalid_alpha_refused(self) -> None:
        structure = self._structure()
        system_a, system_b = self._systems()
        with pytest.raises(ValueError):
            paired_bootstrap_difference_ci(
                system_a=system_a,
                system_b=system_b,
                structure=structure,
                resamples=10,
                seed=1,
                alpha=1.0,
            )

    def test_wider_alpha_narrows_the_interval(self) -> None:
        structure = self._structure()
        system_a, system_b = self._systems()
        tight: BootstrapDifferenceCI = paired_bootstrap_difference_ci(
            system_a=system_a,
            system_b=system_b,
            structure=structure,
            resamples=400,
            seed=3,
            alpha=0.5,
        )
        loose: BootstrapDifferenceCI = paired_bootstrap_difference_ci(
            system_a=system_a,
            system_b=system_b,
            structure=structure,
            resamples=400,
            seed=3,
            alpha=0.05,
        )
        assert tight.ci_high - tight.ci_low <= loose.ci_high - loose.ci_low

    def test_group_level_resampling_moves_members_together(self) -> None:
        # g-1 的两个单位同组：a 在组内同涨同跌，重采样差值只能落在组级可达集合。
        structure = UnitStructure.from_groups(
            {
                "g-1": (("q-1", "u-1"), ("q-1", "u-2")),
                "g-2": (("q-2", "u-1"), ("q-2", "u-2")),
            }
        )
        system_a = {
            ("q-1", "u-1"): True,
            ("q-1", "u-2"): True,
            ("q-2", "u-1"): False,
            ("q-2", "u-2"): False,
        }
        system_b = dict.fromkeys(system_a, False)
        ci = paired_bootstrap_difference_ci(
            system_a=system_a,
            system_b=system_b,
            structure=structure,
            resamples=500,
            seed=11,
        )
        # 每次重采样的差值只能是 {1.0, 0.5, 0.0, -0.5, -1.0} 的凸组合端点：估计 0.5。
        assert ci.estimate == pytest.approx(0.5)
        assert -1.0 <= ci.ci_low <= ci.ci_high <= 1.0


class TestRateImplementation:
    def test_completed_without_success_field_is_non_success(self) -> None:
        outcomes = [
            _outcome("s-1", "u-1", SampleStatus.COMPLETED),
            _outcome("s-2", "u-1", SampleStatus.COMPLETED, result={"passed": True}),
        ]
        proportion: ProportionResult = success_rate_from_outcomes(outcomes)
        # 缺字段按不通过（保守口径），但不剔题：分母 2、成功 1。
        assert (proportion.n, proportion.successes) == (2, 1)

    def test_non_terminal_refused_in_rate(self) -> None:
        with pytest.raises(PreregistrationError):
            success_rate_from_outcomes(
                [_outcome("s-1", "u-1", SampleStatus.RUNNING, result={"passed": True})]
            )

    def test_wilson_single_failure_has_zero_lower_bound(self) -> None:
        proportion = success_rate_from_outcomes(
            [_outcome("s-1", "u-1", SampleStatus.FAILED)]
        )
        assert proportion.estimate == pytest.approx(0.0)
        assert proportion.ci_low == pytest.approx(0.0)
        assert 0.0 < proportion.ci_high < 1.0

    def test_wilson_single_success_has_unity_upper_bound(self) -> None:
        proportion = success_rate_from_outcomes(
            [_outcome("s-1", "u-1", SampleStatus.COMPLETED, result={"passed": True})]
        )
        assert proportion.estimate == pytest.approx(1.0)
        assert 0.0 < proportion.ci_low < 1.0
        assert proportion.ci_high == pytest.approx(1.0)

    def test_wilson_interval_shrinks_with_sample_size(self) -> None:
        small = success_rate_from_outcomes(
            [
                _outcome("s-1", "u-1", SampleStatus.COMPLETED, result={"passed": True}),
                _outcome("s-2", "u-1", SampleStatus.COMPLETED),
            ]
        )
        large = success_rate_from_outcomes(
            [
                _outcome(f"s-{index}", "u-1", SampleStatus.COMPLETED, result={"passed": index % 2 == 0})
                for index in range(1, 201)
            ]
        )
        assert large.ci_high - large.ci_low < small.ci_high - small.ci_low
        assert math.isfinite(large.ci_low) and math.isfinite(large.ci_high)


class TestBreakdownImmutability:
    def test_breakdown_is_frozen(self) -> None:
        breakdown = DenominatorBreakdown(
            n_total=1, n_completed=1, n_failed=0, n_refused=0, n_error=0
        )
        with pytest.raises(FrozenInstanceError):
            breakdown.n_total = 2  # type: ignore[misc]

    def test_terminal_denominator_counts_completed_only_once(self) -> None:
        outcomes = [_outcome("s-1", "u-1", SampleStatus.COMPLETED, result={"passed": True})]
        breakdown: DenominatorBreakdown = terminal_denominator(outcomes)
        assert breakdown.n_total == breakdown.n_completed == 1
        assert breakdown.n_failed == breakdown.n_refused == breakdown.n_error == 0
