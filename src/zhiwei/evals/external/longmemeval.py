"""LongMemEval external adapter（specs/s7 §8、S9 §2/§3）。

模块只做三件事：解析清单登记的 spec、确定性探测、available 时的离线完整性执行。
不内置任何数据、不发任何网络请求——数据受许可约束，由 operator 在部署处放置；
仓库内探测结果恒为 unavailable（机器可读缺失理由）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zhiwei.evals.external.base import (
    REPO_ROOT,
    AdapterProbe,
    ExternalAdapterSpec,
    probe_adapter,
    resolve_external_adapter,
    run_available_adapter,
)

LONGMEMEVAL_ADAPTER = "longmemeval-adapter"


def resolve_longmemeval_adapter() -> ExternalAdapterSpec:
    """解析清单里的 LongMemEval adapter 声明；未登记 fail closed。"""
    return resolve_external_adapter(LONGMEMEVAL_ADAPTER)


def probe_longmemeval(*, root: Path = REPO_ROOT) -> AdapterProbe:
    """确定性本地探测（许可/version/数据文件存在性）。"""
    return probe_adapter(resolve_longmemeval_adapter(), root=root)


def run_longmemeval_integrity(*, root: Path = REPO_ROOT) -> dict[str, Any]:
    """available 时的离线完整性执行；unavailable 即拒绝（fail closed）。"""
    spec = resolve_longmemeval_adapter()
    return run_available_adapter(spec, probe_adapter(spec, root=root), root=root)
