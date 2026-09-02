"""S2-T5 RED: Delegation (ADR-008) tests."""

from __future__ import annotations

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.delegation import (
    MAX_DELEGATION_DEPTH,
    DelegationChain,
    DelegationError,
    DelegationManager,
)


def _make_manager() -> DelegationManager:
    return DelegationManager()


class TestChildTaskScopeBudgetDepthDeadline:
    """ChildTask: scope/budget/depth/deadline narrowing."""

    def test_child_task_has_narrower_budget(self) -> None:
        mgr = _make_manager()
        parent_budget = 100.0
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
        )
        assert child.budget < parent_budget

    def test_child_task_has_depth(self) -> None:
        mgr = _make_manager()
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
        )
        assert child.depth == 1

    def test_child_task_has_deadline(self) -> None:
        mgr = _make_manager()
        child = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
        )
        assert child.deadline_minutes == 30


class TestDelegationChain:
    """delegation_chain as typed field."""

    def test_chain_starts_at_depth_0(self) -> None:
        chain = DelegationChain()
        assert chain.depth == 0

    def test_chain_increments_on_append(self) -> None:
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id())
        assert chain.depth == 1
        assert len(chain.links) == 1

    def test_chain_multiple_appends(self) -> None:
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id())
        chain = chain.append(task_id="t2", run_id=new_id())
        assert chain.depth == 2


class TestMaxDelegationDepth:
    """max_delegation_depth hard limit."""

    def test_exceeds_max_depth_raises(self) -> None:
        chain = DelegationChain()
        for i in range(MAX_DELEGATION_DEPTH):
            chain = chain.append(task_id=f"t{i}", run_id=new_id())
        with pytest.raises(DelegationError, match="depth"):
            chain.append(task_id="overflow", run_id=new_id())

    def test_exact_max_depth_allowed(self) -> None:
        chain = DelegationChain()
        for i in range(MAX_DELEGATION_DEPTH):
            chain = chain.append(task_id=f"t{i}", run_id=new_id())
        assert chain.depth == MAX_DELEGATION_DEPTH


class TestDelegateAndAgentAsToolShareCounter:
    """Delegate and Agent-as-tool share the same depth counter."""

    def test_delegate_increments_counter(self) -> None:
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id())
        assert chain.depth == 1

    def test_agent_as_tool_increments_same_counter(self) -> None:
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id(), is_agent_tool=True)
        assert chain.depth == 1

    def test_mixed_delegation_and_agent_tool_share_counter(self) -> None:
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id())
        chain = chain.append(task_id="t2", run_id=new_id(), is_agent_tool=True)
        assert chain.depth == 2


class TestSelfDelegation:
    """Self-delegation requires explicit declaration + depth limit."""

    def test_self_delegation_requires_declaration(self) -> None:
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id())
        with pytest.raises(DelegationError, match="self-delegation"):
            chain.append(task_id="t1", run_id=new_id(), is_self_delegation=False)

    def test_self_delegation_with_declaration(self) -> None:
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id())
        chain = chain.append(
            task_id="t1", run_id=new_id(), is_self_delegation=True, self_delegation_limit=3
        )
        assert chain.depth == 2

    def test_self_delegation_exceeds_limit(self) -> None:
        chain = DelegationChain()
        chain = chain.append(task_id="t1", run_id=new_id())
        chain = chain.append(
            task_id="t1", run_id=new_id(), is_self_delegation=True, self_delegation_limit=2
        )
        with pytest.raises(DelegationError, match="self-delegation"):
            chain.append(
                task_id="t1", run_id=new_id(), is_self_delegation=True, self_delegation_limit=2
            )


class TestDelegationChainCAS:
    """Delegation chain CAS for concurrent child creation."""

    def test_cas_prevents_inconsistent_chain(self) -> None:
        mgr = _make_manager()
        parent_chain = DelegationChain()
        child1 = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c1",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
            parent_chain=parent_chain,
        )
        child2 = mgr.create_child(
            parent_run_id=new_id(),
            parent_task_id="t1",
            child_task_id="c2",
            scope="limited",
            budget=50.0,
            deadline_minutes=30,
            parent_chain=parent_chain,
        )
        assert child1.child_chain.depth == 1
        assert child2.child_chain.depth == 1
