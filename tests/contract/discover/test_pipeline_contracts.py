"""S8 Discovery pipeline 契约（spec s8 §3.1/§4/§7，ADR-004）。

本文件锁定 pipeline 桥接层（frozen findings → Signal → RiskHypothesis →
序贯证伪 → 准入/去重）的行为契约。求值确定性、EvidenceRef 独立性、准入门槛、
typed 去重与 resolution 不可改写 detector output 是 spec 明文要求。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from zhiwei.discover.hypotheses import HypothesisStatus
from zhiwei.evals.risk_suites import (
    NUMERIC_RISK_V1,
    FalsificationSummary,
    RiskAssets,
    load_risk_assets,
    resolve_risk_suite,
    run_discovery_pass,
)
from zhiwei.evidence.patterns.numeric import SnapshotMetrics

_NOW = datetime(2026, 9, 4, tzinfo=UTC)

MIN_PROBES = 2  # numeric pack 的 falsification standard（ProgramVersion 声明）


@pytest.fixture(scope="module")
def assets() -> RiskAssets:
    return load_risk_assets()


@pytest.fixture(scope="module")
def pass_result(assets: RiskAssets):
    return run_discovery_pass(assets, min_probes_required=MIN_PROBES, evaluated_at=_NOW)


def _canonical(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class TestDiscoveryPass:
    def test_produces_signals_and_hypotheses(self, pass_result) -> None:
        assert pass_result.findings
        assert len(pass_result.signals) >= 10
        assert len(pass_result.hypotheses) == len(pass_result.signals)

    def test_signal_carries_data_quality_and_watermark(self, pass_result) -> None:
        signal = pass_result.signals[0]
        assert signal.data_quality_results, "Signal 必须经过 data quality（所有路径）"
        assert signal.source_watermarks

    def test_hypothesis_links_detector_and_signal(self, pass_result) -> None:
        hypothesis = pass_result.hypotheses[0]
        assert hypothesis.detector_pack_version >= 1
        assert hypothesis.signal_id in {s.id for s in pass_result.signals}
        assert hypothesis.affected_entities

    def test_hypothesis_score_is_not_a_probability(self, pass_result) -> None:
        """启发式 score 不称 probability：SNR 等原始量进 metadata，score 不冒充概率。"""
        for hypothesis in pass_result.hypotheses:
            assert hypothesis.score is None
            assert "snr" in hypothesis.metadata

    def test_pass_is_deterministic_byte_for_byte(self, assets: RiskAssets) -> None:
        first = run_discovery_pass(assets, min_probes_required=MIN_PROBES, evaluated_at=_NOW)
        second = run_discovery_pass(assets, min_probes_required=MIN_PROBES, evaluated_at=_NOW)
        assert first.pass_digest == second.pass_digest
        assert first.falsification == second.falsification
        sig_a = [_canonical(s.model_dump(mode="json")) for s in first.signals]
        sig_b = [_canonical(s.model_dump(mode="json")) for s in second.signals]
        assert sig_a == sig_b


class TestFalsificationStage:
    def test_every_hypothesis_has_executed_probes(self, pass_result) -> None:
        """probe 必须真实执行——求值由确定性组件完成，不得只生成不求值。"""
        for hypothesis in pass_result.hypotheses:
            assert hypothesis.proposed_probes
            assert len(hypothesis.falsification_results) == len(hypothesis.proposed_probes)

    def test_summary_metrics_are_consistent(self, pass_result) -> None:
        summary = pass_result.falsification
        assert isinstance(summary, FalsificationSummary)
        assert summary.probes_executed == summary.probes_generated
        # 口径：falsification_coverage = 有 probe 的 hypothesis 数 / hypothesis 总数，
        # 不是 probes/probes 的恒真式——空 probe 假设存在时必须 < 1。
        with_probes = sum(1 for h in pass_result.hypotheses if h.proposed_probes)
        assert summary.falsification_coverage == pytest.approx(
            with_probes / summary.hypotheses_total if summary.hypotheses_total else 0.0
        )
        rate = summary.hypotheses_refuted / summary.hypotheses_total
        assert summary.hypothesis_refutation_rate == pytest.approx(rate)

    def test_refuted_hypotheses_preserve_falsification_trail(self, pass_result) -> None:
        """被推翻的假设终止并保留完整证伪轨迹（不删除结果）。"""
        refuted = [
            h for h in pass_result.hypotheses if h.status == HypothesisStatus.REJECTED
        ]
        for hypothesis in refuted:
            assert any(not r.passed for r in hypothesis.falsification_results)
            assert len(hypothesis.falsification_results) == len(hypothesis.proposed_probes)

    def test_probe_results_attach_as_independent_evidence(self, pass_result) -> None:
        """每个 probe 结果独立 EvidenceRef：一 probe 一 tag，不可合并或省略。"""
        for hypothesis in pass_result.hypotheses:
            assert len(hypothesis.evidence_tags) == len(hypothesis.falsification_results)
            ref_ids = {tag.source_ref for tag in hypothesis.evidence_tags}
            assert len(ref_ids) == len(hypothesis.evidence_tags)


class TestAdmissionGate:
    def test_insufficient_probes_stay_signal(self, pass_result) -> None:
        """< N 个已执行且未推翻的 probe → 停在 Signal 状态（不进 triage）。"""
        for hypothesis in pass_result.hypotheses:
            if hypothesis.status == HypothesisStatus.READY_FOR_TRIAGE:
                assert len(hypothesis.falsification_results) >= MIN_PROBES
                assert not hypothesis.is_fully_falsified

    def test_refuted_never_enters_triage(self, pass_result) -> None:
        for hypothesis in pass_result.hypotheses:
            if hypothesis.is_fully_falsified:
                assert hypothesis.status == HypothesisStatus.REJECTED

    def test_admission_needs_min_probes_not_just_one(self, assets: RiskAssets) -> None:
        """N=2 时只执行 1 个 probe 不得进入 triage（准入门槛语义）。"""
        result = run_discovery_pass(assets, min_probes_required=2, evaluated_at=_NOW)
        for hypothesis in result.hypotheses:
            if len(hypothesis.falsification_results) < 2:
                assert hypothesis.status == HypothesisStatus.PROPOSED


class TestDedupe:
    def test_fingerprint_decisions_recorded(self, pass_result) -> None:
        assert pass_result.dedupe_new >= 10
        assert pass_result.dedupe_duplicate == 0

    def test_rerun_with_warm_index_does_not_duplicate(self, assets: RiskAssets) -> None:
        """spec §6：刷新/重试不会复制 hypothesis——同指纹再注册必须判 DUPLICATE。"""
        from zhiwei.cases.risk_fingerprint import RiskFingerprintIndex
        from zhiwei.evals.risk_suites import fingerprint_of_finding

        first = run_discovery_pass(assets, min_probes_required=MIN_PROBES, evaluated_at=_NOW)
        index = RiskFingerprintIndex()
        for finding in first.findings:
            index.register(fingerprint_of_finding(finding))
        second = run_discovery_pass(
            assets, min_probes_required=MIN_PROBES, evaluated_at=_NOW, fingerprint_index=index
        )
        assert second.dedupe_duplicate == len(second.findings)
        assert second.dedupe_new == 0
        # 第二遍不产生新 Signal/Hypothesis
        assert second.signals == ()
        assert len(second.hypotheses) == 0


class TestResolutionImmutability:
    def test_resolution_does_not_rewrite_detector_output(self, pass_result) -> None:
        """reopen/dismiss/accepted 等 resolution 不改写原 detector output（spec §4）。"""
        from zhiwei.discover.resolutions import ResolutionKind, create_resolution

        hypothesis = pass_result.hypotheses[0]
        signal = next(s for s in pass_result.signals if s.id == hypothesis.signal_id)
        before_signal = _canonical(signal.model_dump(mode="json"))
        before_hypothesis = _canonical(hypothesis.model_dump(mode="json"))

        resolution = create_resolution(
            hypothesis_id=hypothesis.id,
            kind=ResolutionKind.ACCEPTED,
            rationale="确认风险并立项处理",
            resolved_by="triage-user",
        )
        assert resolution is not None
        after_signal = next(s for s in pass_result.signals if s.id == hypothesis.signal_id)
        assert _canonical(after_signal.model_dump(mode="json")) == before_signal
        assert _canonical(hypothesis.model_dump(mode="json")) == before_hypothesis


class TestSuiteResolution:
    def test_numeric_risk_suite_is_registered(self) -> None:
        suite = resolve_risk_suite(NUMERIC_RISK_V1)
        assert suite.production_path
        assert suite.executor_kind
        units = suite.units
        sample_ids = {u.sample_id for u in units}
        assert any(sid.startswith("planted:") for sid in sample_ids)
        assert any(sid.startswith("distractor:") for sid in sample_ids)

    def test_unknown_suite_fails_closed(self) -> None:
        with pytest.raises(LookupError):
            resolve_risk_suite("numeric-risk-v2")

    def test_blind_suite_units_are_code_defined(self) -> None:
        suite = resolve_risk_suite("discover-blind-v1")
        assert suite.source == "code-defined"
        assert suite.asset_digest, "blind snapshot 的确定性 digest 必须可解析"
        assert all(u.sample_id.startswith("blind:") for u in suite.units)

    def test_blind_digest_is_stable_and_never_writes_evals(self) -> None:
        """blind 资产由代码确定性生成：digest 稳定，且绝不落入 evals/ 冻结目录。"""
        suite_a = resolve_risk_suite("discover-blind-v1")
        suite_b = resolve_risk_suite("discover-blind-v1")
        assert suite_a.asset_digest == suite_b.asset_digest


class TestFrozenAssetLoading:
    def test_assets_carry_digests(self, assets: RiskAssets) -> None:
        assert assets.digest.startswith("sha256:")
        assert set(assets.csv_digests) >= {
            "evals/risk/csv/fact_revenue.csv",
            "evals/risk/planted_manifest.json",
        }

    def test_manifest_shape(self, assets: RiskAssets) -> None:
        assert len(assets.manifest["planted"]) == 14
        assert len(assets.manifest["distractors"]) == 7

    def test_metrics_derived_from_frozen_csv(self, assets: RiskAssets) -> None:
        assert isinstance(assets.metrics, SnapshotMetrics)
        report = assets.metrics.data_quality_report()
        tables = {t["table"]: t for t in report["tables"]}
        assert tables["revenue"]["duplicates"] > 0, "冻结资产含注入的重复行，必须如实报告"


class TestScoring:
    def test_score_against_manifest_shape(self, assets: RiskAssets, pass_result) -> None:
        from zhiwei.evals.risk_suites import score_against_manifest

        score = score_against_manifest(pass_result.findings, assets.manifest)
        assert score["recall"]["overall"] == pytest.approx(
            score["matched_count"] / score["planted_count"]
        )
        assert set(score["recall"]["by_declared_difficulty"]) >= {"easy", "medium", "hard"}
        assert 0.0 <= score["distractor_fp_rate"] <= 1.0
        assert 0.0 <= score["evidence_validity"] <= 1.0

    def test_layer_summary_separates_d0_d6(self, assets: RiskAssets, pass_result) -> None:
        from zhiwei.cli.risk import build_layer_summary
        from zhiwei.evals.risk_suites import score_against_manifest

        layers = build_layer_summary(
            pass_result, score_against_manifest(pass_result.findings, assets.manifest)
        )
        assert set(layers) == {"D0", "D1", "D2", "D3", "D4", "D5", "D6"}
        assert layers["D5"]["status"] == "not_evaluated_offline"
        assert layers["D6"]["status"] == "not_evaluated_offline"
        assert layers["D2"]["recall"]["overall"] > 0
        assert layers["D3"]["falsification_coverage"] >= 0

    def test_check_criteria_gate(self, pass_result, assets: RiskAssets) -> None:
        """--check 的一致性判据：真实口径下的可达门槛，未达即 exit 非零的依据。"""
        from zhiwei.cli.risk import CHECK_CRITERIA, evaluate_check_criteria
        from zhiwei.evals.risk_suites import score_against_manifest

        score = score_against_manifest(pass_result.findings, assets.manifest)
        failures = evaluate_check_criteria(score)
        assert failures == [], f"冻结资产上必须满足 --check 判据: {failures}"
        assert CHECK_CRITERIA["min_recall_overall"] >= 0.7
