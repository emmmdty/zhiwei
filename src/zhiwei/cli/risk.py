"""`zhiwei risk` 命令组（specs/s8 §7/§8）。

suite 注册表、冻结资产加载、生产 detector 路径与 executor 在 zhiwei.evals 层
（risk_suites / executors.risk）——本模块只保留 `risk generate` 命令与 --check 的
D0-D6 口径逻辑（docs/RISK_EVAL.md §2）。

fail closed：未知 suite / 资产 digest 不符 → 非零退出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import typer

from zhiwei.contracts.canonical import canonical_json
from zhiwei.evals.risk_suites import (
    NUMERIC_RISK_V1,
    AssetDigestError,
    DiscoveryPassResult,
    load_risk_assets,
    resolve_risk_suite,
    run_discovery_pass,
    score_against_manifest,
)
from zhiwei.evidence.patterns.numeric import DETECTOR_VERSION

app = typer.Typer(
    help="Numeric Risk Detector Pack：从冻结合成数据经生产 detector 路径生成发现",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


# --check 的一致性判据（specs/s8 §8：一致性不足 exit 非零）。
# 门槛按冻结单 seed 资产上的真实可达口径设定：easy/medium 全检出、hard 档允许
# 结构性漏检（跨维度泄漏、亚噪声效应），distractor 误报率受限于半数。
CHECK_CRITERIA: dict[str, float] = {
    "min_recall_overall": 0.7,
    "min_recall_easy": 1.0,
    "min_recall_medium": 0.5,
    "max_distractor_fp_rate": 0.5,
    "min_evidence_validity": 1.0,
}


def evaluate_check_criteria(score: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if score["recall"]["overall"] < CHECK_CRITERIA["min_recall_overall"]:
        failures.append(
            f"recall_overall {score['recall']['overall']:.3f} < {CHECK_CRITERIA['min_recall_overall']}"
        )
    difficulty = score["recall"]["by_declared_difficulty"]
    if (difficulty.get("easy") or 0.0) < CHECK_CRITERIA["min_recall_easy"]:
        failures.append(f"recall_easy {(difficulty.get('easy') or 0.0):.3f} below gate")
    if (difficulty.get("medium") or 0.0) < CHECK_CRITERIA["min_recall_medium"]:
        failures.append(f"recall_medium {(difficulty.get('medium') or 0.0):.3f} below gate")
    if score["distractor_fp_rate"] > CHECK_CRITERIA["max_distractor_fp_rate"]:
        failures.append(
            f"distractor_fp_rate {score['distractor_fp_rate']:.3f} > "
            f"{CHECK_CRITERIA['max_distractor_fp_rate']}"
        )
    if score["evidence_validity"] < CHECK_CRITERIA["min_evidence_validity"]:
        failures.append(f"evidence_validity {score['evidence_validity']:.3f} < 1.0")
    return failures


def build_layer_summary(pass_result: DiscoveryPassResult, score: dict[str, Any]) -> dict[str, Any]:
    """D0-D6 分层口径摘要（docs/RISK_EVAL.md §2）。D5/D6 不在离线口径内主张。"""
    return {
        "D0": {
            "layer": "contract",
            "detector_version": DETECTOR_VERSION,
            "pass_digest": pass_result.pass_digest,
            "schema": "PatternFinding/Signal/RiskHypothesis v1",
        },
        "D1": {
            "layer": "data_quality",
            "tables": pass_result.quality_report["tables"],
            "note": "missing/duplicate 由冻结资产生成器注入，逐表如实报告",
        },
        "D2": {
            "layer": "detector",
            "recall": score["recall"],
            "precision": score["precision"],
            "distractor_fp_rate": score["distractor_fp_rate"],
            "evidence_validity": score["evidence_validity"],
            "missed": score["missed"],
            "distractor_fps": score["distractor_fps"],
        },
        "D3": {
            "layer": "discovery",
            "falsification_coverage": pass_result.falsification.falsification_coverage,
            "hypothesis_refutation_rate": pass_result.falsification.hypothesis_refutation_rate,
            "dedupe_new": pass_result.dedupe_new,
            "dedupe_duplicate": pass_result.dedupe_duplicate,
            "merge_candidates": pass_result.merge_candidates,
        },
        "D4": {
            "layer": "workflow",
            "status": "covered_by_tests",
            "scope": "tests/contract/discover + tests/integration/discover",
        },
        "D5": {
            "layer": "utility",
            "status": "not_evaluated_offline",
            "reason": "human 盲评相关性/actionability/误报负担需要 live 人评（RISK_EVAL §8）",
        },
        "D6": {
            "layer": "operations",
            "status": "not_evaluated_offline",
            "reason": "schedule/webhook 故障与负载报告不在离线 suite 口径内",
        },
    }


@dataclass
class _GeneratePayload:
    suite: str
    asset_digest: str
    counts: dict[str, int]
    falsification: dict[str, float | int]
    check: bool = False
    layers: dict[str, Any] = field(default_factory=dict)
    score: dict[str, Any] = field(default_factory=dict)
    passed: bool = True
    criteria_failures: list[str] = field(default_factory=list)


def _generate_payload(suite: str, check: bool) -> _GeneratePayload:
    resolve_risk_suite(suite)  # 未知 suite 在此 fail closed
    if suite != NUMERIC_RISK_V1:
        raise LookupError(
            f"--check 的 ground truth 比对只对有冻结植入清单的 suite 定义（{suite} 为代码定义资产）"
        )
    assets = load_risk_assets()
    pass_result = run_discovery_pass(assets, min_probes_required=2, evaluated_at=datetime.now(UTC))
    payload = _GeneratePayload(
        suite=suite,
        asset_digest=assets.digest,
        counts={
            "findings": len(pass_result.findings),
            "signals": len(pass_result.signals),
            "hypotheses": len(pass_result.hypotheses),
            "dedupe_duplicates": pass_result.dedupe_duplicate,
            "merge_candidates": pass_result.merge_candidates,
        },
        falsification={
            "probes_executed": pass_result.falsification.probes_executed,
            "falsification_coverage": pass_result.falsification.falsification_coverage,
            "hypothesis_refutation_rate": pass_result.falsification.hypothesis_refutation_rate,
        },
    )
    if check:
        score = score_against_manifest(pass_result.findings, assets.manifest)
        payload.check = True
        payload.layers = build_layer_summary(pass_result, score)
        payload.score = score
        payload.criteria_failures = evaluate_check_criteria(score)
        payload.passed = not payload.criteria_failures
    return payload


@app.command("generate")
def generate(
    suite: str = typer.Option(..., "--suite", help="risk suite 名称"),
    check: bool = typer.Option(False, "--check", help="与冻结植入清单比对并输出 D0-D6 摘要"),
) -> None:
    """经生产 detector 路径从冻结合成数据生成 Signal/RiskHypothesis。

    fail closed：未知 suite / 资产 digest 不符 → 非零退出。
    """
    import click

    try:
        payload = _generate_payload(suite, check)
    except LookupError as exc:
        click.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from None
    except AssetDigestError as exc:
        click.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from None
    click.echo(canonical_json(payload.__dict__).decode())
    if check and not payload.passed:
        raise typer.Exit(1)
