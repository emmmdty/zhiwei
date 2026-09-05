"""Sealed eval artifact → 质量报告载荷（S9 §8 公开表的最小内核）。

报告只从已复核的 sealed artifact 构建：调用方必须同时提供与密封样本逐一对应的
outcome 原始 result，报告构建期对每个 result 重算 digest 并与密封值比对——任何
漂移、缺失、多余或状态不一致都拒绝出报告。这样统计数字不可能脱离密封证据单独
生成，模板变量只能由 sealed artifact 填充的 S9 约束在此落地。

scope 标签（mode/model/version/date/corpus/environment）全部显式：mode 取自密封
载荷本身（不可声明），其余由调用方显式传入——不允许从环境变量或系统时间猜测，
保证同一输入的报告 digest 逐字节可复现。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date as date_type
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from zhiwei.contracts.canonical import digest
from zhiwei.evals.domain import SampleOutcome
from zhiwei.evals.sealing import SealedEvalArtifact
from zhiwei.evals.statistics import (
    holm_correction,
    mcnemar_exact_two_sided,
    success_rate_from_outcomes,
    terminal_denominator,
)

EVAL_REPORT_SCHEMA_ID = "eval.report"
EVAL_REPORT_SCHEMA_VERSION = 1

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

_SUCCESS_RATE_LABEL = "success_rate"


class EvalReportRefused(RuntimeError):
    """报告输入与密封证据不一致（digest/状态/覆盖面）或绑定信息缺失时拒绝。"""


class EvalReportScopeInput(BaseModel):
    """调用方显式提供的 scope 标签；mode 不在其中——mode 只能来自密封载荷。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    version: str
    date: str
    corpus: str
    environment: str

    @field_validator("model", "version", "corpus", "environment")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("scope label must be non-empty")
        return value

    @field_validator("date")
    @classmethod
    def _require_iso_date(cls, value: str) -> str:
        try:
            date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("scope date must be an ISO-8601 date (YYYY-MM-DD)") from exc
        return value


class EvalReportScope(BaseModel):
    """报告载荷中的完整 scope；mode 是从密封载荷复制的不可声明标签。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: str
    model: str
    version: str
    date: str
    corpus: str
    environment: str


class EvalReportDenominator(BaseModel):
    """完整失败分母的逐状态计数；refused/error/failed 全部留在分母内。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_total: int
    n_completed: int
    n_failed: int
    n_refused: int
    n_error: int


class EvalReportQualityEntry(BaseModel):
    """质量表行：比例估计 + Wilson 区间 + 完整分母。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    n: int
    successes: int
    estimate: float
    ci_low: float
    ci_high: float
    denominator: EvalReportDenominator


class EvalReportProvenance(BaseModel):
    """报告与密封证据的绑定：seal digest 与全部密封 digest 一并引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    eval_run_id: UUID
    seal_digest: str
    mode: str
    migration_revision: str
    code_digest: str
    config_digest: str
    schema_digest: str
    dataset_digest: str
    dataset_manifest_id: UUID
    test_report_digest: str
    test_report_manifest_id: UUID


class EvalReportComparisonItem(BaseModel):
    """单个配对比较：McNemar 精确 p 值与该家族内的 Holm 校正值。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    discordant_first: int
    discordant_second: int
    p_value: float
    p_value_holm: float


class EvalReportPairedComparison(BaseModel):
    """配对比较块：方法与多重性校正口径随数据一起封存。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str
    multiplicity: str
    comparisons: tuple[EvalReportComparisonItem, ...]


class PairedComparison(BaseModel):
    """比较输入：两个方向的 discordant 计数（first vs second 的配对差异）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    first: int
    second: int


class EvalReportArtifact(BaseModel):
    """`eval.report` 密封报告载荷；canonical_mapping 是其唯一序列化入口。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str
    schema_version: int
    generated_from: EvalReportProvenance
    scope: EvalReportScope
    quality: tuple[EvalReportQualityEntry, ...]
    paired_comparison: EvalReportPairedComparison | None = None

    def canonical_mapping(self) -> dict[str, Any]:
        """返回完整 canonical 载荷；报告 digest 即对该 mapping 求值。"""
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "generated_from": {
                "run_id": str(self.generated_from.run_id),
                "eval_run_id": str(self.generated_from.eval_run_id),
                "seal_digest": self.generated_from.seal_digest,
                "mode": self.generated_from.mode,
                "migration_revision": self.generated_from.migration_revision,
                "code_digest": self.generated_from.code_digest,
                "config_digest": self.generated_from.config_digest,
                "schema_digest": self.generated_from.schema_digest,
                "dataset_digest": self.generated_from.dataset_digest,
                "dataset_manifest_id": str(self.generated_from.dataset_manifest_id),
                "test_report_digest": self.generated_from.test_report_digest,
                "test_report_manifest_id": str(self.generated_from.test_report_manifest_id),
            },
            "scope": {
                "mode": self.scope.mode,
                "model": self.scope.model,
                "version": self.scope.version,
                "date": self.scope.date,
                "corpus": self.scope.corpus,
                "environment": self.scope.environment,
            },
            "quality": [
                {
                    "label": entry.label,
                    "n": entry.n,
                    "successes": entry.successes,
                    "estimate": entry.estimate,
                    "ci_low": entry.ci_low,
                    "ci_high": entry.ci_high,
                    "denominator": {
                        "n_total": entry.denominator.n_total,
                        "n_completed": entry.denominator.n_completed,
                        "n_failed": entry.denominator.n_failed,
                        "n_refused": entry.denominator.n_refused,
                        "n_error": entry.denominator.n_error,
                    },
                }
                for entry in self.quality
            ],
            "paired_comparison": (
                {
                    "method": self.paired_comparison.method,
                    "multiplicity": self.paired_comparison.multiplicity,
                    "comparisons": [
                        {
                            "label": item.label,
                            "discordant_first": item.discordant_first,
                            "discordant_second": item.discordant_second,
                            "p_value": item.p_value,
                            "p_value_holm": item.p_value_holm,
                        }
                        for item in self.paired_comparison.comparisons
                    ],
                }
                if self.paired_comparison is not None
                else None
            ),
        }


def build_eval_report(
    artifact: SealedEvalArtifact,
    outcomes: Sequence[SampleOutcome],
    *,
    seal_digest: str,
    scope: EvalReportScopeInput,
    comparisons: Sequence[PairedComparison] | None = None,
    success_field: str = "passed",
) -> tuple[EvalReportArtifact, str]:
    """从 verified sealed artifact 与原始 outcomes 构建报告，返回 (载荷, digest)。

    outcomes 与密封样本必须一一对应且 status/result_digest 逐项复算一致；
    不一致即 EvalReportRefused——报告不允许引用密封之外的任何证据。
    """
    if _DIGEST_PATTERN.fullmatch(seal_digest) is None:
        raise EvalReportRefused("seal digest must be a lowercase SHA-256 reference")
    _verify_outcomes_match_seal(artifact, outcomes)

    breakdown = terminal_denominator(outcomes)
    rate = success_rate_from_outcomes(outcomes, success_field)
    quality = EvalReportQualityEntry(
        label=_SUCCESS_RATE_LABEL,
        n=rate.n,
        successes=rate.successes,
        estimate=rate.estimate,
        ci_low=rate.ci_low,
        ci_high=rate.ci_high,
        denominator=EvalReportDenominator(
            n_total=breakdown.n_total,
            n_completed=breakdown.n_completed,
            n_failed=breakdown.n_failed,
            n_refused=breakdown.n_refused,
            n_error=breakdown.n_error,
        ),
    )

    paired = _build_paired_comparison(comparisons) if comparisons else None

    report = EvalReportArtifact(
        schema_id=EVAL_REPORT_SCHEMA_ID,
        schema_version=EVAL_REPORT_SCHEMA_VERSION,
        generated_from=EvalReportProvenance(
            run_id=artifact.run_id,
            eval_run_id=artifact.eval_run_id,
            seal_digest=seal_digest,
            mode=artifact.mode,
            migration_revision=artifact.migration_revision,
            code_digest=artifact.code_digest,
            config_digest=artifact.config_digest,
            schema_digest=artifact.schema_digest,
            dataset_digest=artifact.dataset_digest,
            dataset_manifest_id=artifact.dataset_manifest_id,
            test_report_digest=artifact.test_report_digest,
            test_report_manifest_id=artifact.test_report_manifest_id,
        ),
        scope=EvalReportScope(
            mode=artifact.mode,
            model=scope.model,
            version=scope.version,
            date=scope.date,
            corpus=scope.corpus,
            environment=scope.environment,
        ),
        quality=(quality,),
        paired_comparison=paired,
    )
    return report, digest(report.canonical_mapping())


def _verify_outcomes_match_seal(
    artifact: SealedEvalArtifact, outcomes: Sequence[SampleOutcome]
) -> None:
    """密封样本与原始 outcome 逐项比对：覆盖面、status、result digest 全等。"""
    sealed = {
        (sample.sample_id, sample.unit_id): (sample.status, sample.result_digest)
        for sample in artifact.samples
    }
    provided: dict[tuple[str, str], SampleOutcome] = {}
    for outcome in outcomes:
        key = (outcome.unit.sample_id, outcome.unit.unit_id)
        if key in provided:
            raise EvalReportRefused(f"duplicate outcome for sealed unit {key!r}")
        provided[key] = outcome
    sealed_keys = {(sample.sample_id, sample.unit_id) for sample in artifact.samples}
    missing = sorted(sealed_keys - set(provided))
    if missing:
        raise EvalReportRefused(f"outcomes do not cover sealed units: {missing}")
    extra = sorted(set(provided) - sealed_keys)
    if extra:
        raise EvalReportRefused(f"outcomes include units outside the seal: {extra}")
    for key, outcome in provided.items():
        status, result_digest = sealed[key]
        if outcome.status is not status:
            raise EvalReportRefused(
                f"status mismatch for sealed unit {key!r}: "
                f"sealed {status.value!r}, provided {outcome.status.value!r}"
            )
        if outcome.result_digest != result_digest:
            raise EvalReportRefused(
                f"result digest mismatch for sealed unit {key!r}: "
                "provided result does not match the sealed evidence"
            )


def _build_paired_comparison(
    comparisons: Sequence[PairedComparison],
) -> EvalReportPairedComparison:
    """McNemar 精确检验 + Holm 家族校正；校正结果按输入原始顺序回填。"""
    p_values = [
        mcnemar_exact_two_sided(comparison.first, comparison.second)
        for comparison in comparisons
    ]
    adjusted = holm_correction(p_values)
    items = tuple(
        EvalReportComparisonItem(
            label=comparison.label,
            discordant_first=comparison.first,
            discordant_second=comparison.second,
            p_value=p_value,
            p_value_holm=holm_value,
        )
        for comparison, p_value, holm_value in zip(
            comparisons, p_values, adjusted, strict=True
        )
    )
    return EvalReportPairedComparison(
        method="mcnemar_exact_two_sided",
        multiplicity="holm",
        comparisons=items,
    )
