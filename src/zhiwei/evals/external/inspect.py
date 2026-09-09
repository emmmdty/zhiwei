"""Inspect harness adapter（specs/s9 §2/§3）：确定性 normalized→native 转换层。

与 promptfoo.py 同构：Inspect 的数据/配置不入库；仓库内可测的是**纯转换**——
给定 normalized case（JSONL：case_id/input/expected），产出 Inspect 原生
sample 结构（id/input/target）。转换是纯函数，未知字段 fail closed。

模块名与 stdlib `inspect` 同名：包内绝对导入（Python 3）不受影响，本模块自身
不 import stdlib inspect；需要内省时用 importlib 路径引用本模块。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
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

INSPECT_ADAPTER = "inspect-adapter"

# 与 promptfoo 共用同一 normalized case schema（见 promptfoo.NORMALIZED_FIELDS）。
NORMALIZED_FIELDS = ("case_id", "input", "expected")


def resolve_inspect_adapter() -> ExternalAdapterSpec:
    """解析清单里的 Inspect adapter 声明；未登记 fail closed。"""
    return resolve_external_adapter(INSPECT_ADAPTER)


def probe_inspect(*, root: Path = REPO_ROOT) -> AdapterProbe:
    """确定性本地探测（许可/version/数据文件存在性）。"""
    return probe_adapter(resolve_inspect_adapter(), root=root)


def run_inspect_integrity(*, root: Path = REPO_ROOT) -> dict[str, Any]:
    """available 时的离线完整性执行；unavailable 即拒绝（fail closed）。"""
    spec = resolve_inspect_adapter()
    return run_available_adapter(spec, probe_adapter(spec, root=root), root=root)


def _normalized_row(row: object) -> tuple[str, str, str]:
    if not isinstance(row, Mapping):
        raise ValueError(f"normalized case 必须是 JSON object: {row!r}")
    unknown = sorted(set(row) - set(NORMALIZED_FIELDS))
    if unknown:
        raise ValueError(f"normalized case 含未声明字段（fail closed）: {unknown}")
    missing = [field for field in NORMALIZED_FIELDS if field not in row]
    if missing:
        raise ValueError(f"normalized case 缺少必需字段: {missing}")
    values = (row["case_id"], row["input"], row["expected"])
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("normalized case 字段必须是非空字符串")
    return values


def inspect_samples_from_normalized(
    rows: Iterable[object],
) -> tuple[dict[str, Any], ...]:
    """normalized 行 → Inspect 原生 sample（id/input/target），保持行序。

    纯函数：不触文件系统、不联网；同一输入逐字节同输出。
    """
    return tuple(
        {"id": case_id, "input": user_input, "target": expected}
        for case_id, user_input, expected in (_normalized_row(row) for row in rows)
    )


def inspect_samples_from_file(path: Path) -> tuple[dict[str, Any], ...]:
    """读取 normalized JSONL fixture 并转换为 Inspect 原生 samples。

    空行跳过；非法行带 `file:line` 定位抛 ValueError（fail closed）。
    """
    rows: list[object] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"{path.name}:{line_no}: normalized 行不是合法 JSON: {exc}") from exc
    return inspect_samples_from_normalized(rows)
