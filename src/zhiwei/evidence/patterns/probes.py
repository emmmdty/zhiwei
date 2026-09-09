"""NegativeProbe 的确定性求值与生成（ADR-004，spec s8 §4.1）。

「模型只提出、不判定」：求值一律由本模块的确定性组件完成——比较器对 snapshot 指标
真值执行，不得由 LLM 或产生假设的 detector 上下文判定（职责分离）。

probe 语义：「若此假设为假，应观察到 X」。X = {metric, entity_scope, window,
comparator, threshold} 的机器可求值断言；X 被观察到 → 假设被推翻（passed=False），
未被观察到 → 该 probe 通过（假设存活）。每个结果独立成 EvidenceRef，不可合并。

窗口坐标约定（NegativeProbe 只有 window_hours 一个窗口字段，求值器与生成器共享
同一约定）：window_hours 从观察日历末端向前计数（trailing window），
months = max(1, window_hours // 720)。生成器只提出 trailing 窗口内可求值的断言。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from operator import eq, ge, gt, le, lt, ne
from uuid import NAMESPACE_URL, uuid5

from zhiwei.contracts.time import ensure_utc
from zhiwei.discover.hypotheses import EvidenceTag, HypothesisKind
from zhiwei.discover.signals import FalsificationResult, NegativeProbe
from zhiwei.evidence.patterns.numeric import MONTH_CALENDAR, PatternFinding

_PROBE_NAMESPACE = uuid5(NAMESPACE_URL, "zhiwei:discover:negative-probe")

_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "lt": lt,
    "gt": gt,
    "eq": eq,
    "neq": ne,
    "gte": ge,
    "lte": le,
}

_HOURS_PER_MONTH = 720
_TERMINAL_MONTHS = 3


class ProbeEvaluationError(RuntimeError):
    """probe 求值所需前提不成立：数据缺失、未知比较器——fail closed，不默认通过。"""


class ProbeMetricTable:
    """(entity_scope, metric) → 月对齐序列的只读指标表。

    日历固定为 Numeric Detector Pack 的版本化观察坐标系（MONTH_CALENDAR）；
    覆盖范围之外的取值返回 None，由 evaluate_probe 显式拒绝——不默认 0 或均值。
    """

    def __init__(self) -> None:
        self._series: dict[tuple[str, str], list[float | None]] = {}

    def put(self, entity_scope: str, metric: str, values: Sequence[float | None]) -> None:
        if len(values) != len(MONTH_CALENDAR):
            raise ValueError(
                f"series for {entity_scope}/{metric} must cover the {len(MONTH_CALENDAR)}-month calendar"
            )
        self._series[(entity_scope, metric)] = list(values)

    def bounds(self, entity_scope: str, metric: str) -> tuple[str, str] | None:
        """该指标有序列覆盖的日历边界；无序列返回 None。"""
        if (entity_scope, metric) not in self._series:
            return None
        return MONTH_CALENDAR[0], MONTH_CALENDAR[-1]

    def value(self, entity_scope: str, metric: str, window_start: str, window_end: str) -> float | None:
        """窗口均值（缺失值跳过）；窗口越界、序列缺失或窗口内无观测返回 None（fail closed）。"""
        series = self._series.get((entity_scope, metric))
        if series is None:
            return None
        index = {month: i for i, month in enumerate(MONTH_CALENDAR)}
        if window_start not in index or window_end not in index:
            return None
        lo, hi = index[window_start], index[window_end]
        if lo > hi:
            return None
        observed = [v for v in series[lo : hi + 1] if v is not None]
        if not observed:
            return None
        return sum(observed) / len(observed)  # type: ignore[arg-type]


def evaluate_probe(
    probe: NegativeProbe, table: ProbeMetricTable, *, evaluated_at: datetime
) -> FalsificationResult:
    """确定性求值：comparator(actual, threshold) 成立 → 反证被观察到 → passed=False。"""
    comparator = _COMPARATORS.get(probe.comparator)
    if comparator is None:
        raise ProbeEvaluationError(f"unknown probe comparator: {probe.comparator}")
    total = len(MONTH_CALENDAR)
    covered = max(1, probe.window_hours // _HOURS_PER_MONTH)
    lo = max(0, total - covered)
    actual = table.value(probe.entity_scope, probe.metric, MONTH_CALENDAR[lo], MONTH_CALENDAR[-1])
    if actual is None:
        raise ProbeEvaluationError(
            f"probe metric unavailable: {probe.entity_scope}/{probe.metric} (fail closed)"
        )
    observed = comparator(actual, probe.threshold)
    return FalsificationResult(
        probe=probe,
        passed=not observed,
        actual_value=actual,
        evaluated_at=ensure_utc(evaluated_at),
    )


def generate_probes_for_finding(
    finding: PatternFinding, table: ProbeMetricTable
) -> tuple[NegativeProbe, ...]:
    """独立 probe 生成节点：只读 finding 的 typed 字段与指标表，不复用 detector 拟合上下文。

    断言设计（方向镜像，全部为 trailing 窗口可求值）：
    - horizon：全日历均值应偏离基线至少 |change|/4——若假设为假（变动是噪声），
      全期均值仍在基线一侧；
    - terminal：最后 3 期均值应仍停在效应侧——若假设为假（效应在观察期末已消退），
      尾部水平应回到基线一侧；
    - persistence：仅当模式窗口之后仍有观察期时生成——若假设为假（效应不持续），
      窗口后的尾部均值应回到基线一侧。
    阈值由指标表（表内均值）派生，确定性、可复算。
    """
    scope = f"{finding.entity_dim}:{finding.entity_value}"
    bounds = table.bounds(scope, finding.metric)
    if bounds is None:
        return ()
    months = list(MONTH_CALENDAR)
    start_i, end_i = months.index(finding.window_start), months.index(finding.window_end)
    pre_end = months[max(0, start_i - 1)]
    pre_mean = table.value(scope, finding.metric, bounds[0], pre_end)
    in_mean = table.value(scope, finding.metric, finding.window_start, finding.window_end)
    if pre_mean is None or in_mean is None:
        return ()
    change = abs(finding.change)
    if change <= 0:
        return ()
    probes: list[NegativeProbe] = []

    # horizon probe：全日历窗口（window_hours 覆盖整个日历）。
    if finding.direction == "decline":
        horizon = _probe(
            finding, scope, "gte", pre_mean - change / 4, 0, len(months) - 1, "horizon"
        )
    else:
        horizon = _probe(
            finding, scope, "lte", pre_mean + change / 4, 0, len(months) - 1, "horizon"
        )
    probes.append(horizon)

    # terminal probe：观察期末（最后 3 期）仍应停在效应侧。
    tail_lo = len(months) - _TERMINAL_MONTHS
    if finding.direction == "decline":
        terminal = _probe(
            finding, scope, "gte", pre_mean - change / 2, tail_lo, len(months) - 1, "terminal"
        )
    else:
        terminal = _probe(
            finding, scope, "lte", pre_mean + change / 2, tail_lo, len(months) - 1, "terminal"
        )
    probes.append(terminal)

    # persistence probe：仅当窗口后仍有完整观察期。
    tail_start = end_i + 1
    if tail_start <= len(months) - 1:
        if finding.direction == "decline":
            persistence = _probe(
                finding, scope, "gte", pre_mean - change / 2, tail_start, len(months) - 1, "persistence"
            )
        else:
            persistence = _probe(
                finding, scope, "lte", pre_mean + change / 2, tail_start, len(months) - 1, "persistence"
            )
        probes.append(persistence)
    return tuple(probes)


def _probe(
    finding: PatternFinding,
    scope: str,
    comparator: str,
    threshold: float,
    lo: int,
    hi: int,
    role: str,
) -> NegativeProbe:
    probe_id = uuid5(
        _PROBE_NAMESPACE,
        f"{finding.kind}:{scope}:{finding.metric}:{finding.window_start}:{finding.window_end}:{role}",
    )
    return NegativeProbe(
        probe_id=probe_id,
        metric=finding.metric,
        entity_scope=scope,
        window_hours=max(1, hi - lo + 1) * _HOURS_PER_MONTH,
        comparator=comparator,
        threshold=threshold,
        description=(
            f"若「{finding.entity_value} {finding.metric} {finding.direction}」假设为假，"
            f"应观察到 {role} 窗口均值 {comparator} {threshold:.6g}"
        ),
    )


def probe_evidence_tags(
    results: tuple[FalsificationResult, ...] | list[FalsificationResult], *, created_at: datetime
) -> tuple[EvidenceTag, ...]:
    """每个 probe 结果独立一个 EvidenceTag：一 probe 一 ref，不合并、不省略（spec §4.1）。"""
    tags: list[EvidenceTag] = []
    for result in results:
        disposition = "refuted" if not result.passed else "held"
        tags.append(
            EvidenceTag(
                tag_id=uuid5(_PROBE_NAMESPACE, f"evidence:{result.probe.probe_id}"),
                kind=HypothesisKind.CONTRADICTING
                if not result.passed
                else HypothesisKind.SUPPORTING,
                description=(
                    f"negative_probe:{result.probe.probe_id} {disposition} "
                    f"(actual={result.actual_value!r} vs {result.probe.comparator} "
                    f"{result.probe.threshold!r})"
                ),
                source_ref=f"negative_probe:{result.probe.probe_id}",
                created_at=ensure_utc(created_at),
            )
        )
    return tuple(tags)
