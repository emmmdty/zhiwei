"""LoCoMo external adapter（specs/s9 §2/§3：external 分开报告）。

与 longmemeval 同构：清单 spec + 确定性探测 + available 时离线完整性执行。
LoCoMo 对话数据不入库（许可约束）；仓库内探测恒为 unavailable，绝不静默下载。
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

LOCOMO_ADAPTER = "locomo-adapter"


def resolve_locomo_adapter() -> ExternalAdapterSpec:
    """解析清单里的 LoCoMo adapter 声明；未登记 fail closed。"""
    return resolve_external_adapter(LOCOMO_ADAPTER)


def probe_locomo(*, root: Path = REPO_ROOT) -> AdapterProbe:
    """确定性本地探测（许可/version/数据文件存在性）。"""
    return probe_adapter(resolve_locomo_adapter(), root=root)


def run_locomo_integrity(*, root: Path = REPO_ROOT) -> dict[str, Any]:
    """available 时的离线完整性执行；unavailable 即拒绝（fail closed）。"""
    spec = resolve_locomo_adapter()
    return run_available_adapter(spec, probe_adapter(spec, root=root), root=root)
