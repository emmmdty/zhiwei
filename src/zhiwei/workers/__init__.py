"""S2 runtime: Worker setup and configuration。

事实源：design doc §4.3、S2-T3 plan。

Worker configuration for Temporal integration. Abstracts the worker
registration and activity binding without requiring the Temporal SDK.
"""

from __future__ import annotations

from typing import Any, Protocol

from zhiwei.workflows.activities.base import Activities
from zhiwei.workflows.agent_run import AgentRunWorkflow


class WorkerConfig(Protocol):
    """Port for worker configuration."""

    def get_task_queue(self) -> str: ...

    def get_max_concurrent_activities(self) -> int: ...

    def get_max_concurrent_workflows(self) -> int: ...


class AgentWorker:
    """Worker that registers workflows and activities.

    Abstracts the Temporal worker setup. In production, this would
    register with a Temporal dev server. For testing, it operates
    in-process.
    """

    def __init__(
        self,
        workflow: AgentRunWorkflow,
        activities: Activities,
        config: WorkerConfig | None = None,
    ) -> None:
        self._workflow = workflow
        self._activities = activities
        self._config = config
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

    def run_workflow(self, config: Any) -> Any:
        """Run a workflow through the registered workflow handler."""
        return self._workflow.run(config)
