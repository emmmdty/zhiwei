"""S0 最小 Eval core：registry 冻结、终态完备、partial/resume/seal 与独立复核。"""

from __future__ import annotations

from zhiwei.evals.datasets import DatasetVersionSpec
from zhiwei.evals.domain import (
    EvalMode,
    RegisteredUnit,
    SampleOutcome,
    SampleStatus,
)
from zhiwei.evals.legacy_assets import LegacyAssetInventory
from zhiwei.evals.runs import (
    CreatedEvalRun,
    CreateEvalRunCommand,
    EvalFoundationService,
    EvalRunNotFound,
    EvalRunState,
    EvalStateError,
    SealedEvalRun,
    SealEmptyCommand,
)
from zhiwei.evals.sealing import (
    EvalSealRefused,
    SealedEvalArtifact,
    SealVerificationError,
    build_sealed_artifact,
    verify_sealed_artifact,
)
from zhiwei.evals.suites import EvalSuiteVersionSpec

__all__ = [
    "CreateEvalRunCommand",
    "CreatedEvalRun",
    "DatasetVersionSpec",
    "EvalFoundationService",
    "EvalMode",
    "EvalRunNotFound",
    "EvalRunState",
    "EvalSealRefused",
    "EvalStateError",
    "EvalSuiteVersionSpec",
    "LegacyAssetInventory",
    "RegisteredUnit",
    "SampleOutcome",
    "SampleStatus",
    "SealEmptyCommand",
    "SealVerificationError",
    "SealedEvalArtifact",
    "SealedEvalRun",
    "build_sealed_artifact",
    "verify_sealed_artifact",
]
