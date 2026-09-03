"""S2-T7 RED: ChildTask execution bridge tests."""

from __future__ import annotations

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.delegation import (
    MAX_DELEGATION_DEPTH,
    ChildTask,
    DelegationChain,
    DelegationError,
    DelegationManager,
    execute_child_task,
)


def _make_manager() -> DelegationManager:
    return DelegationManager()


class TestChildTaskScopeBudgetNarrowing:
    """ChildTask execution: scope/budget/depth narrowing enforcement."""

    def test_child_budget_cannot_exceed_parent(self) -> None:
        mgr = _make_manager()
        parent_budget = 100.0
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=150.0,
            deadline_minutes=30,
        )
        narrowed = execute_child_task(child, parent_budget=parent_budget, parent_scope="full")
        assert float(narrowed["budget"]) <= parent_budget  # type: ignore[arg-type]

    def test_child_scope_is_narrower_than_parent(self) -> None:
        mgr = _make_manager()
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="read-only",
            budget=50.0,
            deadline_minutes=30,
        )
        narrowed = execute_child_task(child, parent_budget=100.0, parent_scope="full")
        assert narrowed["scope"] == "read-only"

    def test_child_depth_increments(self) -> None:
        mgr = _make_manager()
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id())
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
            parent_chain=chain,
        )
        narrowed = execute_child_task(child, parent_budget=100.0, parent_scope="full")
        assert narrowed["depth"] == chain.depth + 1

    def test_execute_child_returns_required_keys(self) -> None:
        mgr = _make_manager()
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
        )
        narrowed = execute_child_task(child, parent_budget=100.0, parent_scope="full")
        assert "budget" in narrowed
        assert "scope" in narrowed
        assert "depth" in narrowed
        assert "deadline_minutes" in narrowed
        assert "delegation_chain" in narrowed

    def test_child_depth_exceeds_max_raises(self) -> None:
        chain = DelegationChain()
        for i in range(MAX_DELEGATION_DEPTH):
            chain = chain.append(task_id=f"t{i}", run_id=new_id())
        child = ChildTask(
            id=new_id(),
            parent_run_id=new_id(),
            parent_task_id=f"t{MAX_DELEGATION_DEPTH - 1}",
            child_task_id="overflow",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
            depth=chain.depth,
            child_chain=chain,
        )
        with pytest.raises(DelegationError, match="depth"):
            execute_child_task(child, parent_budget=100.0, parent_scope="full")

    def test_child_budget_capped_at_parent(self) -> None:
        mgr = _make_manager()
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
        )
        narrowed = execute_child_task(child, parent_budget=10.0, parent_scope="full")
        assert narrowed["budget"] == 10.0

    def test_deadline_preserved(self) -> None:
        mgr = _make_manager()
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=50.0,
            deadline_minutes=45,
        )
        narrowed = execute_child_task(child, parent_budget=100.0, parent_scope="full")
        assert narrowed["deadline_minutes"] == 45
