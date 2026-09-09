"""Promptfoo harness adapter（specs/s9 §2/§3）：确定性 normalized→native 转换层。

Promptfoo 的数据/配置不入库；仓库内可测的是**纯转换**：给定 normalized case
（JSONL：case_id/input/expected），产出 promptfoo 原生 tests 结构。转换是纯函数
（同输入逐字节同输出、保持行序），未知字段 fail closed——不猜测字段语义。

为什么是转换层而不是执行器：实际 harness 运行需要 promptfoo 安装与模型端点
（live、operator 显式触发），本模块只负责把内部 normalized 形态确定性地落到
harness 原生形态；探测/preflight 与其他 adapter 同构（base 机制）。
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

PROMPTFOO_ADAPTER = "promptfoo-adapter"

# normalized case 的最小 schema：适配器之间共用同一内部形态，转换层各自落
# harness 原生形态。schema 之外的字段拒绝（与清单 extra=forbid 同一纪律）。
NORMALIZED_FIELDS = ("case_id", "input", "expected")


def resolve_promptfoo_adapter() -> ExternalAdapterSpec:
    """解析清单里的 Promptfoo adapter 声明；未登记 fail closed。"""
    return resolve_external_adapter(PROMPTFOO_ADAPTER)


def probe_promptfoo(*, root: Path = REPO_ROOT) -> AdapterProbe:
    """确定性本地探测（许可/version/数据文件存在性）。"""
    return probe_adapter(resolve_promptfoo_adapter(), root=root)


def run_promptfoo_integrity(*, root: Path = REPO_ROOT) -> dict[str, Any]:
    """available 时的离线完整性执行；unavailable 即拒绝（fail closed）。"""
    spec = resolve_promptfoo_adapter()
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


def promptfoo_cases_from_normalized(
    rows: Iterable[object],
) -> tuple[dict[str, Any], ...]:
    """normalized 行 → promptfoo 原生 test case（vars + assert），保持行序。

    纯函数：不触文件系统、不联网；同一输入逐字节同输出（dict 字面量键序固定）。
    """
    return tuple(
        {
            "description": case_id,
            "vars": {"input": user_input},
            "assert": [{"type": "equals", "value": expected}],
        }
        for case_id, user_input, expected in (_normalized_row(row) for row in rows)
    )


def promptfoo_cases_from_file(path: Path) -> tuple[dict[str, Any], ...]:
    """读取 normalized JSONL fixture 并转换为 promptfoo 原生 tests。

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
    return promptfoo_cases_from_normalized(rows)
