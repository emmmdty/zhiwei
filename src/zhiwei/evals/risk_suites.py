"""S8 risk suite 注册表与生产 detector 路径（specs/s8 §7/§8）。

事实源：specs/s8-discover-actions.md、docs/RISK_EVAL.md、ADR-004、ADR-013 决策 2。

- 生产 detector 路径：evals/risk/ 冻结合成经营数据 → SnapshotMetrics 序列提取 →
  NumericPatternDetector 逐 kind 结构化扫描 → PatternFinding → Signal → RiskHypothesis
  → 独立 probe 生成与确定性求值 → 准入判定。detector 只读 snapshot，不读植入清单；
  植入清单只在 --check / suite 判分时作为 ground truth 参与（RISK_EVAL §4）。
- 冻结资产以 sha256 对 evals/CHECKSUMS.sha256 校验，不符即拒绝（fail closed）。
- discover-blind-v1 是代码定义的 blind suite：clean seed 数据由确定性生成器产生
  （digest 记入 artifact，绝不写入 evals/）；blind 语义 = detector 执行时不可见
  ground truth，判分目标由 scorer 从 snapshot 独立复算 realized SNR（RISK_EVAL §4）。
  生成器/detector/scorer 不共享实现——scorer 的 SNR 复算是独立实现（executors/risk）。

suite 基建统一在 evals/ 层：CLI（zhiwei.cli.risk）只保留 `risk generate` 命令与
D0-D6 口径逻辑；executor 见 zhiwei.evals.executors.risk。
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from zhiwei.cases.risk_fingerprint import (
    DedupeDecision,
    RiskFingerprint,
    RiskFingerprintIndex,
)
from zhiwei.contracts.canonical import canonical_json, digest, digest_bytes
from zhiwei.discover.hypotheses import (
    HypothesisStatus,
    RiskHypothesis,
)
from zhiwei.discover.signals import (
    DataQualityResult,
    Signal,
    SignalSeverity,
    Watermark,
)
from zhiwei.evals.domain import RegisteredUnit
from zhiwei.evidence.patterns.numeric import (
    MONTH_CALENDAR,
    NumericPatternDetector,
    PatternFinding,
    SnapshotMetrics,
)
from zhiwei.evidence.patterns.probes import (
    ProbeMetricTable,
    evaluate_probe,
    generate_probes_for_finding,
    probe_evidence_tags,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RISK_ASSET_DIR = REPO_ROOT / "evals" / "risk"
CHECKSUM_FILE = REPO_ROOT / "evals" / "CHECKSUMS.sha256"

NUMERIC_RISK_V1 = "numeric-risk-v1"
DISCOVER_BLIND_V1 = "discover-blind-v1"
RISK_SUITE_NAMES = frozenset({NUMERIC_RISK_V1, DISCOVER_BLIND_V1})

_EXECUTOR_KIND = "numeric-detector-pack"
_PRODUCTION_PATH = (
    "FrozenRiskSnapshot->NumericPatternDetector->Signal->RiskHypothesis"
    "->NegativeProbe(deterministic)->FalsificationResult"
)

_PACK_NAMESPACE = uuid5(NAMESPACE_URL, "zhiwei:discover:numeric-detector-pack")
_EVAL_NAMESPACE = uuid5(NAMESPACE_URL, "zhiwei:evals:discover")


_RISK_TABLES = ("fact_revenue", "fact_receivable", "fact_supply", "fact_cashflow")


class AssetDigestError(RuntimeError):
    """冻结资产缺失或 sha256 与 CHECKSUMS 不符——fail closed。"""


# ---------------------------------------------------------------- 冻结资产


def _repo_root() -> Path:
    return REPO_ROOT


def _verify_asset_checksums(root: Path) -> dict[str, str]:
    """对 evals/risk/ 全部冻结文件核对 CHECKSUMS.sha256；返回 path→sha256。"""
    checksums: dict[str, str] = {}
    for line in (root / "evals" / "CHECKSUMS.sha256").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value, _, path = line.partition("  ")
        if path.startswith("evals/risk/"):
            checksums[path] = value.strip()
    risk_dir = root / "evals" / "risk"
    if not risk_dir.is_dir():
        raise AssetDigestError(f"冻结风险资产目录缺失: {risk_dir}")
    actual: dict[str, str] = {}
    for path in sorted(risk_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path, expected in checksums.items():
        found = actual.get(path)
        if found is None:
            raise AssetDigestError(f"冻结资产缺失: {path}")
        if found != expected:
            raise AssetDigestError(
                f"asset digest mismatch: {path} (expected {expected[:12]}…, got {found[:12]}…)"
            )
    return actual


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@dataclass(frozen=True)
class RiskAssets:
    """冻结资产的已验证视图：CSV 序列、植入清单、内容摘要。"""

    metrics: SnapshotMetrics
    manifest: dict[str, Any]
    csv_digests: dict[str, str]
    digest: str


def load_risk_assets(root: Path | None = None) -> RiskAssets:
    """加载并校验冻结资产；digest 不符抛 AssetDigestError（fail closed）。"""
    base = root or _repo_root()
    digests = _verify_asset_checksums(base)
    risk_dir = base / "evals" / "risk"
    tables = {name: _read_csv(risk_dir / "csv" / f"{name}.csv") for name in _RISK_TABLES}
    metrics = SnapshotMetrics.from_tables(
        tables["fact_revenue"], tables["fact_receivable"], tables["fact_supply"], tables["fact_cashflow"]
    )
    manifest = json.loads((risk_dir / "planted_manifest.json").read_text(encoding="utf-8"))
    asset_digest = digest(
        {
            "files": [{"path": p, "digest": d} for p, d in sorted(digests.items()) if p.startswith("evals/risk/")],
        }
    )
    return RiskAssets(
        metrics=metrics,
        manifest=manifest,
        csv_digests={p: d for p, d in digests.items() if p.startswith("evals/risk/")},
        digest=asset_digest,
    )


# ---------------------------------------------------------------- 指纹与指标表


def fingerprint_of_finding(finding: PatternFinding) -> RiskFingerprint:
    return RiskFingerprint(
        kind=finding.kind,
        metric=finding.metric,
        entity_dim=finding.entity_dim,
        entity_value=finding.entity_value,
        window_start=finding.window_start,
        window_end=finding.window_end,
        detector_version=finding.detector_version,
    )


def _stable_id(*parts: str) -> Any:
    return uuid5(_PACK_NAMESPACE, ":".join(parts))


def _series_for_finding(metrics: SnapshotMetrics, finding: PatternFinding) -> list[float | None]:
    """重建 finding 所依据的序列（probe 求值与 detector 看到同一份数据）。"""
    kind = finding.kind
    if kind == "trend":
        return metrics.series("revenue", finding.entity_dim, finding.entity_value, "gross_margin_rate")
    if kind == "seasonal":
        base = metrics.series("revenue", finding.entity_dim, finding.entity_value, "revenue_k")
        return metrics.seasonal_deviation_series(base)
    if kind == "baseline_deviation":
        return metrics.series("receivable", "customer_id", finding.entity_value, "dso_days")
    if kind == "ratio":
        if finding.entity_dim == "company":
            return metrics.company_ratio_series("operating_cash_k", "revenue_k")
        return metrics.series("cashflow", "product_line", finding.entity_value, "cash_to_revenue")
    if kind == "concentration":
        table, k, metric = (
            ("receivable", 5, "revenue_share")
            if finding.entity_dim == "customer_top5"
            else ("supply", 3, "purchase_share")
        )
        return metrics.topk_share_series(table, k, metric)
    if kind == "concentration_signal":
        return metrics.series("supply", "supplier_id", finding.entity_value, "on_time_rate")
    raise ValueError(f"unknown finding kind: {kind}")


def _build_metric_table(metrics: SnapshotMetrics, findings: tuple[PatternFinding, ...]) -> ProbeMetricTable:
    table = ProbeMetricTable()
    for finding in findings:
        scope = f"{finding.entity_dim}:{finding.entity_value}"
        series = _series_for_finding(metrics, finding)
        table.put(scope, finding.metric, series)
    return table


# ---------------------------------------------------------------- pipeline


@dataclass(frozen=True)
class FalsificationSummary:
    """ADR-004 一等指标：falsification_coverage 与 hypothesis_refutation_rate。"""

    probes_generated: int
    probes_executed: int
    falsification_coverage: float
    hypotheses_total: int
    hypotheses_refuted: int
    hypotheses_ready: int
    hypothesis_refutation_rate: float


@dataclass
class DiscoveryPassResult:
    findings: tuple[PatternFinding, ...]
    signals: tuple[Signal, ...]
    hypotheses: tuple[RiskHypothesis, ...]
    dedupe_new: int
    dedupe_duplicate: int
    merge_candidates: int
    falsification: FalsificationSummary
    quality_report: dict[str, Any]
    pass_digest: str


_SEVERITY_BY_BAND = {
    "easy": SignalSeverity.HIGH,
    "medium": SignalSeverity.WARNING,
    "hard": SignalSeverity.INFO,
}

_VALIDATION_ACTIONS = (
    "复核 source ledger 对应窗口明细",
    "向业务 owner 请求窗口内对照数据",
)


def run_discovery_pass(
    assets: RiskAssets | SnapshotMetrics,
    *,
    min_probes_required: int = 2,
    evaluated_at: datetime,
    fingerprint_index: RiskFingerprintIndex | None = None,
) -> DiscoveryPassResult:
    """生产路径一整遍：detector → 去重 → Signal → Hypothesis → 序贯证伪 → 准入。

    全部 id 由内容派生（uuid5）、时钟由 evaluated_at 注入——同输入逐字节同结果。
    """
    metrics = assets.metrics if isinstance(assets, RiskAssets) else assets
    index = fingerprint_index or RiskFingerprintIndex()
    findings = NumericPatternDetector().scan(metrics)
    probe_table = _build_metric_table(metrics, findings)

    signals: list[Signal] = []
    hypotheses: list[RiskHypothesis] = []
    dedupe_new = 0
    dedupe_duplicate = 0
    merge_candidates = 0

    quality_report = metrics.data_quality_report()
    dq_results = tuple(
        DataQualityResult(
            check_name=f"table_integrity:{entry['table']}",
            passed=entry["duplicates"] == 0 and entry["missing_cells"] == 0,
            details={"duplicates": entry["duplicates"], "missing_cells": entry["missing_cells"]},
            row_count=entry["rows"],
        )
        for entry in quality_report["tables"]
    )
    watermarks = tuple(
        Watermark(
            source_id=_stable_id("watermark", entry["table"]),
            field_name="month",
            value=metrics.months[-1] if metrics.months else None,
            captured_at=evaluated_at,
        )
        for entry in quality_report["tables"]
    )

    probes_generated = 0
    refuted_count = 0
    ready_count = 0

    for finding in findings:
        fp = fingerprint_of_finding(finding)
        merge_candidates += len(index.merge_candidates(fp))
        decision, _ = index.register(fp)
        if decision is DedupeDecision.DUPLICATE:
            dedupe_duplicate += 1
            continue
        dedupe_new += 1

        scope = f"{finding.entity_dim}:{finding.entity_value}"
        signal = Signal(
            id=_stable_id("signal", fp.value()),
            program_version_id=_stable_id("program-version", "numeric-risk"),
            detector_pack_id=_stable_id("pack", "numeric"),
            detector_pack_version=1,
            severity=_SEVERITY_BY_BAND.get(finding.band, SignalSeverity.INFO),
            title=f"{finding.kind}: {scope} {finding.metric} {finding.direction}",
            description=(
                f"realized SNR {finding.snr:.3f} ({finding.band}), change {finding.change:.4g} "
                f"over {finding.window_start}..{finding.window_end} (formula {finding.formula_id})"
            ),
            source_watermarks=watermarks,
            data_quality_results=dq_results,
            affected_entities=(scope,),
            metadata={
                "snr": finding.snr,
                "band": finding.band,
                "formula_id": finding.formula_id,
                "detector_version": finding.detector_version,
            },
            created_at=evaluated_at,
        )
        signals.append(signal)

        probes = generate_probes_for_finding(finding, probe_table)
        results = [
            evaluate_probe(probe, probe_table, evaluated_at=evaluated_at) for probe in probes
        ]
        tags = probe_evidence_tags(results, created_at=evaluated_at)
        if any(not r.passed for r in results):
            status = HypothesisStatus.REJECTED
            refuted_count += 1
        elif len(results) >= min_probes_required:
            status = HypothesisStatus.READY_FOR_TRIAGE
            ready_count += 1
        else:
            status = HypothesisStatus.PROPOSED
        hypotheses.append(
            RiskHypothesis(
                id=_stable_id("hypothesis", fp.value()),
                signal_id=signal.id,
                program_version_id=signal.program_version_id,
                detector_pack_id=signal.detector_pack_id,
                detector_pack_version=1,
                title=f"假设：{scope} {finding.metric} 呈{finding.direction}（{finding.band}）",
                description=signal.description,
                affected_entities=(scope,),
                source_watermarks=watermarks,
                evidence_tags=tags,
                proposed_probes=probes,
                falsification_results=tuple(results),
                status=status,
                suggested_validation_actions=_VALIDATION_ACTIONS,
                metadata={
                    "snr": finding.snr,
                    "band": finding.band,
                    "formula_id": finding.formula_id,
                    "detector_version": finding.detector_version,
                },
                created_at=evaluated_at,
                updated_at=evaluated_at,
            )
        )
        probes_generated += len(probes)

    total = len(hypotheses)
    # ADR-004 口径：falsification_coverage = 有 probe 的 hypothesis 数 / hypothesis
    # 总数。probes_generated/probes_generated 是恒真式，无法区分「每假设都有 probe」
    # 与「probe 集中在少数假设」（generate_probes_for_finding 在指标表缺序列等
    # 情况下返回空，该口径真实可达）。
    hypotheses_with_probes = sum(1 for h in hypotheses if h.proposed_probes)
    summary = FalsificationSummary(
        probes_generated=probes_generated,
        probes_executed=probes_generated,
        falsification_coverage=(hypotheses_with_probes / total) if total else 0.0,
        hypotheses_total=total,
        hypotheses_refuted=refuted_count,
        hypotheses_ready=ready_count,
        hypothesis_refutation_rate=(refuted_count / total) if total else 0.0,
    )
    pass_digest = digest_bytes(
        canonical_json([f.model_dump(mode="json") for f in findings])
    )
    return DiscoveryPassResult(
        findings=findings,
        signals=tuple(signals),
        hypotheses=tuple(hypotheses),
        dedupe_new=dedupe_new,
        dedupe_duplicate=dedupe_duplicate,
        merge_candidates=merge_candidates,
        falsification=summary,
        quality_report=quality_report,
        pass_digest=pass_digest,
    )


# ---------------------------------------------------------------- 判分（eval 侧）


def _window_iou(a: tuple[str, str], b: tuple[str, str]) -> float:
    index = {m: i for i, m in enumerate(MONTH_CALENDAR)}
    a_lo, a_hi = index[a[0]], index[a[1]]
    b_lo, b_hi = index[b[0]], index[b[1]]
    intersection = max(0, min(a_hi, b_hi) - max(a_lo, b_lo) + 1)
    union = max(a_hi, b_hi) - min(a_lo, b_lo) + 1
    return intersection / union


def _finding_key(finding: PatternFinding) -> tuple[str, str, str]:
    return (finding.kind, finding.entity_dim, finding.entity_value)


def _target_key(target: dict[str, Any]) -> tuple[str, str, str]:
    return (target["kind"], target["entity"]["dim"], target["entity"]["value"])


def score_against_manifest(
    findings: tuple[PatternFinding, ...], manifest: dict[str, Any]
) -> dict[str, Any]:
    """与冻结植入清单的确定性比对（RISK_EVAL §7 匹配规则）。

    kind+entity+metric 相同且 window IoU >= 0.5；一对一：按 (kind, entity) 遍历、
    IoU 降序、id 升序平局，已匹配目标不再复用。
    """
    planted = manifest["planted"]
    matched: dict[str, dict[str, Any]] = {}
    used_findings: set[int] = set()
    candidates: list[tuple[float, str, int, PatternFinding, dict[str, Any]]] = []
    for fi, finding in enumerate(findings):
        for target in planted:
            if _finding_key(finding) != _target_key(target):
                continue
            if target["metric"] != finding.metric and finding.kind != "concentration_signal":
                continue
            iou = _window_iou(
                (finding.window_start, finding.window_end),
                (target["window"]["start"], target["window"]["end"]),
            )
            if iou >= 0.5:
                candidates.append((iou, target["id"], fi, finding, target))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    for iou, target_id, fi, finding, target in candidates:
        if target_id in matched or fi in used_findings:
            continue
        used_findings.add(fi)
        matched[target_id] = {
            "finding": finding.model_dump(mode="json"),
            "iou": iou,
            "evidence_valid": _evidence_valid(finding, target),
        }
    # distractor：正确行为是不报；报出且窗口 IoU>=0.5 计入 distractor_fp_rate。
    distractor_fps = []
    for distractor in manifest["distractors"]:
        for finding in findings:
            if _finding_key(finding) != (
                distractor["kind"],
                distractor["entity"]["dim"],
                distractor["entity"]["value"],
            ):
                continue
            if _window_iou(
                (finding.window_start, finding.window_end),
                (distractor["window"]["start"], distractor["window"]["end"]),
            ) >= 0.5:
                distractor_fps.append(
                    {"id": distractor["id"], "snr": finding.snr}
                )
                break
    matched_by_difficulty = {"easy": 0, "medium": 0, "hard": 0}
    planted_by_difficulty = {"easy": 0, "medium": 0, "hard": 0}
    for target in planted:
        planted_by_difficulty[target["difficulty"]] += 1
        if target["id"] in matched:
            matched_by_difficulty[target["difficulty"]] += 1
    recall_by_difficulty = {
        band: (matched_by_difficulty[band] / count if count else None)
        for band, count in planted_by_difficulty.items()
    }
    evidence_valid_count = sum(1 for m in matched.values() if m["evidence_valid"])
    return {
        "planted_count": len(planted),
        "matched_count": len(matched),
        "recall": {
            "overall": len(matched) / len(planted) if planted else 0.0,
            "by_declared_difficulty": recall_by_difficulty,
        },
        "precision": len(matched) / len(findings) if findings else 0.0,
        "distractor_fp_rate": len(distractor_fps) / len(manifest["distractors"])
        if manifest["distractors"]
        else 0.0,
        "evidence_validity": evidence_valid_count / len(matched) if matched else 0.0,
        "matched": [
            {"id": target_id, **payload} for target_id, payload in sorted(matched.items())
        ],
        "missed": [t["id"] for t in planted if t["id"] not in matched],
        "distractor_fps": distractor_fps,
    }


def _evidence_valid(finding: PatternFinding, target: dict[str, Any]) -> bool:
    """RISK_EVAL §7：pattern 命中但 Evidence 失败时 evidence_validity=0。

    期望证据：至少 min_rows 行、必须引用 metric 与维度字段。
    """
    expected = target.get("expected_evidence", {})
    referenced = {finding.metric, finding.entity_dim, finding.entity_value}
    must_reference = set(expected.get("must_reference", []))
    return finding.rows_observed >= expected.get("min_rows", 1) and must_reference <= referenced


# ---------------------------------------------------------------- suite 定义


@dataclass(frozen=True)
class RiskSuiteDefinition:
    """risk suite 的冻结视图：注册单位、生产路径绑定与资产来源。"""

    name: str
    executor_kind: str
    production_path: str
    units: tuple[RegisteredUnit, ...]
    source: str  # "frozen-asset" | "code-defined"
    asset_digest: str | None


def _numeric_risk_units(manifest: dict[str, Any]) -> tuple[RegisteredUnit, ...]:
    units = [RegisteredUnit(sample_id=f"planted:{p['id']}", unit_id=f"{p['kind']}:{p['entity']['value']}") for p in manifest["planted"]]
    units += [
        RegisteredUnit(sample_id=f"distractor:{d['id']}", unit_id=f"{d['kind']}:{d['entity']['value']}")
        for d in manifest["distractors"]
    ]
    units.append(RegisteredUnit(sample_id="falsification:pass", unit_id="falsification-stage"))
    return tuple(units)


def resolve_risk_suite(suite: str) -> RiskSuiteDefinition:
    """按名解析 risk suite；未知名称 fail closed（LookupError）。"""
    if suite == NUMERIC_RISK_V1:
        assets = load_risk_assets()
        return RiskSuiteDefinition(
            name=suite,
            executor_kind=_EXECUTOR_KIND,
            production_path=_PRODUCTION_PATH,
            units=_numeric_risk_units(assets.manifest),
            source="frozen-asset",
            asset_digest=assets.digest,
        )
    if suite == DISCOVER_BLIND_V1:
        snapshot = generate_blind_snapshot()
        return RiskSuiteDefinition(
            name=suite,
            executor_kind=_EXECUTOR_KIND,
            production_path=_PRODUCTION_PATH,
            units=tuple(
                RegisteredUnit(sample_id=unit.sample_id, unit_id=unit.unit_id)
                for unit in _blind_units()
            ),
            source="code-defined",
            asset_digest=snapshot.digest,
        )
    raise LookupError(f"未知 risk suite: {suite}")


# ---------------------------------------------------------------- blind suite

_BLIND_TREND_WINDOW = ("2024-06", "2025-03")
_BLIND_RATIO_WINDOW = ("2025-01", "2025-11")


@dataclass(frozen=True)
class BlindSnapshot:
    """代码定义的 clean seed 数据：确定性生成、digest 稳定、不落 evals/。"""

    revenue_rows: list[dict[str, Any]]
    cashflow_rows: list[dict[str, Any]]
    plants: tuple[dict[str, Any], ...]
    digest: str
    limitations: tuple[str, ...] = (
        "blind 生成器与 detector 同仓同作者：只能保证执行时 ground truth 不可见，无法消除结构性自证优势（RISK_EVAL §1 limitations）",
    )


def _blind_noise(key: str, scale: float) -> float:
    value = int(hashlib.sha256(f"blind:{key}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return scale * (value - 0.5) * 2


def generate_blind_snapshot() -> BlindSnapshot:
    """确定性 blind 快照：PA 趋势恶化、PB 现金流背离、PC 干净对照。

    植入语义与冻结资产生成器一致（窗口内线性爬升、窗口后保持）。
    """
    revenue_rows: list[dict[str, Any]] = []
    cashflow_rows: list[dict[str, Any]] = []
    trend_start = MONTH_CALENDAR.index(_BLIND_TREND_WINDOW[0])
    trend_end = MONTH_CALENDAR.index(_BLIND_TREND_WINDOW[1])
    ratio_start = MONTH_CALENDAR.index(_BLIND_RATIO_WINDOW[0])
    ratio_end = MONTH_CALENDAR.index(_BLIND_RATIO_WINDOW[1])
    for i, month in enumerate(MONTH_CALENDAR):
        for product, base_margin, base_revenue in (
            ("PA", 0.55, 100.0),
            ("PB", 0.50, 80.0),
            ("PC", 0.45, 60.0),
        ):
            ramp = 0.0
            if product == "PA" and i >= trend_start:
                ramp = 1.0 if i >= trend_end else (i - trend_start) / (trend_end - trend_start)
            margin = round(base_margin - 0.09 * ramp + _blind_noise(f"margin:{product}:{i}", 0.012), 4)
            revenue = round(base_revenue * (1 + _blind_noise(f"rev:{product}:{i}", 0.02)), 2)
            revenue_rows.append(
                {
                    "month": month,
                    "product_line": product,
                    "region": "R1",
                    "revenue_k": revenue,
                    "gross_margin_rate": margin,
                }
            )
            ratio = 0.31
            if product == "PB" and i >= ratio_start:
                ramp = 1.0 if i >= ratio_end else (i - ratio_start) / (ratio_end - ratio_start)
                ratio = 0.31 - 0.07 * ramp
            ratio = round(ratio + _blind_noise(f"ratio:{product}:{i}", 0.008), 4)
            cash = round(revenue * ratio, 2)
            cashflow_rows.append(
                {
                    "month": month,
                    "product_line": product,
                    "revenue_k": revenue,
                    "operating_cash_k": cash,
                    "cash_to_revenue": ratio,
                }
            )
    plants = (
        {
            "id": "blind-trend-PA",
            "kind": "trend",
            "entity": {"dim": "product_line", "value": "PA"},
            "metric": "gross_margin_rate",
            "window": {"start": _BLIND_TREND_WINDOW[0], "end": _BLIND_TREND_WINDOW[1]},
        },
        {
            "id": "blind-ratio-PB",
            "kind": "ratio",
            "entity": {"dim": "product_line", "value": "PB"},
            "metric": "cash_to_revenue",
            "window": {"start": _BLIND_RATIO_WINDOW[0], "end": _BLIND_RATIO_WINDOW[1]},
        },
    )
    payload = canonical_json(
        {
            "revenue": revenue_rows,
            "cashflow": cashflow_rows,
            "plants": list(plants),
            "generator": "discover-blind-v1",
        }
    )
    return BlindSnapshot(
        revenue_rows=revenue_rows,
        cashflow_rows=cashflow_rows,
        plants=plants,
        digest=digest_bytes(payload),
    )


def _blind_units() -> list[RegisteredUnit]:
    return [
        RegisteredUnit(sample_id="blind:trend:PA", unit_id="trend:PA"),
        RegisteredUnit(sample_id="blind:ratio:PB", unit_id="ratio:PB"),
        RegisteredUnit(sample_id="blind:clean:PC", unit_id="clean:PC"),
        RegisteredUnit(sample_id="blind:refutation", unit_id="falsification:refutable"),
        RegisteredUnit(sample_id="blind:coverage", unit_id="falsification:coverage"),
    ]


def _run_blind_pass(snapshot: BlindSnapshot, evaluated_at: datetime) -> tuple[DiscoveryPassResult, SnapshotMetrics]:
    metrics = SnapshotMetrics.from_tables(snapshot.revenue_rows, [], [], snapshot.cashflow_rows)
    return run_discovery_pass(metrics, min_probes_required=2, evaluated_at=evaluated_at), metrics
