"""S9-T2 RED: sealed artifact → 质量报告载荷（纯路径，无 DB）。

报告只能从 verified sealed artifact 构建：outcome 的 result digest 必须与密封样本
逐一复算匹配，任何漂移拒绝出报告；scope 标签（mode/model/version/date/corpus/
environment）全部显式，不允许从环境猜测。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from zhiwei.contracts.canonical import digest
from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.reports import (
    EVAL_REPORT_SCHEMA_ID,
    EVAL_REPORT_SCHEMA_VERSION,
    EvalReportArtifact,
    EvalReportRefused,
    EvalReportScopeInput,
    PairedComparison,
    build_eval_report,
)
from zhiwei.evals.runs import _eval_schema_registry
from zhiwei.evals.sealing import build_sealed_artifact

RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
EVAL_RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
# manifest id 固定取值：digest 稳定性断言要求两次构建的输入完全一致。
DATASET_MANIFEST_ID = UUID("55555555-5555-4555-8555-555555555555")
TEST_REPORT_MANIFEST_ID = UUID("66666666-6666-4666-8666-666666666666")


def _unit(sample_id: str, unit_id: str = "u-1") -> RegisteredUnit:
    return RegisteredUnit(sample_id=sample_id, unit_id=unit_id)


def _outcome(sample_id: str, status: SampleStatus, result: dict[str, Any]) -> SampleOutcome:
    return SampleOutcome(unit=_unit(sample_id), status=status, result=result)


def _state_outcomes() -> list[SampleOutcome]:
    return [
        _outcome("s-1", SampleStatus.COMPLETED, {"answer": "42", "passed": True}),
        _outcome("s-2", SampleStatus.COMPLETED, {"answer": "41", "passed": False}),
        _outcome("s-3", SampleStatus.REFUSED, {"reason": "policy_refusal"}),
        _outcome("s-4", SampleStatus.ERROR, {"reason": "provider_timeout"}),
    ]


def _scope() -> EvalReportScopeInput:
    return EvalReportScopeInput(
        model="internal-llm",
        version="agent-2026.09",
        date="2026-09-05",
        corpus="internal-120",
        environment="offline",
    )


def _seal_inputs(
    outcomes: list[SampleOutcome],
) -> dict[str, Any]:
    """build_sealed_artifact 的固定输入：同一 registry 两次构建必须逐字节一致。"""
    return {
        "run_id": RUN_ID,
        "eval_run_id": EVAL_RUN_ID,
        "state": _StubState(tuple(outcomes)),
        "dataset_digest": "sha256:" + "3" * 64,
        "dataset_manifest_id": DATASET_MANIFEST_ID,
        "suite_digest": "sha256:" + "4" * 64,
        "migration_revision": "0014_cost_ledger",
        "test_report_digest": "sha256:" + "5" * 64,
        "test_report_manifest_id": TEST_REPORT_MANIFEST_ID,
    }


def _seal(
    outcomes: list[SampleOutcome],
) -> tuple[EvalReportArtifact, str]:
    artifact, seal_digest = build_sealed_artifact(**_seal_inputs(outcomes))
    report, report_digest = build_eval_report(
        artifact,
        outcomes,
        seal_digest=seal_digest,
        scope=_scope(),
    )
    return report, report_digest


def _sealed_artifact(
    outcomes: list[SampleOutcome],
) -> tuple[Any, str]:
    """构造 (sealed artifact, seal_digest) 供报告构建与拒绝路径复用同一密封输入。"""
    return build_sealed_artifact(**_seal_inputs(outcomes))


class _StubState:
    """build_sealed_artifact 协议的最小实现。"""

    def __init__(self, outcomes: tuple[SampleOutcome, ...]) -> None:
        self.mode = EvalMode.OFFLINE
        self.registered_units = tuple(outcome.unit for outcome in outcomes)
        self.outcomes = outcomes
        self.code_digest = "sha256:" + "0" * 64
        self.config_digest = "sha256:" + "1" * 64
        self.schema_digest = "sha256:" + "2" * 64


class TestReportPayload:
    def test_scope_labels_are_carried_from_artifact_and_input(self) -> None:
        report, _ = _seal(_state_outcomes())
        assert report.scope.mode == EvalMode.OFFLINE.value
        assert report.scope.model == "internal-llm"
        assert report.scope.version == "agent-2026.09"
        assert report.scope.date == "2026-09-05"
        assert report.scope.corpus == "internal-120"
        assert report.scope.environment == "offline"

    def test_full_failure_denominator_is_reported(self) -> None:
        report, _ = _seal(_state_outcomes())
        entry = report.quality[0]
        assert entry.label == "success_rate"
        # refused/error 全部留在分母：4 个 terminal 单位，1 个成功。
        assert entry.n == 4
        assert entry.successes == 1
        assert entry.estimate == pytest.approx(0.25)
        assert entry.ci_low < entry.estimate < entry.ci_high
        assert entry.denominator.n_total == 4
        assert entry.denominator.n_completed == 2
        assert entry.denominator.n_failed == 0
        assert entry.denominator.n_refused == 1
        assert entry.denominator.n_error == 1

    def test_report_is_bound_to_the_sealed_artifact(self) -> None:
        outcomes = _state_outcomes()
        artifact, seal_digest = _sealed_artifact(outcomes)
        report, report_digest = build_eval_report(
            artifact, outcomes, seal_digest=seal_digest, scope=_scope()
        )
        assert report.schema_id == EVAL_REPORT_SCHEMA_ID
        assert report.schema_version == EVAL_REPORT_SCHEMA_VERSION
        assert report.generated_from.eval_run_id == EVAL_RUN_ID
        assert report.generated_from.run_id == RUN_ID
        assert report.generated_from.seal_digest == seal_digest
        assert report.generated_from.migration_revision == "0014_cost_ledger"
        assert report.generated_from.mode == EvalMode.OFFLINE.value
        # canonical mapping 是报告 digest 的唯一来源，且必须可复算。
        assert digest(report.canonical_mapping()) == report_digest

    def test_report_digest_is_stable_for_identical_inputs(self) -> None:
        _, first_digest = _seal(_state_outcomes())
        _, second_digest = _seal(_state_outcomes())
        assert first_digest == second_digest

    def test_paired_comparison_with_holm_in_original_order(self) -> None:
        outcomes = _state_outcomes()
        artifact, seal_digest = _sealed_artifact(outcomes)
        report, _ = build_eval_report(
            artifact,
            outcomes,
            seal_digest=seal_digest,
            scope=_scope(),
            comparisons=[
                PairedComparison(label="vs-baseline", first=2, second=10),
                PairedComparison(label="vs-ablation", first=0, second=0),
            ],
        )
        block = report.paired_comparison
        assert block is not None
        assert [item.label for item in block.comparisons] == ["vs-baseline", "vs-ablation"]
        assert block.comparisons[0].p_value == pytest.approx(2 * 79 / 4096)
        assert block.comparisons[1].p_value == pytest.approx(1.0)
        # Holm 家族校正按原始顺序回填，单调不降。
        assert block.comparisons[1].p_value_holm >= block.comparisons[0].p_value_holm

    def test_report_schema_is_registered(self) -> None:
        registry = _eval_schema_registry()
        assert registry.resolve(
            EVAL_REPORT_SCHEMA_ID, EVAL_REPORT_SCHEMA_VERSION
        ) is EvalReportArtifact


class TestReportRefusals:
    def test_tampered_result_digest_refused(self) -> None:
        outcomes = _state_outcomes()
        artifact, seal_digest = _sealed_artifact(outcomes)
        forged = list(outcomes)
        forged[0] = SampleOutcome(
            unit=outcomes[0].unit,
            status=outcomes[0].status,
            result={"answer": "43", "passed": True},
        )
        with pytest.raises(EvalReportRefused):
            build_eval_report(artifact, forged, seal_digest=seal_digest, scope=_scope())

    def test_missing_and_extra_outcomes_refused(self) -> None:
        outcomes = _state_outcomes()
        artifact, seal_digest = _sealed_artifact(outcomes)
        with pytest.raises(EvalReportRefused):
            build_eval_report(artifact, outcomes[:3], seal_digest=seal_digest, scope=_scope())
        with pytest.raises(EvalReportRefused):
            build_eval_report(
                artifact,
                [*outcomes, _outcome("s-9", SampleStatus.FAILED, {})],
                seal_digest=seal_digest,
                scope=_scope(),
            )

    def test_status_mismatch_refused(self) -> None:
        outcomes = _state_outcomes()
        artifact, seal_digest = _sealed_artifact(outcomes)
        altered = [
            SampleOutcome(unit=outcome.unit, status=SampleStatus.FAILED, result=outcome.result)
            if outcome.unit.sample_id == "s-3"
            else outcome
            for outcome in outcomes
        ]
        with pytest.raises(EvalReportRefused):
            build_eval_report(artifact, altered, seal_digest=seal_digest, scope=_scope())

    def test_malformed_seal_digest_refused(self) -> None:
        outcomes = _state_outcomes()
        artifact, _ = _sealed_artifact(outcomes)
        with pytest.raises(EvalReportRefused):
            build_eval_report(artifact, outcomes, seal_digest="deadbeef", scope=_scope())

    def test_non_iso_date_refused(self) -> None:
        with pytest.raises(ValueError):
            EvalReportScopeInput(
                model="m", version="v", date="not-a-date", corpus="c", environment="e"
            )

    def test_empty_scope_labels_refused(self) -> None:
        with pytest.raises(ValueError):
            EvalReportScopeInput(
                model="", version="v", date="2026-09-05", corpus="c", environment="e"
            )
