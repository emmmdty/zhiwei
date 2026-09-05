"""Numeric Risk Detector Pack 的确定性模式复算（docs/RISK_EVAL.md §5，spec s8 §5）。

生产 detector 路径：snapshot → 序列提取（first-wins 去重）→ 逐 kind 结构化扫描 →
PatternFinding。全部计算确定性、无墙钟、无随机数——同输入逐字节同输出。

SNR 口径（RISK_EVAL §5，方向不成立时为 0）：
- P1 trend:                OLS 首尾变化绝对值 / 残差 robust sigma
- P2 concentration:        share OLS 首尾增量 / 残差 robust sigma
- P3 seasonal:             相对历史同月中位数偏差 / 历史季节残差 robust sigma
- P4 baseline_deviation:   post/pre 中位差 / pre robust sigma
- P5 ratio:                revenue 与 cashflow 背离（cash_to_revenue 斜率 / 残差 robust sigma，
                           且 revenue 侧不塌缩）
- P6 concentration_signal: min(share 增量侧, -on_time 下滑侧)

难度由数据复算：hard [0.8,1.5)、medium [1.5,3)、easy [3,+inf)；< 0.8 不报（distractor 档）。
"""

from __future__ import annotations

from enum import StrEnum
from statistics import fmean
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

DETECTOR_VERSION: Final = "1"
DETECTABLE_SNR_FLOOR: Final = 0.8

PATTERN_KINDS: Final[frozenset[str]] = frozenset(
    {
        "trend",
        "concentration",
        "seasonal",
        "baseline_deviation",
        "ratio",
        "concentration_signal",
    }
)

# 月份日历是 Numeric Detector Pack 的版本化观察坐标系（kind/unit 共享面的一部分）。
MONTH_CALENDAR: Final[tuple[str, ...]] = tuple(
    f"{year}-{month:02d}" for year in (2023, 2024, 2025) for month in range(1, 13)
)

_SIGMA_FLOOR: Final = 1e-9


class Direction(StrEnum):
    RISE = "rise"
    DECLINE = "decline"


def robust_sigma(values: list[float]) -> float:
    """1.4826 * MAD，下沿 1e-9：常数序列不得除零，也不得返回 0。"""
    if len(values) < 2:
        return _SIGMA_FLOOR
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    deviations = sorted(abs(v - median) for v in values)
    mid = len(deviations) // 2
    mad = deviations[mid] if len(deviations) % 2 else (deviations[mid - 1] + deviations[mid]) / 2
    return max(1.4826 * mad, _SIGMA_FLOOR)


def window_iou(
    a_start: str, a_end: str, b_start: str, b_end: str, months: tuple[str, ...] = MONTH_CALENDAR
) -> float:
    """窗口 IoU（RISK_EVAL §7 匹配规则的几何量）。"""
    index = {m: i for i, m in enumerate(months)}
    a_lo, a_hi = index[a_start], index[a_end]
    b_lo, b_hi = index[b_start], index[b_end]
    intersection = max(0, min(a_hi, b_hi) - max(a_lo, b_lo) + 1)
    union = max(a_hi, b_hi) - min(a_lo, b_lo) + 1
    return intersection / union


def difficulty_band(snr: float) -> str:
    """难度由数据复算，不由主观标注（RISK_EVAL §5）。"""
    if snr < DETECTABLE_SNR_FLOOR:
        return "sub_threshold"
    if snr < 1.5:
        return "hard"
    if snr < 3.0:
        return "medium"
    return "easy"


def _ols(values: list[float]) -> tuple[float, float]:
    """最小二乘 (slope, intercept)，x = 0..n-1。"""
    n = len(values)
    mx = (n - 1) / 2
    my = fmean(values)
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (y - my) for i, y in enumerate(values))
    slope = sxy / sxx
    return slope, my - slope * mx


class PatternFinding(BaseModel):
    """一条候选模式的结构化发现：窗口、realized SNR、方向与证据规模。

    冻结模型：detector output 是 immutable 事实，resolution/human triage
    不得改写（spec §4）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    entity_dim: str
    entity_value: str
    metric: str
    window_start: str
    window_end: str
    snr: float = Field(ge=0.0)
    band: str
    direction: str
    change: float
    sigma: float
    units: str
    rows_observed: int = Field(ge=0)
    detector_version: str
    formula_id: str


class _Window:
    __slots__ = ("change", "end", "sigma", "snr", "start")

    def __init__(self, start: int, end: int, snr: float, change: float, sigma: float) -> None:
        self.start = start
        self.end = end
        self.snr = snr
        self.change = change
        self.sigma = sigma


def _scan_ramp(series: list[float | None], *, min_span: int, min_pre: int, min_post: int) -> _Window | None:
    """三段式 ramp 扫描：平坦前段 / 线性爬升段 / 平坦后段。

    植入语义（evals/scripts/gen_risk_data.py）是「窗口内线性爬升、窗口后保持不回弹」，
    线性 ramp + 前后平坦基准的拟合在窗口端点处残差最小——窗口估计因此对齐效应区间。
    """
    n = len(series)
    best: _Window | None = None
    for start in range(min_pre, n - min_span - min_post + 1):
        for end in range(start + min_span - 1, n - min_post + 1):
            window = series[start : end + 1]
            if any(v is None for v in window):
                continue
            slope, intercept = _ols([float(v) for v in window])  # type: ignore[arg-type]
            base = intercept
            final = intercept + slope * (len(window) - 1)
            residuals = [float(v) - base for v in series[:start] if v is not None]
            if len(residuals) < min_pre:
                continue
            residuals.extend(
                float(v) - (intercept + slope * i) for i, v in enumerate(window)  # type: ignore[arg-type]
            )
            tail = [float(v) - final for v in series[end + 1 :] if v is not None]
            if len(tail) < min_post:
                continue
            residuals.extend(tail)
            sigma = robust_sigma(residuals)
            change = slope * (len(window) - 1)
            snr = abs(change) / sigma
            if snr >= DETECTABLE_SNR_FLOOR and (best is None or snr > best.snr):
                best = _Window(start, end, snr, change, sigma)
    return best


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _scan_step(series: list[float | None], *, min_pre: int, min_post: int) -> _Window | None:
    """阶跃扫描：onset 之后的尾部水平对 pre 基线的偏移（P4 口径：pre robust sigma）。

    账期突变等 baseline deviation 在窗口后持续存在——受影响窗口是 onset 到序列末端。
    """
    n = len(series)
    best: _Window | None = None
    for start in range(min_pre, n - min_post + 1):
        pre = [float(v) for v in series[:start] if v is not None]
        post = [float(v) for v in series[start:] if v is not None]
        if len(pre) < min_pre or len(post) < min_post:
            continue
        sigma = robust_sigma(pre)
        change = _median(post) - _median(pre)
        snr = abs(change) / sigma
        if snr >= DETECTABLE_SNR_FLOOR and (best is None or snr > best.snr):
            best = _Window(start, n - 1, snr, change, sigma)
    return best


class SnapshotMetrics:
    """snapshot 的确定性序列视图：first-wins 去重 + 月对齐 + 聚合。

    脏数据（重复行、缺失值）不静默丢弃——重复行进入 D1 数据质量报告。
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables
        self._dedup: dict[str, list[dict[str, Any]]] = {}
        self._duplicates: dict[str, int] = {}
        self._missing: dict[str, int] = {}
        self._months: tuple[str, ...] = tuple(
            sorted({str(row["month"]) for rows in tables.values() for row in rows})
        )
        self._deduplicate()

    @classmethod
    def from_tables(
        cls,
        revenue: list[dict[str, Any]],
        receivable: list[dict[str, Any]],
        supply: list[dict[str, Any]],
        cashflow: list[dict[str, Any]],
    ) -> SnapshotMetrics:
        return cls(
            {
                "revenue": revenue,
                "receivable": receivable,
                "supply": supply,
                "cashflow": cashflow,
            }
        )

    def _deduplicate(self) -> None:
        entity_keys = {
            "revenue": ("month", "product_line", "region"),
            "receivable": ("month", "customer_id"),
            "supply": ("month", "supplier_id"),
            "cashflow": ("month", "product_line"),
        }
        for name, rows in self._tables.items():
            key_fields = entity_keys[name]
            seen: set[tuple[Any, ...]] = set()
            unique: list[dict[str, Any]] = []
            missing = 0
            for row in rows:
                key = tuple(row.get(field) for field in key_fields)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(row)
                for column, value in row.items():
                    if column not in key_fields and value in (None, ""):
                        missing += 1
            self._dedup[name] = unique
            self._duplicates[name] = len(rows) - len(unique)
            self._missing[name] = missing

    @property
    def months(self) -> tuple[str, ...]:
        return self._months

    def entity_values(self, table: str, dim: str) -> tuple[str, ...]:
        """某表某维度的去重实体清单（稳定排序，扫描顺序确定性）。"""
        return tuple(sorted({str(r[dim]) for r in self._dedup[table] if r.get(dim) is not None}))

    def series(self, table: str, dim: str, value: str, metric: str) -> list[float | None]:
        by_month: dict[str, float] = {}
        for row in self._dedup[table]:
            if str(row.get(dim)) != value:
                continue
            raw = row.get(metric)
            if raw in (None, ""):
                continue
            by_month.setdefault(str(row["month"]), float(raw))
        return [by_month.get(m) for m in self._months]

    def company_ratio_series(self, cash_metric: str, revenue_metric: str) -> list[float | None]:
        """公司级比值序列：分子分母逐月聚合后相除（不是逐行比值的平均）。"""
        numerators: dict[str, float] = {}
        denominators: dict[str, float] = {}
        for row in self._dedup["cashflow"]:
            cash = row.get(cash_metric)
            revenue = row.get(revenue_metric)
            if cash in (None, "") or revenue in (None, ""):
                continue
            month = str(row["month"])
            numerators[month] = numerators.get(month, 0.0) + float(cash)
            denominators[month] = denominators.get(month, 0.0) + float(revenue)
        series: list[float | None] = []
        for month in self._months:
            den = denominators.get(month)
            series.append(numerators[month] / den if den else None)
        return series

    def topk_share_series(self, table: str, k: int, metric: str) -> list[float | None]:
        """top-k 集中度序列：逐月取该 metric 最大的 k 个值求和。"""
        by_month: dict[str, list[float]] = {}
        for row in self._dedup[table]:
            raw = row.get(metric)
            if raw in (None, ""):
                continue
            by_month.setdefault(str(row["month"]), []).append(float(raw))
        series: list[float | None] = []
        for month in self._months:
            values = by_month.get(month, [])
            series.append(sum(sorted(values, reverse=True)[:k]) if len(values) >= k else None)
        return series

    def seasonal_deviation_series(self, base: list[float | None]) -> list[float | None]:
        """P3 口径：当期相对历史同月中位数的相对偏差。

        历史同月中位数由两年（i-12、i-24）同月值构成；有机增长带来的常数偏移由
        ramp 拟合的截距吸收，不影响窗口估计与 SNR。历史不足或基线为 0 记 None。
        """
        deviations: list[float | None] = []
        history_depth = 24
        for i in range(len(self._months)):
            if i < history_depth:
                deviations.append(None)
                continue
            historical = [v for v in (base[i - 12], base[i - 24]) if v is not None]
            current = base[i]
            if len(historical) < 2 or current is None:
                deviations.append(None)
                continue
            baseline = _median(historical)
            deviations.append((current - baseline) / baseline if baseline else None)
        return deviations

    def data_quality_report(self) -> dict[str, Any]:
        """D1 层输入：逐表行数、重复行、缺失单元格。"""
        return {
            "months": len(self._months),
            "tables": [
                {
                    "table": name,
                    "rows": len(self._dedup[name]),
                    "duplicates": self._duplicates[name],
                    "missing_cells": self._missing[name],
                }
                for name in ("revenue", "receivable", "supply", "cashflow")
            ],
        }


_METRIC_UNITS: Final[dict[str, str]] = {
    "gross_margin_rate": "ratio",
    "revenue_k": "k_currency",
    "revenue": "k_currency",
    "dso_days": "days",
    "cash_to_revenue": "ratio",
    "revenue_share": "ratio",
    "purchase_share": "ratio",
    "on_time_rate": "ratio",
}


class NumericPatternDetector:
    """逐 kind 结构化扫描：每个 (kind, dim, entity, metric) 只报 realized SNR 最高的窗口。"""

    def scan(self, metrics: SnapshotMetrics) -> tuple[PatternFinding, ...]:
        findings: list[PatternFinding] = []
        findings.extend(self._scan_trend(metrics))
        findings.extend(self._scan_seasonal(metrics))
        findings.extend(self._scan_baseline(metrics))
        findings.extend(self._scan_ratio(metrics))
        findings.extend(self._scan_concentration(metrics))
        findings.extend(self._scan_supplier_signal(metrics))
        return tuple(findings)

    def _finding(
        self,
        kind: str,
        dim: str,
        value: str,
        metric: str,
        window: _Window,
        formula_id: str,
        rows: int,
    ) -> PatternFinding:
        return PatternFinding(
            kind=kind,
            entity_dim=dim,
            entity_value=value,
            metric=metric,
            window_start=MONTH_CALENDAR[window.start],
            window_end=MONTH_CALENDAR[window.end],
            snr=window.snr,
            band=difficulty_band(window.snr),
            direction=Direction.DECLINE if window.change < 0 else Direction.RISE,
            change=window.change,
            sigma=window.sigma,
            units=_METRIC_UNITS.get(metric, "unknown"),
            rows_observed=rows,
            detector_version=DETECTOR_VERSION,
            formula_id=formula_id,
        )

    def _scan_trend(self, metrics: SnapshotMetrics) -> list[PatternFinding]:
        findings = []
        for dim in ("product_line", "region"):
            for value in metrics.entity_values("revenue", dim):
                series = metrics.series("revenue", dim, value, "gross_margin_rate")
                window = _scan_ramp(series, min_span=6, min_pre=6, min_post=3)
                if window is None:
                    continue
                rows = sum(1 for v in series if v is not None)
                findings.append(self._finding("trend", dim, value, "gross_margin_rate", window, "P1", rows))
        return findings

    def _scan_seasonal(self, metrics: SnapshotMetrics) -> list[PatternFinding]:
        findings = []
        for dim in ("product_line", "region"):
            for value in metrics.entity_values("revenue", dim):
                base = metrics.series("revenue", dim, value, "revenue_k")
                deviations = metrics.seasonal_deviation_series(base)
                window = _scan_ramp(deviations, min_span=4, min_pre=3, min_post=1)
                if window is None:
                    continue
                rows = sum(1 for v in deviations if v is not None)
                findings.append(self._finding("seasonal", dim, value, "revenue", window, "P3", rows))
        return findings

    def _scan_baseline(self, metrics: SnapshotMetrics) -> list[PatternFinding]:
        findings = []
        for value in metrics.entity_values("receivable", "customer_id"):
            series = metrics.series("receivable", "customer_id", value, "dso_days")
            window = _scan_step(series, min_pre=12, min_post=4)
            if window is None:
                continue
            rows = sum(1 for v in series if v is not None)
            findings.append(
                self._finding("baseline_deviation", "customer", value, "dso_days", window, "P4", rows)
            )
        return findings

    def _scan_ratio(self, metrics: SnapshotMetrics) -> list[PatternFinding]:
        findings = []
        for value in metrics.entity_values("cashflow", "product_line"):
            series = metrics.series("cashflow", "product_line", value, "cash_to_revenue")
            window = _scan_ramp(series, min_span=6, min_pre=12, min_post=3)
            if window is None:
                continue
            rows = sum(1 for v in series if v is not None)
            findings.append(
                self._finding("ratio", "product_line", value, "cash_to_revenue", window, "P5", rows)
            )
        company = metrics.company_ratio_series("operating_cash_k", "revenue_k")
        window = _scan_ramp(company, min_span=6, min_pre=12, min_post=3)
        if window is not None:
            rows = sum(1 for v in company if v is not None)
            findings.append(
                self._finding("ratio", "company", "全公司", "cash_to_revenue", window, "P5", rows)
            )
        return findings

    def _scan_concentration(self, metrics: SnapshotMetrics) -> list[PatternFinding]:
        findings = []
        specs = (
            ("receivable", 5, "revenue_share", "customer_top5"),
            ("supply", 3, "purchase_share", "supplier_top3"),
        )
        for table, k, metric, dim in specs:
            series = metrics.topk_share_series(table, k, metric)
            window = _scan_ramp(series, min_span=6, min_pre=6, min_post=3)
            if window is None:
                continue
            rows = sum(1 for v in series if v is not None)
            findings.append(self._finding("concentration", dim, "全公司", metric, window, "P2", rows))
        return findings

    def _scan_supplier_signal(self, metrics: SnapshotMetrics) -> list[PatternFinding]:
        """P6 联合条件：准时率下滑（ramp 扫描定窗）+ 同窗内采购占比上升（z >= 检出门槛）。"""
        findings = []
        for value in metrics.entity_values("supply", "supplier_id"):
            on_time = metrics.series("supply", "supplier_id", value, "on_time_rate")
            share = metrics.series("supply", "supplier_id", value, "purchase_share")
            window = _scan_ramp(on_time, min_span=6, min_pre=12, min_post=3)
            if window is None:
                continue
            pre_share = [float(v) for v in share[: window.start] if v is not None]
            post_share = [float(v) for v in share[window.end + 1 :] if v is not None]
            post_share = post_share or [float(v) for v in share[window.start : window.end + 1] if v is not None]
            if not pre_share or not post_share:
                continue
            base = _median(pre_share)
            share_sigma = max(robust_sigma(pre_share), 1e-4)
            if (_median(post_share) - base) / share_sigma < DETECTABLE_SNR_FLOOR:
                continue
            rows = sum(1 for v in on_time if v is not None)
            findings.append(
                self._finding(
                    "concentration_signal",
                    "supplier",
                    value,
                    "purchase_share+on_time_rate",
                    window,
                    "P6",
                    rows,
                )
            )
        return findings
