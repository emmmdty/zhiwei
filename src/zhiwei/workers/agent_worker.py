"""S2 runtime: Agent worker setup and configuration。

事实源：design doc §4.3、S2-T3 plan。

Worker that orchestrates agent runs through Temporal workflows.
Binds activities to the workflow and manages worker lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zhiwei.workflows.activities.base import Activities
from zhiwei.workflows.agent_run import AgentRunWorkflow, WorkflowClient


@dataclass(frozen=True)
class AgentWorkerConfig:
    """Configuration for the agent worker."""

    task_queue: str = "zhiwei-agent-runs"
    max_concurrent_activities: int = 10
    max_concurrent_workflows: int = 5


class AgentWorker:
    """Worker that registers and executes agent run workflows.

    Binds RuntimeActivities to the workflow and manages the worker lifecycle.
    In production, this would register with a Temporal dev server.
    """

    def __init__(
        self,
        client: WorkflowClient,
        activities: Activities,
        config: AgentWorkerConfig | None = None,
    ) -> None:
        self._client = client
        self._activities = activities
        self._config = config or AgentWorkerConfig()
        self._workflow = AgentRunWorkflow(client, activities)
        self._running = False

    def start(self) -> None:
        """Start the worker."""
        self._running = True

    def stop(self) -> None:
        """Stop the worker."""
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def workflow(self) -> AgentRunWorkflow:
        return self._workflow

    @property
    def activities(self) -> Activities:
        return self._activities

    def run_workflow(self, config: Any) -> Any:
        """Run a workflow through the registered handler."""
        return self._workflow.run(config)
