"""S9 冻结契约：ADR-002 token ROI 指标进入 sealed eval artifact（A 档，S9-T6/T2）。

七项 ROI 指标按 Run/trajectory 归集并写入密封载荷；指标参与 canonical digest（改动任一指标
即改变 seal digest）；旧版（无 usage 块）载荷必须保持可验证（向后兼容）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.sealing import build_sealed_artifact, verify_sealed_artifact
from zhiwei.models.usage import RunUsageSnapshot, TokenWeights

USAGE_METRIC_KEYS = frozenset(
    {
        "weighted_tokens",
        "authoritative_token_share",
        "evidence_per_kilotoken",
        "recoverable_reload_waste",
        "context_utilization",
        "compression_ratio",
        "cost_per_completed_task",
    }
)


@dataclass(frozen=True)
class _SealableState:
    """build_sealed_artifact 协议的最小实现（测试桩）。"""

    mode: EvalMode
    registered_units: tuple[RegisteredUnit, ...]
    outcomes: tuple[SampleOutcome, ...]
    code_digest: str
    config_digest: str
    schema_digest: str


def _state(mode: EvalMode = EvalMode.OFFLINE) -> _SealableState:
    units = (RegisteredUnit(sample_id="s-1", unit_id="u-1"),)
    outcomes = (
        SampleOutcome(
            unit=units[0], status=SampleStatus.COMPLETED, result={"passed": True}
        ),
    )
    return _SealableState(
        mode=mode,
        registered_units=units,
        outcomes=outcomes,
        code_digest="sha256:" + "0" * 64,
        config_digest="sha256:" + "1" * 64,
        schema_digest="sha256:" + "2" * 64,
    )


def _usage(output_tokens: int = 1000) -> RunUsageSnapshot:
    return RunUsageSnapshot(
        total_new_input_tokens=500,
        total_cache_read_tokens=200,
        total_output_tokens=output_tokens,
        authoritative_tokens_sent=300,
        total_tokens_sent=700,
        verified_evidence_count=2,
        recoverable_reload_tokens=50,
        context_window=8000,
        compression_input_tokens=400,
        compression_output_tokens=100,
        completed_task_count=1,
        weights=TokenWeights(),
    )


def _build(usage: RunUsageSnapshot | None = None) -> tuple[dict[str, Any], str]:
    _artifact, seal_digest = build_sealed_artifact(
        run_id=uuid4(),
        eval_run_id=uuid4(),
        state=_state(),
        dataset_digest="sha256:" + "3" * 64,
        dataset_manifest_id=uuid4(),
        suite_digest="sha256:" + "4" * 64,
        migration_revision="0013_evals_campaigns",
        test_report_digest="sha256:" + "5" * 64,
        test_report_manifest_id=uuid4(),
        usage=usage,
    )
    return _artifact.canonical_mapping(), seal_digest


class TestUsageMetricsInSealedArtifact:
    def test_seven_roi_metrics_present(self) -> None:
        payload, _ = _build(usage=_usage())
        metrics = payload.get("usage_metrics")
        assert isinstance(metrics, dict)
        assert USAGE_METRIC_KEYS.issubset(metrics.keys())

    def test_metric_values_match_computation(self) -> None:
        payload, _ = _build(usage=_usage())
        metrics = payload["usage_metrics"]
        # 1.0*500 + 0.1*200 + 4.0*1000 = 4520（ADR-002 权重：1.0/0.1/4.0）
        assert metrics["weighted_tokens"] == 4520.0
        # 300/700
        assert metrics["authoritative_token_share"] == 300 / 700

    def test_metrics_participate_in_digest(self) -> None:
        _, baseline = _build(usage=_usage(output_tokens=1000))
        _, mutated = _build(usage=_usage(output_tokens=2000))
        assert baseline != mutated

    def test_verify_roundtrip_preserves_metrics(self) -> None:
        payload, seal_digest = _build(usage=_usage())
        artifact = verify_sealed_artifact(payload, seal_digest)
        metrics = artifact.usage_metrics
        assert metrics is not None
        assert USAGE_METRIC_KEYS.issubset(metrics.keys())

    def test_legacy_payload_without_usage_still_verifies(self) -> None:
        payload, seal_digest = _build(usage=None)
        assert "usage_metrics" not in payload
        artifact = verify_sealed_artifact(payload, seal_digest)
        assert artifact.usage_metrics is None

    def test_tampered_metric_breaks_verification(self) -> None:
        from zhiwei.evals.sealing import SealVerificationError

        payload, seal_digest = _build(usage=_usage())
        assert isinstance(payload["usage_metrics"], dict)
        tampered = dict(payload)
        metrics = dict(payload["usage_metrics"])
        metrics["weighted_tokens"] = 1.0
        tampered["usage_metrics"] = metrics
        try:
            verify_sealed_artifact(tampered, seal_digest)
        except SealVerificationError:
            return
        raise AssertionError("tampered usage_metrics must break seal verification")
