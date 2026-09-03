"""S2 runtime: real Temporal worker assembly。

事实源：specs/s2-agent-runtime.md §3/§4、S2-T3 plan。

`build_agent_worker` 把真实 workflow + activities 绑定到一个 task queue；
`build_runtime_activities` 把 activities 绑到 PG session factory 与 handler registry。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.workflows.activities.runtime import RuntimeActivities
from zhiwei.workflows.agent_run import AgentRunWorkflow

DEFAULT_TASK_QUEUE = "zhiwei-agent-runtime"


def build_runtime_activities(
    session_factory: async_sessionmaker[AsyncSession],
    handler_registry: TaskHandlerRegistry,
) -> RuntimeActivities:
    """Bind runtime activities to PG sessions and the handler registry."""

    return RuntimeActivities(session_factory, handler_registry)


def build_agent_worker(
    client: Client,
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
    session_factory: async_sessionmaker[AsyncSession],
    handler_registry: TaskHandlerRegistry,
    **worker_kwargs: Any,
) -> Worker:
    """Assemble the durable-shell worker: workflow + activities on one queue."""

    activities_impl = build_runtime_activities(session_factory, handler_registry)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AgentRunWorkflow],
        activities=[
            activities_impl.start_run,
            activities_impl.execute_task,
            activities_impl.create_approval,
            activities_impl.check_approval,
            activities_impl.record_approval_outcome,
            activities_impl.record_run_terminal,
            activities_impl.record_task_skipped,
            activities_impl.record_task_failed,
        ],
        **worker_kwargs,
    )
