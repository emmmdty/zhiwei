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
from zhiwei.workflows.activities.memory_ttl import (
    MemoryTtlSweepActivity,
    MemoryTtlSweepWorkflow,
)
from zhiwei.workflows.activities.runtime import RuntimeActivities
from zhiwei.workflows.activities.source_ledger import SourceLedgerActivity
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
    # F-R6-04：SyncIntent DELETE/REVOKE → PG Source Ledger 的生产消费入口
    # （产生方：knowledge source disable 等；幂等由状态转移承接）。
    source_ledger_activity = SourceLedgerActivity(session_factory)
    # F-R6-06：ADR-009 TTL 自动过期的生产调度 activity（周期触发走
    # workers/schedules.ensure_memory_ttl_schedule 创建的 Temporal schedule）。
    memory_ttl_activity = MemoryTtlSweepActivity(session_factory)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AgentRunWorkflow, MemoryTtlSweepWorkflow],
        activities=[
            activities_impl.start_run,
            activities_impl.execute_task,
            activities_impl.create_approval,
            activities_impl.check_approval,
            activities_impl.record_approval_outcome,
            activities_impl.record_run_terminal,
            activities_impl.record_task_skipped,
            activities_impl.record_task_failed,
            # S11-T5：CAN 边界 PG 意图回查（崩溃窗口 #11 兜底）
            activities_impl.run_intent_recheck,
            source_ledger_activity.apply_sync_intent,
            memory_ttl_activity.sweep,
        ],
        **worker_kwargs,
    )
