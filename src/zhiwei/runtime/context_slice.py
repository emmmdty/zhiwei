"""S2 §3.1 ContextSlice——父 Agent 委托子 Agent 的最小上下文清单（域层）。

事实源：specs/s2-agent-runtime.md §3.1、docs/review/findings/R2-agent-security.md
F-R2-05（P5/T-P5.1 域层实现，冻结 RED `38f0233`）。

设计：结构上不存在承载禁传内容（父级 transcript、原始凭据、personal
memory）的字段——收窄不靠约定靠类型；`include_personal_memory` 无开启路径
（fail closed）。发布期对委托图的扫描与 delegation handler 的调用点随 S11
委托接线批落位（委托边持久化缺席 = E-R3，2026-09-07 登记）——届时
`validate_context_slice` 在发布期拒绝违规委托、`assert_context_slice_minimal`
在每次委托前再次断言，不得放宽。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta


@dataclass(frozen=True)
class DataScope:
    """子 Agent 数据访问边界：父级 scope 的显式子集。"""

    workspaces: frozenset[uuid.UUID] = field(default_factory=frozenset)
    data_classes: frozenset[str] = field(default_factory=frozenset)
    # spec §3.1：personal memory 禁传——无开启路径（构造即拒绝）
    include_personal_memory: bool = False

    def __post_init__(self) -> None:
        if self.include_personal_memory:
            raise ValueError(
                "personal memory must not be delegated (spec s2 §3.1); "
                "include_personal_memory has no enabled path"
            )


@dataclass(frozen=True)
class TokenBudget:
    """分配给子 Agent 的 token 预算（≤ 父级为该委托分配的份额）。"""

    total_tokens: int


@dataclass(frozen=True)
class ContextSlice:
    """Minimal context passed to child agents during delegation."""

    allowed_tools: list[str]
    data_scope: DataScope
    budget: TokenBudget
    deadline: timedelta
    output_schema: dict | None = None


def validate_context_slice(
    slice_: ContextSlice,
    *,
    parent_workspaces: frozenset[uuid.UUID] | None = None,
) -> None:
    """发布期/通用校验：违规委托在此拒绝（发布期扫描点随 S11 接线落位）。

    Raises ValueError: allowlist 为空、budget/deadline 非正、或 data_scope
    不是父级 workspace 集的子集。
    """
    if not slice_.allowed_tools:
        raise ValueError("context slice allowlist must not be empty")
    if slice_.budget.total_tokens <= 0:
        raise ValueError("context slice budget must be positive")
    if slice_.deadline <= timedelta(0):
        raise ValueError("context slice deadline must be positive")
    if parent_workspaces is not None and not slice_.data_scope.workspaces <= (
        parent_workspaces
    ):
        raise ValueError(
            "context slice data_scope must be a subset of the parent's workspaces"
        )


def assert_context_slice_minimal(slice_: ContextSlice) -> ContextSlice:
    """运行时断言：delegation handler 每次委托前调用（spec §3.1 二次防线）。

    Returns the slice unchanged when minimal；违规即抛（调用方不得吞）。
    """
    validate_context_slice(slice_)
    return slice_
