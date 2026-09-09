"""unverified endpoint 首次使用留痕的 canonical PG 写入器（ADR-011 §6）。

「首次」判定 = org+workspace 事件流查重（event_type + payload->>'base_url'），
先以 (workspace, EPFU) advisory xact lock 串行化并发：两个 run 同时首次使用同一
endpoint 时，后到事务在锁内看到先到事务已提交的记录后返回 False——确定性、
无重复、无额外表。

写入走 CanonicalUnitOfWork 落账路径：canonical event + audit（action=
canonical_event.append，payload_digest=event_digest）+ outbox 同事务提交，
调用方 commit/rollback 决定整体命运。声明字段全量进入 event payload。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.contracts.envelope import SchemaRegistry
from zhiwei.models.first_use import (
    FIRST_USE_EVENT_TYPE,
    FIRST_USE_PAYLOAD_SCHEMA_VERSION,
    EndpointFirstUseDeclaration,
    EndpointFirstUsePayload,
    first_use_idempotency_key,
    first_use_payload,
)
from zhiwei.persistence.events import EventCommand
from zhiwei.persistence.models import CanonicalEvent
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired, tenant_session
from zhiwei.persistence.unit_of_work import CanonicalUnitOfWork, advisory_lock

# 首次使用查重的独立 advisory lock 族（与事件 0x45564E54 / 审计 0x41554454 区分）。
_FIRST_USE_LOCK_NAMESPACE = 0x45504655

_FIRST_USE_ACTOR_REF = "system:models"


def _first_use_schema_registry() -> SchemaRegistry:
    registry = SchemaRegistry()
    registry.register(
        FIRST_USE_EVENT_TYPE, FIRST_USE_PAYLOAD_SCHEMA_VERSION, EndpointFirstUsePayload
    )
    return registry


class CanonicalEndpointFirstUseSink:
    """EndpointFirstUseSink 的生产实现；绑定 session 工厂，每次留痕一个原子事务。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        context: TenantContext,
    ) -> None:
        if context.workspace_id is None:
            raise TenantContextRequired("canonical events require workspace context")
        self._sessions = sessions
        self._context = context
        self._workspace_id: UUID = context.workspace_id

    async def record_first_use(
        self, declaration: EndpointFirstUseDeclaration, *, run_id: UUID
    ) -> bool:
        async with tenant_session(self._sessions, self._context) as session:
            # 先锁后查：并发 run 的「查重→写入」竞态被串行化，后到事务的查重
            # 语句必然看到先到事务已提交的行（READ COMMITTED 新快照）。
            await advisory_lock(
                session, self._workspace_id, namespace=_FIRST_USE_LOCK_NAMESPACE
            )
            seen = await session.scalar(
                select(CanonicalEvent.id).where(
                    CanonicalEvent.organization_id == self._context.organization_id,
                    CanonicalEvent.workspace_id == self._context.workspace_id,
                    CanonicalEvent.event_type == FIRST_USE_EVENT_TYPE,
                    CanonicalEvent.payload["base_url"].as_string() == declaration.base_url,
                )
            )
            if seen is not None:
                return False
            uow = CanonicalUnitOfWork(
                session, self._context, schema_registry=_first_use_schema_registry()
            )
            result = await uow.append_event(
                EventCommand(
                    run_id=run_id,
                    event_type=FIRST_USE_EVENT_TYPE,
                    payload_schema_version=FIRST_USE_PAYLOAD_SCHEMA_VERSION,
                    payload=first_use_payload(declaration),
                    actor_ref=_FIRST_USE_ACTOR_REF,
                    idempotency_key=first_use_idempotency_key(declaration),
                )
            )
            return result.created
