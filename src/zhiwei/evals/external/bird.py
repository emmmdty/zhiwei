"""BIRD external adapter（specs/s9 §2/§3：text-to-SQL 外部诊断分开报告）。

与 longmemeval/locomo 同构：清单 spec + 确定性探测 + available 时离线完整性执行。
BIRD 数据集不入库（许可约束）；仓库内探测恒为 unavailable，绝不静默下载。
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

BIRD_ADAPTER = "bird-adapter"


def resolve_bird_adapter() -> ExternalAdapterSpec:
    """解析清单里的 BIRD adapter 声明；未登记 fail closed。"""
    return resolve_external_adapter(BIRD_ADAPTER)


def probe_bird(*, root: Path = REPO_ROOT) -> AdapterProbe:
    """确定性本地探测（许可/version/数据文件存在性）。"""
    return probe_adapter(resolve_bird_adapter(), root=root)


def run_bird_integrity(*, root: Path = REPO_ROOT) -> dict[str, Any]:
    """available 时的离线完整性执行；unavailable 即拒绝（fail closed）。"""
    spec = resolve_bird_adapter()
    return run_available_adapter(spec, probe_adapter(spec, root=root), root=root)
