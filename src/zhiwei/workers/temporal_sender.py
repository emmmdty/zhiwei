"""S2 workers: real Temporal binding for the WorkflowSignalSender port。

事实源：specs/s2-agent-runtime.md §4、S2-T4 plan。

- start：deterministic workflow id（`run-{run_id}`）+ REJECT_DUPLICATE——同 id 只允许
  一次外部 start（running 或已结束均拒绝）；重复投递映射为幂等 delivered no-op。
  真相在 PG canonical events，不在 workflow 存在性。
- signal：目标 workflow 不存在 → WorkflowNotFoundError（dispatcher 有界重试；worker
  未起或 start 命令尚未投递时出现）。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from zhiwei.runtime.outbox_handlers import WorkflowNotFoundError

logger = logging.getLogger(__name__)

_AGENT_RUN_WORKFLOW = "agent-run"


class TemporalWorkflowSender:
    """Async WorkflowSignalSender backed by a real Temporal client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def start_workflow(
        self,
        *,
        workflow_id: str,
        workflow_type: str,
        input: dict[str, Any],
        organization_id: UUID,
        workspace_id: UUID,
    ) -> None:
        if workflow_type != _AGENT_RUN_WORKFLOW:
            raise WorkflowNotFoundError(f"unknown workflow type: {workflow_type!r}")
        from zhiwei.workflows.agent_run import AgentRunWorkflowInput

        workflow_input = AgentRunWorkflowInput(
            run_id=workflow_id.removeprefix("run-"),
            organization_id=str(organization_id),
            workspace_id=str(workspace_id),
            graph=dict(input["graph"]),
            task_queue=str(input["task_queue"]),
            max_task_attempts=int(input.get("max_task_attempts", 3)),
            continue_as_new_after=int(input.get("continue_as_new_after", 1000)),
            activity_timeout_seconds=int(input.get("activity_timeout_seconds", 60)),
            requested_by=str(input.get("requested_by") or "system"),
        )
        try:
            await self._client.start_workflow(
                _AGENT_RUN_WORKFLOW,
                workflow_input,
                id=workflow_id,
                task_queue=workflow_input.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            logger.info(
                "workflow %s already started or finished; treating as delivered",
                workflow_id,
            )

    async def signal_workflow(
        self,
        *,
        workflow_id: str,
        signal_name: str,
        payload: dict[str, Any],
    ) -> None:
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(signal_name, payload)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise WorkflowNotFoundError(
                    f"workflow {workflow_id!r} not found for signal {signal_name!r}"
                ) from exc
            raise
