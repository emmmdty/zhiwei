"""S2 runtime: real Temporal durable shell for agent runs。

事实源：specs/s2-agent-runtime.md §3/§4、S2-T3 plan、总设计 §4.3。

Workflow 只做编排（排序、信号、重试位置、Continue-As-New），PG canonical events 是
唯一业务真相；activity 是唯一副作用边界。本包绑定真实 `temporalio` SDK——不提供
任何「评测专用」或进程内旁路（评测走同一 Workflow/TaskGraph，见 evals executor）。
"""

from __future__ import annotations

from zhiwei.workflows.agent_run import (
    AgentRunWorkflow,
    AgentRunWorkflowInput,
    AgentRunWorkflowResult,
)

__all__ = [
    "AgentRunWorkflow",
    "AgentRunWorkflowInput",
    "AgentRunWorkflowResult",
]
