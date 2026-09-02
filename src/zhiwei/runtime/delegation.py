"""S2 runtime: delegation chain, depth bounds (ADR-008)。

事实源：design doc §4.6、ADR-008。

ChildTask: scope/budget/depth/deadline narrowing. delegation_chain as typed field.
max_delegation_depth hard limit. Delegate and Agent-as-tool share same counter.
Self-delegation requires explicit declaration + depth limit.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from zhiwei.contracts.identifiers import new_id

# ADR-008: hard upper bound on delegation depth
MAX_DELEGATION_DEPTH = 8


class DelegationError(RuntimeError):
    """Invalid delegation operation (depth exceeded, self-delegation violation)."""


class DelegationLink(BaseModel):
    """A single link in the delegation chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    run_id: UUID
    is_agent_tool: bool = False
    is_self_delegation: bool = False


class DelegationChain(BaseModel):
    """Typed delegation chain with depth tracking.

    Delegate and Agent-as-tool share the same depth counter (ADR-008).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    links: tuple[DelegationLink, ...] = ()

    @property
    def depth(self) -> int:
        return len(self.links)

    def append(
        self,
        *,
        task_id: str,
        run_id: UUID,
        is_agent_tool: bool = False,
        is_self_delegation: bool = False,
        self_delegation_limit: int | None = None,
    ) -> DelegationChain:
        """Append a delegation link, enforcing depth bounds."""
        if len(self.links) >= MAX_DELEGATION_DEPTH:
            raise DelegationError(
                f"Delegation depth {len(self.links)} already at maximum "
                f"({MAX_DELEGATION_DEPTH})"
            )
        if not is_self_delegation:
            # Check if this task already appears in the chain (potential cycle)
            existing_tasks = [link.task_id for link in self.links]
            if task_id in existing_tasks:
                raise DelegationError(
                    "self-delegation requires explicit declaration"
                )
        if is_self_delegation and self_delegation_limit is not None:
            # Count total occurrences of this task in the chain
            total_count = sum(1 for link in self.links if link.task_id == task_id)
            if total_count >= self_delegation_limit:
                raise DelegationError(
                    f"self-delegation for task '{task_id}' exceeded limit "
                    f"of {self_delegation_limit}"
                )
            link = DelegationLink(
                task_id=task_id,
                run_id=run_id,
                is_agent_tool=is_agent_tool,
                is_self_delegation=True,
            )
            return DelegationChain(
                links=(*self.links, link),
            )
        link = DelegationLink(
            task_id=task_id,
            run_id=run_id,
            is_agent_tool=is_agent_tool,
        )
        return DelegationChain(
            links=(*self.links, link),
        )


class ChildTask(BaseModel):
    """A child task created by delegation, with narrowed scope/budget/depth/deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    parent_run_id: UUID
    parent_task_id: str
    child_task_id: str
    scope: str
    budget: float
    deadline_minutes: int
    depth: int
    child_chain: DelegationChain


class DelegationManager:
    """Manages delegation of child tasks with depth tracking."""

    def __init__(self) -> None:
        self._children: dict[UUID, ChildTask] = {}

    def create_child(
        self,
        *,
        parent_run_id: UUID,
        parent_task_id: str,
        child_task_id: str,
        scope: str,
        budget: float,
        deadline_minutes: int,
        parent_chain: DelegationChain | None = None,
    ) -> ChildTask:
        """Create a child task, inheriting and incrementing the delegation chain."""
        chain = parent_chain or DelegationChain()
        child_chain = chain.append(task_id=child_task_id, run_id=parent_run_id)
        child = ChildTask(
            id=new_id(),
            parent_run_id=parent_run_id,
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            scope=scope,
            budget=budget,
            deadline_minutes=deadline_minutes,
            depth=child_chain.depth,
            child_chain=child_chain,
        )
        self._children[child.id] = child
        return child

    def get(self, child_id: UUID) -> ChildTask:
        """Get a child task by ID."""
        child = self._children.get(child_id)
        if child is None:
            raise DelegationError(f"Child task {child_id} not found")
        return child
