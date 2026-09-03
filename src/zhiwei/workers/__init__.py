"""S2 runtime: real Temporal worker and outbox dispatcher。

事实源：specs/s2-agent-runtime.md §3/§4、S2-T3/T4 plan。
"""

from __future__ import annotations

from zhiwei.workers.agent_worker import (
    DEFAULT_TASK_QUEUE,
    build_agent_worker,
    build_runtime_activities,
)
from zhiwei.workers.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxDispatcherConfig,
    SessionOutboxRepository,
)
from zhiwei.workers.temporal_sender import TemporalWorkflowSender

__all__ = [
    "DEFAULT_TASK_QUEUE",
    "OutboxDispatcher",
    "OutboxDispatcherConfig",
    "SessionOutboxRepository",
    "TemporalWorkflowSender",
    "build_agent_worker",
    "build_runtime_activities",
]
