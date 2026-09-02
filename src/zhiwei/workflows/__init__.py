"""S2 runtime: Temporal durable shell for agent runs。

事实源：design doc §4.3、S2-T3 plan。

Workflow orchestration only — activities append PG events idempotently.
No Temporal SDK dependency; protocols define the contract for future integration.
"""

from __future__ import annotations

from zhiwei.workflows.agent_run import (
    AgentRunWorkflow,
    CancelSignal,
    PauseSignal,
    WorkflowClient,
    WorkflowClientError,
    WorkflowExecutionResult,
    WorkflowRunConfig,
)
from zhiwei.workflows.versioning import VersionPublishWorkflow

__all__ = [
    "AgentRunWorkflow",
    "CancelSignal",
    "PauseSignal",
    "VersionPublishWorkflow",
    "WorkflowClient",
    "WorkflowClientError",
    "WorkflowExecutionResult",
    "WorkflowRunConfig",
]
