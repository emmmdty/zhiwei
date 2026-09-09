"""S9 统计层： prereg 口径的检验、区间与完整失败分母。

设计约束来自 specs/s9 §4 与冻结契约（tests/contract/evals/test_statistics_frozen.py）：
- 配对实验的自由度由独立性单位层（UnitStructure）决定，bootstrap 在 group 级重采样，
  组内单位同进同出——单位错误（重叠/重复/空组）一律拒绝，不取「常见默认」。
- refused/error/failed 全部留在分母：剔题即自证，宁可让成功率难看也不允许空成功报告。
- 所有数值方法确定性：给定 seed 的 bootstrap 可逐字节复现。
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats

from zhiwei.evals.domain import SampleOutcome, SampleStatus, is_terminal

_DEFAULT_ALPHA = 0.05


class PreregistrationError(ValueError):
    """prereg 口径被违反（空分母、非终态混入等）时拒绝，不产出误导性统计。"""


class IndependenceUnitError(ValueError):
    """独立性单位结构非法（重叠/重复/空组）或输入与结构不一致时拒绝。"""


@dataclass(frozen=True)
class ProportionResult:
    """比例估计及其 Wilson 区间；n 是包含全部终态的完整分母。"""

    n: int
    successes: int
    estimate: float
    ci_low: float
    ci_high: float


@dataclass(frozen=True)
class DenominatorBreakdown:
    """完整失败分母的逐状态计数；partial/error/refusal 都计入 n_total。"""

    n_total: int
    n_completed: int
    n_failed: int
    n_refused: int
    n_error: int


@dataclass(frozen=True)
class BootstrapDifferenceCI:
    """配对差值的点估计与 percentile 区间。"""

    estimate: float
    ci_low: float
    ci_high: float


def mcnemar_exact_two_sided(first: int, second: int) -> float:
    """McNemar 精确双侧 p 值：2 * P(X <= min(b, c) | B(b+c, 0.5))，cap 到 1。

    精确二项而非正态近似：配对 discordant 计数常为小样本，近似会失真；
    b=c=0 时无分歧证据，返回中性 1.0。
    """
    for count in (first, second):
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("discordant counts must be integers")
        if count < 0:
            raise ValueError("discordant counts must be non-negative")
    total = first + second
    if total == 0:
        return 1.0
    tail = stats.binom.cdf(min(first, second), total, 0.5)
    return float(min(1.0, 2.0 * tail))


def holm_correction(p_values: Sequence[float]) -> tuple[float, ...]:
    """Holm step-down 家族校正；结果按输入原始顺序回填，单调不降、cap 到 1。"""
    family_size = len(p_values)
    if family_size == 0:
        return ()
    adjusted: list[float] = []
    running_max = 0.0
    order = sorted(range(family_size), key=lambda index: p_values[index])
    for rank, index in enumerate(order):
        value = p_values[index]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("p-values must be numbers in [0, 1]")
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("p-values must be numbers in [0, 1]")
        running_max = max(running_max, (family_size - rank) * float(value))
        adjusted.append(min(1.0, running_max))
    result = [0.0] * family_size
    for rank, index in enumerate(order):
        result[index] = adjusted[rank]
    return tuple(result)


@dataclass(frozen=True, slots=True)
class UnitStructure:
    """独立性单位层：group -> members 的封闭映射，决定配对重采样的自由度。

    「错误的独立性单位」（同一 unit 被多个 group 认领、重复成员、空组）会虚增
    自由度并让 bootstrap 低估方差——构造期全部拒绝，不留静默口径。
    """

    groups: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    group_of: Mapping[tuple[str, str], str]

    @classmethod
    def from_groups(
        cls, groups: Mapping[str, Sequence[tuple[str, str]]]
    ) -> UnitStructure:
        if not groups:
            raise IndependenceUnitError("unit structure requires at least one group")
        normalized: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        membership: dict[tuple[str, str], str] = {}
        for group_id, members in groups.items():
            if not isinstance(group_id, str) or not group_id:
                raise IndependenceUnitError("group id must be a non-empty string")
            if not members:
                raise IndependenceUnitError(f"group {group_id!r} is empty")
            normalized_members: list[tuple[str, str]] = []
            for member in members:
                try:
                    sample_id, unit_id = member
                except (TypeError, ValueError) as exc:
                    raise IndependenceUnitError(
                        f"group {group_id!r} member {member!r} is not a (sample_id, unit_id) pair"
                    ) from exc
                key = (sample_id, unit_id)
                if key in membership:
                    raise IndependenceUnitError(
                        f"unit {key!r} is claimed by groups "
                        f"{membership[key]!r} and {group_id!r}"
                    )
                if key in normalized_members:
                    raise IndependenceUnitError(
                        f"unit {key!r} is duplicated within group {group_id!r}"
                    )
                membership[key] = group_id
                normalized_members.append(key)
            normalized.append((group_id, tuple(normalized_members)))
        normalized.sort(key=lambda item: item[0])
        return cls(groups=tuple(normalized), group_of=membership)

    @property
    def group_ids(self) -> tuple[str, ...]:
        return tuple(group_id for group_id, _ in self.groups)

    @property
    def units(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.group_of))

    def __len__(self) -> int:
        return len(self.group_of)


def paired_bootstrap_difference_ci(
    *,
    system_a: Mapping[tuple[str, str], bool],
    system_b: Mapping[tuple[str, str], bool],
    structure: UnitStructure,
    resamples: int,
    seed: int,
    alpha: float = _DEFAULT_ALPHA,
) -> BootstrapDifferenceCI:
    """配对系统差值的 bootstrap 区间；重采样在独立性 group 级进行。

    组内单位同进同出：把组当重采样单位才不会虚增自由度。给定 seed 与组序
    （结构内确定化排序），重采样序列完全确定；点估计始终是全样本的
    mean(a) - mean(b)，不受重采样影响。
    """
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")

    expected_units = set(structure.units)
    for label, system in (("system_a", system_a), ("system_b", system_b)):
        keys = set(system)
        missing = expected_units - keys
        if missing:
            raise IndependenceUnitError(
                f"{label} is missing units from the structure: {sorted(missing)}"
            )
        extra = keys - expected_units
        if extra:
            raise IndependenceUnitError(
                f"{label} has units outside the structure: {sorted(extra)}"
            )
        for key, value in system.items():
            if not isinstance(value, bool):
                raise ValueError(f"{label}[{key!r}] must be a bool verdict")

    units = structure.units
    successes_a = [bool(system_a[key]) for key in units]
    successes_b = [bool(system_b[key]) for key in units]
    estimate = sum(successes_a) / len(units) - sum(successes_b) / len(units)

    # 组级重采样预算：每个 group 预先折算 (成员数, a 成功数, b 成功数)，
    # 重采样时整组搬移，避免逐单位抽取破坏独立性。
    group_budget: list[tuple[int, int, int]] = []
    offset = {key: index for index, key in enumerate(units)}
    for _, members in structure.groups:
        indices = [offset[key] for key in members]
        group_budget.append(
            (
                len(indices),
                sum(successes_a[index] for index in indices),
                sum(successes_b[index] for index in indices),
            )
        )

    rng = random.Random(seed)
    n_groups = len(group_budget)
    diffs = np.empty(resamples, dtype=np.float64)
    for iteration in range(resamples):
        picked = [rng.randrange(n_groups) for _ in range(n_groups)]
        count = sum(group_budget[index][0] for index in picked)
        sum_a = sum(group_budget[index][1] for index in picked)
        sum_b = sum(group_budget[index][2] for index in picked)
        diffs[iteration] = sum_a / count - sum_b / count

    low, high = np.quantile(diffs, [alpha / 2.0, 1.0 - alpha / 2.0])
    return BootstrapDifferenceCI(
        estimate=estimate, ci_low=float(low), ci_high=float(high)
    )


def terminal_denominator(outcomes: Sequence[SampleOutcome]) -> DenominatorBreakdown:
    """完整失败分母：任何非终态混入都拒绝——分母口径必须封闭。"""
    counts = {
        SampleStatus.COMPLETED: 0,
        SampleStatus.FAILED: 0,
        SampleStatus.REFUSED: 0,
        SampleStatus.ERROR: 0,
    }
    for outcome in outcomes:
        if not is_terminal(outcome.status):
            raise PreregistrationError(
                f"denominator requires terminal outcomes, got {outcome.status.value!r} "
                f"for unit {outcome.unit.sample_id!r}/{outcome.unit.unit_id!r}"
            )
        counts[outcome.status] += 1
    return DenominatorBreakdown(
        n_total=len(outcomes),
        n_completed=counts[SampleStatus.COMPLETED],
        n_failed=counts[SampleStatus.FAILED],
        n_refused=counts[SampleStatus.REFUSED],
        n_error=counts[SampleStatus.ERROR],
    )


def _wilson_interval(
    successes: int, n: int, alpha: float
) -> tuple[float, float]:
    """Wilson score 区间：小样本/边界比例下比正态近似更保守且不越界。"""
    z = float(stats.norm.ppf(1.0 - alpha / 2.0))
    proportion = successes / n
    denominator = 1.0 + z * z / n
    center = (proportion + z * z / (2.0 * n)) / denominator
    spread = (
        z
        / denominator
        * math.sqrt(
            proportion * (1.0 - proportion) / n + z * z / (4.0 * n * n)
        )
    )
    low = max(0.0, center - spread)
    high = min(1.0, center + spread)
    return low, high


def success_rate_from_outcomes(
    outcomes: Sequence[SampleOutcome],
    success_field: str = "passed",
    *,
    alpha: float = _DEFAULT_ALPHA,
) -> ProportionResult:
    """成功率 = COMPLETED 且 result[success_field] 为真的单位 / 全部终态单位。

    refused/error/failed 留在分母内计为非成功——这是反自证口径的核心：
    被策略拒绝或 provider 出错的题不算「通过」也不允许被剔除。分母为空或
    混入非终态一律 PreregistrationError，不生成空成功报告。
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    breakdown = terminal_denominator(outcomes)
    if breakdown.n_total == 0:
        raise PreregistrationError("denominator is empty; refusing to fabricate a rate")
    successes = sum(
        1
        for outcome in outcomes
        if outcome.status is SampleStatus.COMPLETED
        and bool(outcome.result.get(success_field))
    )
    n = breakdown.n_total
    low, high = _wilson_interval(successes, n, alpha)
    return ProportionResult(
        n=n,
        successes=successes,
        estimate=successes / n,
        ci_low=low,
        ci_high=high,
    )
