"""F-R5-05（T-P4.4/B4b）：trigger 状态持久 store——watermark 推进记录 +
webhook delivery nonce（一次性 claim）。

事实源：docs/review/findings/R5-distributed.md F-R5-05（watermark 进程内
dict 重启即失；webhook 无防重放）。0023 迁移提供 tenant-scoped 表；本模块
是唯一访问面：watermark = UPSERT value 列，nonce = INSERT ON CONFLICT DO
NOTHING 的一次性 claim（重复投递 claim 返回 False，fail closed）。

会话纪律与 PgMemoryRepository 同型：绑定调用方事务 session，不自行
commit/rollback——claim 与业务写入的原子性由调用方事务边界决定。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.persistence.models import TriggerWatermarkRow
from zhiwei.persistence.tenant import TenantContext

_WATERMARK_SCOPE = "source_delta.watermark"
_NONCE_SCOPE = "webhook.nonce"
_SCHEMA_VERSION = 1


class PgTriggerStateStore:
    """DiscoveryTriggerService 的可选状态面：注入后 watermark 跨进程/重启
    持久，webhook delivery 防重放可用；未注入时服务按 fail closed 拒绝
    webhook delivery 校验。"""

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        self._session = session
        self._context = context

    async def load_watermark(self, key: str) -> str | None:
        row = await self._session.scalar(
            select(TriggerWatermarkRow).where(
                TriggerWatermarkRow.organization_id == self._context.organization_id,
                TriggerWatermarkRow.workspace_id == self._context.workspace_id,
                TriggerWatermarkRow.scope == _WATERMARK_SCOPE,
                TriggerWatermarkRow.state_key == key,
            )
        )
        if row is None:
            return None
        watermark = row.value.get("watermark")
        return str(watermark) if watermark is not None else None

    async def store_watermark(self, key: str, value: str) -> None:
        statement = (
            pg_insert(TriggerWatermarkRow)
            .values(
                id=uuid.uuid4(),
                organization_id=self._context.organization_id,
                workspace_id=self._context.workspace_id,
                scope=_WATERMARK_SCOPE,
                state_key=key,
                value={"watermark": value},
                schema_version=_SCHEMA_VERSION,
            )
            .on_conflict_do_update(
                constraint="uq_trigger_watermarks_tenant_scope_key",
                set_={"value": {"watermark": value}, "updated_at": _utcnow()},
            )
        )
        await self._session.execute(statement)

    async def claim_delivery(self, delivery_id: str, claimed_at: str) -> bool:
        """一次性 claim：True = 首次投递；False = 已存在（重放拒绝）。"""
        statement = (
            pg_insert(TriggerWatermarkRow)
            .values(
                id=uuid.uuid4(),
                organization_id=self._context.organization_id,
                workspace_id=self._context.workspace_id,
                scope=_NONCE_SCOPE,
                state_key=delivery_id,
                value={"claimed_at": claimed_at},
                schema_version=_SCHEMA_VERSION,
            )
            .on_conflict_do_nothing(
                constraint="uq_trigger_watermarks_tenant_scope_key"
            )
            .returning(TriggerWatermarkRow.id)
        )
        claimed = await self._session.scalar(statement)
        return claimed is not None


def _utcnow() -> datetime:
    return datetime.now(UTC)
