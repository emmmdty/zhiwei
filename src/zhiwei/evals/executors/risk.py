"""S8 risk suite executors：units 经生产 detector→hypothesis→falsification 路径判分。

suite 注册表、冻结资产加载、生产 pipeline 与 blind 快照生成见 zhiwei.evals.risk_suites
（specs/s8 §7/§8、RISK_EVAL §4）；blind scorer 的 SNR 复算保持独立实现。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from zhiwei.discover.hypotheses import (
    HypothesisStatus,
    RiskHypothesis,
)
from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.risk_suites import (
    BlindSnapshot,
    DiscoveryPassResult,
    RiskAssets,
    _build_metric_table,
    _finding_key,
    _stable_id,
    _window_iou,
    generate_blind_snapshot,
    load_risk_assets,
    run_discovery_pass,
    score_against_manifest,
)
from zhiwei.evidence.patterns.numeric import MONTH_CALENDAR, SnapshotMetrics
from zhiwei.evidence.patterns.probes import evaluate_probe, probe_evidence_tags


def _blind_scorer_targets(snapshot: BlindSnapshot) -> list[dict[str, Any]]:
    """blind scorer：从 snapshot 独立复算植入窗口上的 realized SNR（不导入 detector 实现）。

    只保留复算后 SNR >= 0.8 的目标（plantability 口径，RISK_EVAL §6）。
    """
    targets: list[dict[str, Any]] = []
    by_series: dict[str, list[float]] = {}
    for row in snapshot.revenue_rows:
        by_series.setdefault(f"margin:{row['product_line']}", []).append(row["gross_margin_rate"])
    for row in snapshot.cashflow_rows:
        by_series.setdefault(f"ratio:{row['product_line']}", []).append(row["cash_to_revenue"])
    for plant in snapshot.plants:
        key = f"margin:{plant['entity']['value']}" if plant["kind"] == "trend" else (
            f"ratio:{plant['entity']['value']}"
        )
        series = by_series[key]
        lo = MONTH_CALENDAR.index(plant["window"]["start"])
        hi = MONTH_CALENDAR.index(plant["window"]["end"])
        window = series[lo : hi + 1]
        n = len(window)
        mx = (n - 1) / 2
        my = sum(window) / n
        sxx = sum((k - mx) ** 2 for k in range(n))
        sxy = sum((k - mx) * (v - my) for k, v in enumerate(window))
        slope = sxy / sxx
        if plant["kind"] == "trend":
            residuals = [
                v - (my - slope * mx + slope * k) for k, v in enumerate(window)
            ]
            residuals += [v - series[0] for v in series[:lo]]
            residuals += [v - (my + slope * (n - 1 - mx)) for v in series[hi + 1 :]]
            ordered = sorted(residuals)
            mid = len(ordered) // 2
            sigma = max(1.4826 * (ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2), 1e-9)
            snr = abs(slope * (n - 1)) / sigma
        else:
            pre = series[:lo]
            pre_mean = sum(pre) / len(pre)
            residuals = [v - pre_mean for v in pre]
            ordered = sorted(residuals)
            mid = len(ordered) // 2
            sigma = max(1.4826 * (ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2), 1e-9)
            snr = abs(my - pre_mean) / sigma
        if snr >= 0.8:
            targets.append({**plant, "realized_snr": snr})
    return targets


# ---------------------------------------------------------------- 执行器


class NumericRiskSuiteExecutor:
    """numeric-risk-v1 执行器：units 经生产 detector→hypothesis→falsification 路径判分。

    detector pass 在首个 unit 执行时运行一次并缓存——units 是同一次生产扫描上的
    独立判分视图，不重复扫描（与 factqa 的 snapshot 复用同构）。
    """

    def __init__(self, assets: RiskAssets | None = None) -> None:
        self._assets = assets
        self._cached: tuple[DiscoveryPassResult, dict[str, Any]] | None = None

    def _pass_and_score(self) -> tuple[DiscoveryPassResult, dict[str, Any]]:
        if self._cached is None:
            assets = self._assets or load_risk_assets()
            pass_result = run_discovery_pass(
                assets, min_probes_required=2, evaluated_at=datetime(2026, 9, 4, tzinfo=UTC)
            )
            self._cached = (pass_result, score_against_manifest(pass_result.findings, assets.manifest))
        return self._cached

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        return SampleOutcome(
            unit=unit, status=SampleStatus.COMPLETED, result=self.result_for(unit.sample_id)
        )

    def result_for(self, sample_id: str) -> dict[str, Any]:
        pass_result, score = self._pass_and_score()
        if sample_id.startswith("planted:"):
            target_id = sample_id.split(":", 1)[1]
            entry = next((m for m in score["matched"] if m["id"] == target_id), None)
            return {
                "target": target_id,
                "matched": entry is not None,
                "iou": entry["iou"] if entry else None,
                "snr": entry["finding"]["snr"] if entry else None,
                "evidence_valid": entry["evidence_valid"] if entry else False,
                "correct": entry is not None and entry["evidence_valid"],
                "falsification_coverage": pass_result.falsification.falsification_coverage,
            }
        if sample_id.startswith("distractor:"):
            target_id = sample_id.split(":", 1)[1]
            reported = any(fp["id"] == target_id for fp in score["distractor_fps"])
            return {"target": target_id, "reported": reported, "correct": not reported}
        if sample_id == "falsification:pass":
            summary = pass_result.falsification
            return {
                "probes_executed": summary.probes_executed,
                "falsification_coverage": summary.falsification_coverage,
                "hypotheses_refuted": summary.hypotheses_refuted,
                "hypothesis_refutation_rate": summary.hypothesis_refutation_rate,
                "correct": summary.falsification_coverage == 1.0 and summary.hypotheses_total > 0,
            }
        raise ValueError(f"unknown numeric-risk unit: {sample_id}")


class DiscoverBlindSuiteExecutor:
    """discover-blind-v1 执行器：代码定义快照 + scorer 独立复算目标 + 注入必推翻假设。"""

    def __init__(self, snapshot: BlindSnapshot | None = None) -> None:
        self._snapshot = snapshot
        self._cached: tuple[BlindSnapshot, DiscoveryPassResult, SnapshotMetrics] | None = None

    def _pass(self) -> tuple[BlindSnapshot, DiscoveryPassResult, SnapshotMetrics]:
        if self._cached is None:
            snapshot = self._snapshot or generate_blind_snapshot()
            metrics = SnapshotMetrics.from_tables(
                snapshot.revenue_rows, [], [], snapshot.cashflow_rows
            )
            pass_result = run_discovery_pass(
                metrics, min_probes_required=2, evaluated_at=datetime(2026, 9, 4, tzinfo=UTC)
            )
            self._cached = (snapshot, pass_result, metrics)
        return self._cached

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        snapshot, pass_result, metrics = self._pass()
        return SampleOutcome(
            unit=unit,
            status=SampleStatus.COMPLETED,
            result=self.result_for(snapshot, pass_result, metrics, unit.sample_id),
        )

    def result_for(
        self,
        snapshot: BlindSnapshot,
        pass_result: DiscoveryPassResult,
        metrics: SnapshotMetrics,
        sample_id: str,
    ) -> dict[str, Any]:
        if sample_id == "blind:refutation":
            injected = _injected_refutable_hypothesis(metrics, pass_result)
            refuted = injected.status == HypothesisStatus.REJECTED
            return {
                "injected_hypothesis_refuted": refuted,
                "probe_passed": injected.falsification_results[0].passed,
                "actual_value": injected.falsification_results[0].actual_value,
                "correct": refuted,
            }
        if sample_id == "blind:coverage":
            injected = _injected_refutable_hypothesis(metrics, pass_result)
            refuted = injected.status == HypothesisStatus.REJECTED
            total = pass_result.falsification.hypotheses_total + 1
            # 注入假设的被推翻事实计入 refutation_rate——danger-signal 探针进 artifact：
            # 该指标在 blind suite 上不可能恒为 0。
            return {
                "probes_executed": pass_result.falsification.probes_executed + 1,
                "falsification_coverage": pass_result.falsification.falsification_coverage,
                "hypothesis_refutation_rate": (
                    pass_result.falsification.hypotheses_refuted + (1 if refuted else 0)
                )
                / total,
                "injected_refuted": refuted,
                "correct": pass_result.falsification.falsification_coverage == 1.0,
            }
        if sample_id == "blind:clean:PC":
            # 「干净」的诚实契约：PC 上不得出现 easy 档发现（不得把植入量级的效应
            # 归因到干净实体）。逐窗扫描的噪声底（~2-3σ）意味着亚门槛发现不可根除，
            # 它们是如实报告的 precision 负担，与 D2 的 distractor_fp_rate 同一立场。
            pc_findings = [f for f in pass_result.findings if f.entity_value == "PC"]
            easy = [f for f in pc_findings if f.band == "easy"]
            return {
                "target": "PC",
                "findings": len(pc_findings),
                "easy_band": len(easy),
                "correct": not easy,
            }
        _, kind, entity = sample_id.split(":")
        targets = _blind_scorer_targets(snapshot)
        target = next(
            (t for t in targets if t["kind"] == kind and t["entity"]["value"] == entity), None
        )
        if target is None:
            return {"target": sample_id, "scorer_target_found": False, "correct": False}
        matches = [
            f
            for f in pass_result.findings
            if _finding_key(f) == (kind, "product_line", entity)
            and _window_iou(
                (f.window_start, f.window_end),
                (target["window"]["start"], target["window"]["end"]),
            )
            >= 0.5
        ]
        matched = bool(matches)
        return {
            "target": target["id"],
            "scorer_realized_snr": target["realized_snr"],
            "matched": matched,
            "iou": _window_iou(
                (matches[0].window_start, matches[0].window_end),
                (target["window"]["start"], target["window"]["end"]),
            )
            if matched
            else None,
            "correct": matched,
        }


def _injected_refutable_hypothesis(
    metrics: SnapshotMetrics, pass_result: DiscoveryPassResult
) -> RiskHypothesis:
    """在 blind pass 上注入一条必然可被推翻的假设（真实走 probe 求值路径）。

    PC 是干净对照——假设「PC 的毛利率出现基线突变」，其 probe 断言「若为假，
    PC 毛利率全期均值应低于 0.45」。PC 数据恒在基线附近 → 观察成立 → 必须被推翻。
    refutation_rate 恒 0 是危险信号（spec §7）——注入保证该指标真实工作。
    """
    evaluated_at = datetime(2026, 9, 4, tzinfo=UTC)
    from zhiwei.discover.signals import NegativeProbe

    probe = NegativeProbe(
        probe_id=_stable_id("injected", "refutable"),
        metric="gross_margin_rate",
        entity_scope="product_line:PC",
        window_hours=36 * 720,
        comparator="lt",
        # 门槛 = 基线 + 0.02：噪声均值波动（σ/√36 ≈ 0.002）远够不到，
        # 而任何真实的基线突变都会把全期均值推过门槛 → 观察成立 → 推翻。
        threshold=0.47,
        description="若「PC 毛利率突变」假设为假，全期均值应低于 0.47",
    )
    table = _build_metric_table(metrics, pass_result.findings)
    if table.bounds("product_line:PC", "gross_margin_rate") is None:
        # PC 是干净对照，不产生 finding，其序列不在指标表里——显式补入以供注入 probe 求值。
        table.put(
            "product_line:PC",
            "gross_margin_rate",
            metrics.series("revenue", "product_line", "PC", "gross_margin_rate"),
        )
    result = evaluate_probe(probe, table, evaluated_at=evaluated_at)
    (tag,) = probe_evidence_tags([result], created_at=evaluated_at)
    return RiskHypothesis(
        id=_stable_id("hypothesis", "injected-refutable"),
        signal_id=_stable_id("signal", "injected-refutable"),
        program_version_id=pass_result.signals[0].program_version_id
        if pass_result.signals
        else _stable_id("program-version", "blind"),
        detector_pack_id=_stable_id("pack", "numeric"),
        detector_pack_version=1,
        title="注入假设：PC 毛利率基线突变（必然可被推翻）",
        description="danger-signal 探针：refutation_rate 恒 0 等于证伪机制没有在工作",
        affected_entities=("product_line:PC",),
        evidence_tags=(tag,),
        proposed_probes=(probe,),
        falsification_results=(result,),
        status=HypothesisStatus.READY_FOR_TRIAGE if result.passed else HypothesisStatus.REJECTED,
        metadata={"injected": True, "expected_refuted": True},
        created_at=evaluated_at,
        updated_at=evaluated_at,
    )
