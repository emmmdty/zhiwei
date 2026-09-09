"""S4 Capability Runner worker.

Assembles the Temporal worker that handles tool execution activities.
The Capability Runner worker is an independent internal service that
executes tools in isolated sandboxes (S4 spec §5).

No Docker socket or K8s credential on API/Agent Worker.
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from zhiwei.capabilities.invocations import InvocationRepository
from zhiwei.capabilities.runners.client import RunnerClient
from zhiwei.capabilities.tool_gateway import ToolGateway
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.workflows.activities.tools import ToolActivity

logger = logging.getLogger(__name__)

DEFAULT_TASK_QUEUE = "zhiwei-capability-runner"


def build_tool_activity(
    gateway: ToolGateway,
    invocation_repo: InvocationRepository,
) -> ToolActivity:
    """Build the Tool Activity with its dependencies."""
    return ToolActivity(gateway, invocation_repo)


def build_capability_runner_worker(
    client: Client,
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
    policy_enforcer: PolicyEnforcer,
    runner_client: RunnerClient,
    invocation_repo: InvocationRepository,
    connection_registry: dict[Any, Any] | None = None,
    credential_registry: dict[Any, Any] | None = None,
    capability_registry: dict[Any, Any] | None = None,
    **worker_kwargs: Any,
) -> Worker:
    """Assemble the Capability Runner worker.

    This worker handles tool execution activities in isolated sandboxes.
    It is an independent internal service separate from the API/Agent Worker.

    Args:
        client: Temporal client.
        task_queue: Task queue for capability runner activities.
        policy_enforcer: Policy enforcement engine.
        runner_client: Runner IPC client.
        invocation_repo: Invocation repository.
        connection_registry: Connection registry for tool gateway.
        credential_registry: Credential binding registry.
        capability_registry: Capability version registry.
        **worker_kwargs: Additional worker configuration.

    Returns:
        Configured Temporal Worker for capability runner activities.
    """
    gateway = ToolGateway(
        policy_enforcer=policy_enforcer,
        runner_client=runner_client,
        invocation_repo=invocation_repo,
        connection_registry=connection_registry or {},
        credential_registry=credential_registry or {},
        capability_registry=capability_registry or {},
    )

    tool_activity = build_tool_activity(gateway, invocation_repo)

    return Worker(
        client,
        task_queue=task_queue,
        activities=[
            tool_activity.execute,
        ],
        **worker_kwargs,
    )
