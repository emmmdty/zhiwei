"""F-R2-05（P5/T-P5.1）：ContextSlice 冻结 RED 三件套——spec s2 §3.1 委托
上下文收窄清单的域层契约（typed 字段 + 校验 + 运行时断言）。

事实源：specs/s2-agent-runtime.md §3.1（禁含父级 transcript/原始凭据/
personal memory；发布期拒绝 + 运行时由 delegation handler 再次断言）。

本批冻结的是域层契约与断言面（F-R2-05 建议「在委托接线前冻结，避免先接线
后补门」）；委托边持久化表示缺席（例外 E-R3，2026-09-07 登记），发布期对
委托图的扫描与 delegation handler 的调用点随 S11 委托接线批落位——届时本
三件套即生效锚点，不得放宽。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from zhiwei.runtime.context_slice import (
    ContextSlice,
    DataScope,
    TokenBudget,
    assert_context_slice_minimal,
    validate_context_slice,
)


class TestContextSliceTypedFields:
    """三件套之一：typed 字段——结构上不存在承载禁传内容的字段。"""

    def test_spec_fields_present(self) -> None:
        slice_ = ContextSlice(
            allowed_tools=["search", "calculator"],
            data_scope=DataScope(
                workspaces=frozenset({__import__("uuid").uuid4()}),
                data_classes=frozenset({"public"}),
            ),
            budget=TokenBudget(total_tokens=4096),
            deadline=timedelta(minutes=10),
        )
        assert slice_.allowed_tools == ["search", "calculator"]
        assert slice_.output_schema is None
        assert slice_.deadline == timedelta(minutes=10)

    def test_structurally_excludes_forbidden_content(self) -> None:
        """父级 transcript / 原始凭据 / personal memory 无承载字段——
        dataclass 字段集恒等于 spec 声明集，任何「顺手加字段」即契约破坏。"""
        fields = set(ContextSlice.__dataclass_fields__)
        assert fields == {
            "allowed_tools",
            "data_scope",
            "budget",
            "deadline",
            "output_schema",
        }
        scope_fields = set(DataScope.__dataclass_fields__)
        assert "transcript" not in scope_fields
        assert "credentials" not in scope_fields

    def test_personal_memory_cannot_be_enabled(self) -> None:
        """personal memory 禁传：include_personal_memory 无开启路径（fail closed）。"""
        with pytest.raises(ValueError, match="personal memory"):
            DataScope(
                workspaces=frozenset(),
                data_classes=frozenset(),
                include_personal_memory=True,
            )


class TestContextSliceValidation:
    """三件套之二：发布期/通用校验——违规委托在发布期即被拒绝。"""

    def _valid_slice(self) -> ContextSlice:
        return ContextSlice(
            allowed_tools=["search"],
            data_scope=DataScope(
                workspaces=frozenset(), data_classes=frozenset({"public"})
            ),
            budget=TokenBudget(total_tokens=1024),
            deadline=timedelta(minutes=5),
        )

    def test_valid_slice_passes(self) -> None:
        validate_context_slice(self._valid_slice())

    def test_empty_allowlist_rejected(self) -> None:
        slice_ = ContextSlice(
            allowed_tools=[],
            data_scope=DataScope(workspaces=frozenset(), data_classes=frozenset()),
            budget=TokenBudget(total_tokens=1024),
            deadline=timedelta(minutes=5),
        )
        with pytest.raises(ValueError, match="allowlist"):
            validate_context_slice(slice_)

    def test_non_positive_budget_or_deadline_rejected(self) -> None:
        base = self._valid_slice()
        zero_budget = ContextSlice(
            allowed_tools=base.allowed_tools,
            data_scope=base.data_scope,
            budget=TokenBudget(total_tokens=0),
            deadline=base.deadline,
        )
        with pytest.raises(ValueError, match="budget"):
            validate_context_slice(zero_budget)
        zero_deadline = ContextSlice(
            allowed_tools=base.allowed_tools,
            data_scope=base.data_scope,
            budget=base.budget,
            deadline=timedelta(0),
        )
        with pytest.raises(ValueError, match="deadline"):
            validate_context_slice(zero_deadline)

    def test_data_scope_must_be_subset_of_parent(self) -> None:
        """data_scope 必须为父级 scope 子集——越权数据类拒绝。"""
        from uuid import uuid4

        parent_ws = uuid4()
        slice_ = ContextSlice(
            allowed_tools=["search"],
            data_scope=DataScope(
                workspaces=frozenset({uuid4()}),  # 父级不可见 workspace
                data_classes=frozenset({"public"}),
            ),
            budget=TokenBudget(total_tokens=1024),
            deadline=timedelta(minutes=5),
        )
        with pytest.raises(ValueError, match="subset"):
            validate_context_slice(slice_, parent_workspaces=frozenset({parent_ws}))
        # 父级可见 → 通过
        validate_context_slice(
            slice_, parent_workspaces=frozenset({uuid4(), *slice_.data_scope.workspaces})
        )


class TestContextSliceRuntimeAssertion:
    """三件套之三：运行时断言——delegation handler（S11 接线）每次委托前调用。"""

    def test_assertion_returns_slice_when_minimal(self) -> None:
        slice_ = ContextSlice(
            allowed_tools=["search"],
            data_scope=DataScope(workspaces=frozenset(), data_classes=frozenset()),
            budget=TokenBudget(total_tokens=1024),
            deadline=timedelta(minutes=5),
        )
        assert assert_context_slice_minimal(slice_) is slice_

    def test_assertion_raises_on_violation(self) -> None:
        slice_ = ContextSlice(
            allowed_tools=[],
            data_scope=DataScope(workspaces=frozenset(), data_classes=frozenset()),
            budget=TokenBudget(total_tokens=1024),
            deadline=timedelta(minutes=5),
        )
        with pytest.raises(ValueError, match="allowlist"):
            assert_context_slice_minimal(slice_)
