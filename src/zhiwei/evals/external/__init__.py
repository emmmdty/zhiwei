"""S7 external eval adapter 注册表与确定性可用性探测。

事实源：specs/s7-memory.md §8（external-status 二选一 sealed artifact 契约）、
ADR-013 决策 2、S7 plan Task 7、S9 §2/§3（per-adapter 模块 + 诊断 scope 标签）。

S9 起机制本体迁移至 `zhiwei.evals.external.base`（manifest/preflight/完整性执行
单一维护点），per-adapter 模块（longmemeval/locomo/bird/promptfoo/inspect）与
blind holdout（holdout.py）、metamorphic 注册（metamorphic.py）建立在 base 之上。
本包 `__init__` 保留共享机制的 re-export：`zhiwei.cli.evals` 与既有测试的
`from zhiwei.evals.external import ...` 路径不变。
"""

from __future__ import annotations

from zhiwei.evals.external.base import (
    AVAILABLE,
    BLIND_HOLDOUT_SCOPE,
    CORPUS_INTEGRITY_RUN,
    DEFAULT_MANIFEST_PATH,
    EXTERNAL_ADAPTER_NAMES,
    EXTERNAL_DIAGNOSTIC_SCOPE,
    METAMORPHIC_SCOPE,
    REPO_ROOT,
    UNAVAILABLE,
    AdapterProbe,
    ExternalAdapterSpec,
    diagnostic_scope,
    ensure_diagnostic_scope,
    external_adapter_names,
    load_adapter_manifest,
    probe_adapter,
    resolve_external_adapter,
    run_available_adapter,
)

__all__ = [
    "AVAILABLE",
    "BLIND_HOLDOUT_SCOPE",
    "CORPUS_INTEGRITY_RUN",
    "DEFAULT_MANIFEST_PATH",
    "EXTERNAL_ADAPTER_NAMES",
    "EXTERNAL_DIAGNOSTIC_SCOPE",
    "METAMORPHIC_SCOPE",
    "REPO_ROOT",
    "UNAVAILABLE",
    "AdapterProbe",
    "ExternalAdapterSpec",
    "diagnostic_scope",
    "ensure_diagnostic_scope",
    "external_adapter_names",
    "load_adapter_manifest",
    "probe_adapter",
    "resolve_external_adapter",
    "run_available_adapter",
]
