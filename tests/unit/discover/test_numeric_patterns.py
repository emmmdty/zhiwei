"""Numeric Risk Detector Pack 的模式复算契约（RISK_EVAL §5/§7，spec s8 §5）。

生产 detector 路径的确定性组件：从 snapshot 序列复算 realized SNR、窗口与难度档。
判分器（scorer）与 detector 共享版本化 kind/unit，不共享实现——本文件的测试对象是
detector 侧的唯一实现（zhiwei.evidence.patterns.numeric），scorer 的复算在 eval 层独立进行。
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from zhiwei.evidence.patterns.numeric import (
    PATTERN_KINDS,
    NumericPatternDetector,
    SnapshotMetrics,
    difficulty_band,
    robust_sigma,
    window_iou,
)

MONTHS_36 = tuple(f"{y}-{m:02d}" for y in (2023, 2024, 2025) for m in range(1, 13))


def _white_noise(i: int, scale: float) -> float:
    """确定性白噪声：固定输入的伪随机扰动（测试 fixture 不得引入随机性）。"""
    import hashlib

    digest = hashlib.sha256(f"fixed-noise:{i}".encode()).hexdigest()
    uniform = int(digest[:8], 16) / 0xFFFFFFFF
    return scale * (uniform - 0.5) * 2


class TestRobustSigma:
    def test_constant_series_has_floor_sigma(self) -> None:
        # 全常数序列的 MAD=0：sigma 落在 1e-9 下沿，不得除零，也不得返回 0。
        assert robust_sigma([5.0, 5.0, 5.0]) == pytest.approx(1e-9)

    def test_known_mad(self) -> None:
        # 逐字节手工值：deviations sorted [0.5, 0.5, 1.5, 97.5] -> median = 1.0
        values = [1.0, 2.0, 3.0, 100.0]
        assert robust_sigma(values) == pytest.approx(1.4826 * 1.0)

    def test_empty_is_floor(self) -> None:
        assert robust_sigma([]) == pytest.approx(1e-9)


class TestWindowIou:
    def test_identical_windows(self) -> None:
        assert window_iou("2024-01", "2024-06", "2024-01", "2024-06", MONTHS_36) == 1.0

    def test_disjoint_windows(self) -> None:
        assert window_iou("2023-01", "2023-06", "2024-01", "2024-06", MONTHS_36) == 0.0

    def test_partial_overlap(self) -> None:
        # inter=2024-03..2024-06 (4) / union=2024-01..2024-09 (9)
        assert window_iou("2024-01", "2024-06", "2024-03", "2024-09", MONTHS_36) == pytest.approx(4 / 9)


class TestDifficultyBand:
    def test_bands_follow_risk_eval(self) -> None:
        assert difficulty_band(0.79) == "sub_threshold"
        assert difficulty_band(0.8) == "hard"
        assert difficulty_band(1.49) == "hard"
        assert difficulty_band(1.5) == "medium"
        assert difficulty_band(2.99) == "medium"
        assert difficulty_band(3.0) == "easy"


class TestSnapshotMetrics:
    def _rows(self) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        revenue = []
        for m in MONTHS_36:
            revenue.append(
                {
                    "month": m,
                    "product_line": "P1",
                    "region": "R1",
                    "revenue_k": 100.0,
                    "gross_margin_rate": 0.5,
                }
            )
        # 脏度：一条重复行（同 month+entity）——必须按 first-wins 去重
        revenue.append(dict(revenue[0]))
        receivable = [
            {"month": m, "customer_id": f"C{i:02d}", "dso_days": 47.0, "revenue_share": 0.05}
            for m in MONTHS_36
            for i in (1, 2)
        ]
        supply = [
            {"month": m, "supplier_id": f"S{i:02d}", "purchase_share": 0.1, "on_time_rate": 0.96}
            for m in MONTHS_36
            for i in (1, 2)
        ]
        cashflow = [
            {"month": m, "product_line": "P1", "revenue_k": 100.0, "operating_cash_k": 31.0}
            for m in MONTHS_36
        ]
        return revenue, receivable, supply, cashflow

    def test_first_wins_dedup_on_duplicate_rows(self) -> None:
        revenue, receivable, supply, cashflow = self._rows()
        metrics = SnapshotMetrics.from_tables(revenue, receivable, supply, cashflow)
        # 重复行若未去重，序列长度会超过 36 期
        assert len(metrics.series("revenue", "product_line", "P1", "gross_margin_rate")) == 36

    def test_duplicate_rows_counted_in_quality_report(self) -> None:
        revenue, receivable, supply, cashflow = self._rows()
        metrics = SnapshotMetrics.from_tables(revenue, receivable, supply, cashflow)
        report = metrics.data_quality_report()
        duplicates = {d["table"]: d["duplicates"] for d in report["tables"]}
        assert duplicates["revenue"] == 1

    def test_missing_value_tracked_as_none(self) -> None:
        revenue, receivable, supply, cashflow = self._rows()
        revenue[5]["gross_margin_rate"] = None
        metrics = SnapshotMetrics.from_tables(revenue, receivable, supply, cashflow)
        series = metrics.series("revenue", "product_line", "P1", "gross_margin_rate")
        assert series[5] is None


class TestNumericPatternDetector:
    def _declining_margin_rows(self, *, start: str = "2024-06", end: str = "2025-03", delta: float = -0.09):
        """按生成器语义构造 trend 植入：窗口内线性爬升，窗口后保持（不回弹）。

        叠加确定性白噪声（幅度=噪声档 0.012）：无噪声序列的 robust sigma 落在
        1e-9 下沿，SNR 失去意义（任何窗口都「完美拟合」），窗口估计也随之退化。
        """
        rows = []
        s_idx, e_idx = MONTHS_36.index(start), MONTHS_36.index(end)
        for i, m in enumerate(MONTHS_36):
            ramp = 0.0
            if i >= s_idx:
                ramp = 1.0 if i >= e_idx else (i - s_idx) / (e_idx - s_idx)
            mgn = round(0.62 + delta * ramp + _white_noise(i, 0.012), 4)
            rows.append(
                {
                    "month": m,
                    "product_line": "云梯-企业版",
                    "region": "华东",
                    "revenue_k": 300.0,
                    "cost_k": 300.0 * (1 - mgn),
                    "gross_margin_rate": mgn,
                }
            )
        return rows

    def _detector(self) -> NumericPatternDetector:
        return NumericPatternDetector()

    def test_detects_planted_trend_with_matching_window(self) -> None:
        rows = self._declining_margin_rows()
        metrics = SnapshotMetrics.from_tables(rows, [], [], [])
        findings = self._detector().scan(metrics)
        trends = [f for f in findings if f.kind == "trend" and f.entity_value == "云梯-企业版"]
        assert trends, "clean 构造的 0.09 跌幅必须被检出"
        best = max(trends, key=lambda f: f.snr)
        assert best.entity_dim == "product_line"
        assert best.metric == "gross_margin_rate"
        # 窗口必须落在植入窗口附近（IoU >= 0.5 的可匹配性由 scorer 复核，这里验证结构）
        assert MONTHS_36.index(best.window_start) >= MONTHS_36.index("2024-04")
        assert best.direction == "decline"
        assert best.band == "easy"
        assert best.rows_observed >= 6

    def test_clean_series_stays_below_easy_band(self) -> None:
        """纯噪声序列不得产生 easy 档发现。

        逐窗扫描在纯噪声上的最大统计量存在固有噪声底（~2-3σ）：这正是 D2 层把
        distractor_fp_rate 作为报告指标而非断言的原因——这里只锁定噪声底不越过
        easy 档下沿这一更强的性质。
        """
        rows = self._declining_margin_rows(delta=0.0)
        metrics = SnapshotMetrics.from_tables(rows, [], [], [])
        findings = [
            f for f in self._detector().scan(metrics) if f.entity_value == "云梯-企业版"
        ]
        assert all(f.band != "easy" for f in findings)

    def test_scan_is_deterministic_byte_for_byte(self) -> None:
        rows = self._declining_margin_rows()
        metrics = SnapshotMetrics.from_tables(rows, [], [], [])
        first = self._detector().scan(metrics)
        second = self._detector().scan(metrics)
        import json

        assert json.dumps([f.model_dump(mode="json") for f in first]) == json.dumps(
            [f.model_dump(mode="json") for f in second]
        )

    def test_findings_are_frozen_models(self) -> None:
        rows = self._declining_margin_rows()
        metrics = SnapshotMetrics.from_tables(rows, [], [], [])
        findings = self._detector().scan(metrics)
        assert findings
        with pytest.raises(ValidationError):
            findings[0].snr = 999.0  # type: ignore[misc]

    def test_sub_threshold_effect_not_promoted_to_easy(self) -> None:
        """亚门槛效应（delta=-0.008，对应 D-001 量级）不得被抬升为 easy 档发现。

        该效应的 realized SNR（0.67）低于逐窗扫描的噪声底——detector 无法把它从
        噪声里分离出来（这是 honest FP，不是漏检）；契约是它不得被「放大」：
        报出的发现不得进入 easy 档，变化幅度不得接近真实植入的量级。
        """
        rows = self._declining_margin_rows(delta=-0.008)
        metrics = SnapshotMetrics.from_tables(rows, [], [], [])
        findings = [
            f for f in self._detector().scan(metrics) if f.entity_value == "云梯-企业版"
        ]
        assert all(f.band != "easy" for f in findings)
        assert all(abs(f.change) < 0.05 for f in findings)

    def test_pattern_kinds_cover_risk_eval_catalog(self) -> None:
        assert frozenset(
            {
                "trend",
                "concentration",
                "seasonal",
                "baseline_deviation",
                "ratio",
                "concentration_signal",
            }
        ) == PATTERN_KINDS

    def test_baseline_deviation_uses_pre_sigma(self) -> None:
        """RISK_EVAL §5 P4：post/pre 中位差 / pre robust sigma。"""
        receivable = []
        s_idx = MONTHS_36.index("2025-02")
        for i, m in enumerate(MONTHS_36):
            dso = 47.0 * (2.4 if i >= s_idx else 1.0) + _white_noise(i, 3.2)
            receivable.append({"month": m, "customer_id": "C03", "dso_days": round(dso, 1)})
        metrics = SnapshotMetrics.from_tables([], receivable, [], [])
        findings = self._detector().scan(metrics)
        base = [f for f in findings if f.kind == "baseline_deviation" and f.entity_value == "C03"]
        assert base, "2.4x 的账期突变必须被检出"
        assert base[0].band == "easy"

    def test_supplier_signal_requires_both_share_and_ontime(self) -> None:
        """P6：占比上升 + 准时率下滑必须同时成立，单一条件不得触发。"""
        supply_share_only = []
        s_idx = MONTHS_36.index("2024-10")
        for i, m in enumerate(MONTHS_36):
            share = 0.10 + (0.22 if i >= s_idx else 0.0)
            supply_share_only.append(
                {"month": m, "supplier_id": "S02", "purchase_share": share, "on_time_rate": 0.96}
            )
        metrics = SnapshotMetrics.from_tables([], [], supply_share_only, [])
        findings = [f for f in self._detector().scan(metrics) if f.kind == "concentration_signal"]
        assert not findings, "只有占比上升、准时率未下滑时不得报 concentration_signal"

    def test_ratio_requires_joint_divergence(self) -> None:
        """P5：营收升而现金流同比例升（ratio 不变）不得报背离。"""
        cashflow = [
            {"month": m, "product_line": "P1", "revenue_k": 100.0, "operating_cash_k": 31.0,
             "cash_to_revenue": 0.31}
            for m in MONTHS_36
        ]
        metrics = SnapshotMetrics.from_tables([], [], [], cashflow)
        findings = [f for f in self._detector().scan(metrics) if f.kind == "ratio"]
        assert findings == []

    def test_math_domains_stay_finite(self) -> None:
        """全 None / 全同值等退化序列不得产生 inf/nan。"""
        rows = self._declining_margin_rows()
        for r in rows:
            r["gross_margin_rate"] = None
        metrics = SnapshotMetrics.from_tables(rows, [], [], [])
        findings = self._detector().scan(metrics)
        assert all(math.isfinite(f.snr) for f in findings)
