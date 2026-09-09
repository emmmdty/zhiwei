"""Temporal schedule 注册（F-R6-06，P2b）：ADR-009 TTL sweep 周期执行。

ensure_memory_ttl_schedule 幂等创建 schedule（已存在即返回，不改 spec——
周期/任务队列的变更走显式运维动作），schedule 动作 = 启动
MemoryTtlSweepWorkflow（workers/activities/memory_ttl.py）。handle.trigger()
提供测试/运维的立即触发入口。

生产 worker 组装点属 S11 compose（当前仓内 worker 仅 evals executor 与测试
组装）——登记于 F-R6-06 修复台账。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from temporalio.client import Client
from temporalio.service import RPCError

from zhiwei.workflows.activities.memory_ttl import (
    MemoryTtlSweepWorkflow,
    TtlSweepInput,
)

logger = logging.getLogger(__name__)

TTL_SWEEP_SCHEDULE_ID = "memory-ttl-sweep"
_TTL_SWEEP_WORKFLOW_ID = "memory-ttl-sweep-workflow"


async def ensure_memory_ttl_schedule(
    client: Client,
    *,
    task_queue: str,
    interval_seconds: int,
) -> None:
    """幂等创建 TTL sweep schedule；已存在即返回（spec 不就地变更）。"""
    handle = client.get_schedule_handle(TTL_SWEEP_SCHEDULE_ID)
    try:
        await handle.describe()
        return
    except RPCError as exc:
        if exc.status.name != "NOT_FOUND":
            raise
    # 并发首建的 ALREADY_EXISTS 同样视为已存在（幂等；spec 不就地改写）


    try:
        await _create_schedule(client, task_queue, interval_seconds)
    except RPCError as exc:
        if exc.status.name != "ALREADY_EXISTS":
            raise
        return


async def _create_schedule(
    client: Client, task_queue: str, interval_seconds: int
) -> None:
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleSpec,
    )

    await client.create_schedule(
        TTL_SWEEP_SCHEDULE_ID,
        Schedule(
            action=ScheduleActionStartWorkflow(
                MemoryTtlSweepWorkflow.__name__,
                TtlSweepInput(),
                id=f"{TTL_SWEEP_SCHEDULE_ID}:{_TTL_SWEEP_WORKFLOW_ID}",
                task_queue=task_queue,
                execution_timeout=timedelta(minutes=15),
            ),
            spec=ScheduleSpec(
                intervals=[ScheduleIntervalSpec(every=timedelta(seconds=interval_seconds))]
            ),
        ),
    )
    logger.info(
        "temporal schedule %s created (every %ss on %s)",
        TTL_SWEEP_SCHEDULE_ID,
        interval_seconds,
        task_queue,
    )
