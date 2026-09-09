"""ADR-009 TTL 自动过期的生产调度（F-R6-06，P2b）。

MemoryTtlSweepWorkflow + MemoryTtlSweepActivity：Temporal schedule 周期触发
（workers/schedules.ensure_memory_ttl_schedule），activity 经窄 SECURITY
DEFINER 函数 zhiwei_ttl_sweep_targets（0022）枚举存在过期 candidate 的租户，
逐租户 tenant_session 内执行 PgMemoryRepository.expire_candidates——状态机、
advisory lock、lifecycle 台账与审计同事务全部复用既有机制（_TTL_EXPIRY_ACTOR），
本模块不出现第二套过期实现。

租户纪律：活动以 app 引擎（无 GUC）调用 definer 函数拿目标（0022 是唯一的
跨租户发现面），随后每个目标都在自己的租户事务内处理；单目标失败即整体失败
（Temporal 重试，幂等由 expire 的 CAS/台账承接）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from zhiwei.contracts.time import ensure_utc
from zhiwei.memory.events import MemoryLifecycleLedger
from zhiwei.memory.repositories import PgMemoryRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session


@dataclass(frozen=True, slots=True)
class TtlSweepInput:
    """sweep 输入；now 缺省 = activity 侧 utc_now（测试钉钟传入）。"""

    now: str | None = None


@dataclass(frozen=True, slots=True)
class TtlSweepOutput:
    target_count: int
    expired_count: int


@workflow.defn
class MemoryTtlSweepWorkflow:
    """周期 sweep workflow（由 Temporal schedule 周期启动；单次执行一轮）。"""

    @workflow.run
    async def run(self, data: TtlSweepInput) -> TtlSweepOutput:
        return await workflow.execute_activity(
            "memory_ttl_sweep",
            data,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


async def _enumerate_targets(
    session: AsyncSession, cutoff: datetime
) -> list[tuple[UUID, UUID]]:
    rows = await session.execute(
        text(
            "SELECT organization_id, workspace_id FROM zhiwei_ttl_sweep_targets(:cutoff)"
        ),
        {"cutoff": cutoff},
    )
    return [(row.organization_id, row.workspace_id) for row in rows]


class MemoryTtlSweepActivity:
    """activity 形态的持有者（构造注入 session factory；worker 注册方法）。"""

    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    @activity.defn(name="memory_ttl_sweep")
    async def sweep(self, data: TtlSweepInput) -> TtlSweepOutput:
        from zhiwei.contracts.time import utc_now

        now = ensure_utc(datetime.fromisoformat(data.now)) if data.now else utc_now()
        # 域层 RetentionPolicy 默认 30d；cutoff 与仓储 expire 的判定由同一
        # RetentionPolicy 承担——这里只负责枚举（definer 谓词同口径）
        from zhiwei.memory.domain import RetentionPolicy

        cutoff = now - RetentionPolicy().candidate_ttl
        expired_count = 0
        async with self._sessions() as session:
            targets = await _enumerate_targets(session, cutoff)
        for organization_id, workspace_id in targets:
            context = TenantContext(
                organization_id=organization_id, workspace_id=workspace_id
            )
            async with tenant_session(self._sessions, context) as session:
                # 台账 + 审计与状态转移同事务（_TTL_EXPIRY_ACTOR，既有机制）
                ledger = MemoryLifecycleLedger(session, context)
                repo = PgMemoryRepository(session, context, ledger=ledger)
                expired = await repo.expire_candidates(now)
                expired_count += len(expired)
        return TtlSweepOutput(target_count=len(targets), expired_count=expired_count)
