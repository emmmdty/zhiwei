"""S5 Source Ledger 的 SyncIntent 消费 activity（F-R6-04，P2b）。

DELETE/REVOKE SyncIntent → PG ledger 的生产处理入口（@activity.defn，注册进
build_agent_worker——见 workers/agent_worker.py）。输入为 JSON 安全 dataclass
（Temporal payload 纪律：业务数据走 schema 化输入，不传 session）；
apply_delete_revoke 在租户事务内执行（幂等：非 REVOKED 行才更新，重复投递
零新审计——at-least-once 语义由状态转移幂等性承接）。

产生 intent 的生产入口：knowledge source disable（api/knowledge.py，P2b 接线）；
webhook HTTP 入口未交付（登记为 S11 移交项，见 F-R6-04 修复台账）。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from zhiwei.knowledge.pg_ledger import ApplyIntentInput, ApplyIntentOutput, PgSourceLedger
from zhiwei.knowledge.sync import SyncEventType, SyncIntent
from zhiwei.persistence.tenant import TenantContext, tenant_session


class SourceLedgerActivity:
    """apply DELETE/REVOKE SyncIntent 到 PG Source Ledger。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @activity.defn
    async def apply_sync_intent(self, data: ApplyIntentInput) -> ApplyIntentOutput:
        context = TenantContext(
            organization_id=UUID(data.organization_id),
            workspace_id=UUID(data.workspace_id),
        )
        intent = SyncIntent(
            event_type=SyncEventType(data.event_type),
            connector=data.connector,
            source_object_id=UUID(data.source_object_id),
            event_id=data.event_id,
            payload=data.payload,
            idempotency_key=data.idempotency_key,
        )
        async with tenant_session(self._sessions, context) as session:
            result = await PgSourceLedger(session, context).apply_delete_revoke(intent)
        return ApplyIntentOutput(
            applied=bool(result.revoked_version_ids),
            revoked_version_ids=sorted(str(vid) for vid in result.revoked_version_ids),
        )
