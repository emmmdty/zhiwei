"""Sealed eval artifact 的构建与独立复算。

密封载荷把 mode、注册单位 registry、全部 sample 终态、dataset/suite/code/config/schema digest、
migration revision 以及 dataset/test-report 的 manifest id + digest 一并纳入 canonical digest。
验证方不得信任任何调用方传入的 digest 摘要——所有绑定值都从载荷本身复算后比对。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from zhiwei.contracts.canonical import digest
from zhiwei.evals.domain import (
    EvalMode,
    RegisteredUnit,
    SampleOutcome,
    SampleStatus,
    unit_sort_key,
)

SEALED_ARTIFACT_SCHEMA_ID = "eval.sealed-run"
SEALED_ARTIFACT_SCHEMA_VERSION = 1

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class SealVerificationError(RuntimeError):
    """Raised when a sealed artifact cannot be independently recomputed."""


class EvalSealRefused(RuntimeError):
    """Raised when a Run/EvalRun refuses to seal (e.g. registry not terminal)."""


class SampleRecord(BaseModel):
    """密封载荷里的单个 sample 终态；status 与 result_digest 一并封存。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_id: str
    unit_id: str
    status: SampleStatus
    result_digest: str


class _SealableState(Protocol):
    """build_sealed_artifact 所需的 EvalRunState 结构性视图（避免循环依赖）。

    EvalRunState 是 frozen dataclass（属性只读），协议侧用 property 声明匹配。
    """

    @property
    def mode(self) -> EvalMode: ...

    @property
    def registered_units(self) -> tuple[RegisteredUnit, ...]: ...

    @property
    def outcomes(self) -> tuple[SampleOutcome, ...]: ...

    @property
    def code_digest(self) -> str: ...

    @property
    def config_digest(self) -> str: ...

    @property
    def schema_digest(self) -> str: ...


class SealedEvalArtifact(BaseModel):
    """密封载荷的类型化视图；canonical_mapping 是唯一 canonical 序列化入口。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    eval_run_id: UUID
    mode: str
    code_digest: str
    config_digest: str
    schema_digest: str
    migration_revision: str
    dataset_digest: str
    dataset_manifest_id: UUID
    suite_digest: str
    test_report_digest: str
    test_report_manifest_id: UUID
    registered_units: tuple[RegisteredUnit, ...]
    samples: tuple[SampleRecord, ...]

    @field_validator(
        "code_digest",
        "config_digest",
        "schema_digest",
        "dataset_digest",
        "suite_digest",
        "test_report_digest",
    )
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("sealed artifact digest must be lowercase SHA-256")
        return value

    def canonical_mapping(self) -> dict[str, Any]:
        """返回完整 canonical 载荷；digest 即对该 mapping 的 canonical JSON 求值。"""
        return {
            "schema_id": SEALED_ARTIFACT_SCHEMA_ID,
            "schema_version": SEALED_ARTIFACT_SCHEMA_VERSION,
            "run_id": str(self.run_id),
            "eval_run_id": str(self.eval_run_id),
            "mode": self.mode,
            "code_digest": self.code_digest,
            "config_digest": self.config_digest,
            "schema_digest": self.schema_digest,
            "migration_revision": self.migration_revision,
            "dataset_digest": self.dataset_digest,
            "dataset_manifest_id": str(self.dataset_manifest_id),
            "suite_digest": self.suite_digest,
            "test_report_digest": self.test_report_digest,
            "test_report_manifest_id": str(self.test_report_manifest_id),
            "registered_units": [
                {"sample_id": unit.sample_id, "unit_id": unit.unit_id}
                for unit in self.registered_units
            ],
            "samples": [
                {
                    "sample_id": sample.sample_id,
                    "unit_id": sample.unit_id,
                    "status": sample.status.value,
                    "result_digest": sample.result_digest,
                }
                for sample in self.samples
            ],
        }


def build_sealed_artifact(
    *,
    run_id: UUID,
    eval_run_id: UUID,
    state: _SealableState,
    dataset_digest: str,
    dataset_manifest_id: UUID,
    suite_digest: str,
    migration_revision: str,
    test_report_digest: str,
    test_report_manifest_id: UUID,
) -> tuple[SealedEvalArtifact, str]:
    """从 sealed EvalRunState 构建载荷并返回 (artifact, seal_digest)。

    samples 按注册单位排序后封存：同一 registry 的不同执行顺序必须产生逐字节相同的密封载荷。
    """
    outcomes = sorted(state.outcomes, key=lambda outcome: unit_sort_key(outcome.unit))
    artifact = SealedEvalArtifact(
        run_id=run_id,
        eval_run_id=eval_run_id,
        mode=state.mode.value,
        code_digest=state.code_digest,
        config_digest=state.config_digest,
        schema_digest=state.schema_digest,
        migration_revision=migration_revision,
        dataset_digest=dataset_digest,
        dataset_manifest_id=dataset_manifest_id,
        suite_digest=suite_digest,
        test_report_digest=test_report_digest,
        test_report_manifest_id=test_report_manifest_id,
        registered_units=state.registered_units,
        samples=tuple(
            SampleRecord(
                sample_id=outcome.unit.sample_id,
                unit_id=outcome.unit.unit_id,
                status=outcome.status,
                result_digest=outcome.result_digest,
            )
            for outcome in outcomes
        ),
    )
    return artifact, digest(artifact.canonical_mapping())


def verify_sealed_artifact(
    payload: Mapping[str, object], expected_digest: str
) -> SealedEvalArtifact:
    """独立复算密封载荷：schema 未知、digest 不符一律拒绝。"""
    if not isinstance(payload, Mapping):
        raise SealVerificationError("sealed artifact payload must be a mapping")
    schema_id = payload.get("schema_id")
    schema_version = payload.get("schema_version")
    if (
        schema_id != SEALED_ARTIFACT_SCHEMA_ID
        or schema_version != SEALED_ARTIFACT_SCHEMA_VERSION
    ):
        raise SealVerificationError(
            f"unknown sealed artifact schema: {schema_id!r}@{schema_version!r}"
        )
    recomputed = digest(dict(payload))
    if recomputed != expected_digest:
        raise SealVerificationError(
            f"seal digest mismatch: expected {expected_digest}, recomputed {recomputed}"
        )
    try:
        return SealedEvalArtifact.model_validate(
            {
                "run_id": payload["run_id"],
                "eval_run_id": payload["eval_run_id"],
                "mode": payload["mode"],
                "code_digest": payload["code_digest"],
                "config_digest": payload["config_digest"],
                "schema_digest": payload["schema_digest"],
                "migration_revision": payload["migration_revision"],
                "dataset_digest": payload["dataset_digest"],
                "dataset_manifest_id": payload["dataset_manifest_id"],
                "suite_digest": payload["suite_digest"],
                "test_report_digest": payload["test_report_digest"],
                "test_report_manifest_id": payload["test_report_manifest_id"],
                "registered_units": payload["registered_units"],
                "samples": payload["samples"],
            }
        )
    except (KeyError, TypeError, ValidationError, ValueError) as exc:
        raise SealVerificationError(f"sealed artifact payload is malformed: {exc}") from exc
