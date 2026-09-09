"""F-R6-09：Audit 事件列表 API——Auditor journey 的产品内可达面（S1 §4）。

GET /api/v1/audit-events：org.read_audit cell（PEP 前置，与 observability 同
款，deny → 403）+ audit_events FORCE RLS + keyset 游标分页（created_at DESC,
id DESC）。可见性即 RLS 语义：org 级行恒可见；ws 级行仅当 actor 携带该
workspace 上下文（跨 ws 审计行需相应 ws 上下文——租户边界在 RLS，不在本
端点；org 全量导出属 break-glass 范畴，不在本面）。响应只含审计元数据列
——审计行没有 payload 正文，payload_digest 指纹即内容边界（脱敏查看语义，
PERMISSIONS §3.1）。

游标格式 `{created_at.isoformat}|{event_id}`：解析失败/naive 时间戳一律 422
invalid cursor（与 api/events.py 的 cursor 拒绝形状一致），不猜测语义。

工厂模式与 api/observability.py 同型。注意：本模块【不用】from __future__
import annotations——endpoint 签名里的 Annotated[ActorContext, Depends(...)]
引用工厂闭包变量，必须在 def 期立即求值才能被 FastAPI 解析。
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import authorize_read, request_trace
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.models import AuditEvent
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, ResourceType

_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MAX = 200


class AuditEventView(BaseModel):
    """单条审计事件的元数据投影（脱敏查看：元数据 + 指纹，无 payload 正文）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    organization_id: UUID
    workspace_id: UUID | None
    action: str
    resource_type: str
    resource_id: UUID
    resource_version: int | None
    actor_ref: str
    effective_identity_ref: str | None
    decision_id: str | None
    policy_revision: str | None
    decision_reason: str | None
    result: str | None
    request_id: str | None
    trace_id: str | None
    payload_digest: str
    created_at: datetime


class AuditEventPage(BaseModel):
    """一页审计事件；耗尽时 next_cursor=None。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: list[AuditEventView]
    next_cursor: str | None


def _encode_cursor(event: AuditEvent) -> str:
    return f"{event.created_at.isoformat()}|{event.id}"


def _decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        raw_created_at, raw_id = value.split("|", 1)
        created_at = datetime.fromisoformat(raw_created_at)
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        event_id = UUID(raw_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid cursor"
        ) from exc
    return created_at, event_id


def _view(row: AuditEvent) -> AuditEventView:
    return AuditEventView(
        id=row.id,
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        resource_version=row.resource_version,
        actor_ref=row.actor_ref,
        effective_identity_ref=row.effective_identity_ref,
        decision_id=row.decision_id,
        policy_revision=row.policy_revision,
        decision_reason=row.decision_reason,
        result=row.result,
        request_id=row.request_id,
        trace_id=row.trace_id,
        payload_digest=row.payload_digest,
        created_at=row.created_at,
    )


def create_audit_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """Audit 事件列表 router（组合期必需 actor 依赖 + session factory + PEP）。"""
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1", tags=["audit"])

    @router.get("/audit-events", response_model=AuditEventPage)
    async def list_audit_events(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=_PAGE_SIZE_MAX)] = _PAGE_SIZE_DEFAULT,
    ) -> AuditEventPage:
        if actor.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="outside tenant scope"
            )
        # 读路径 PEP 前置：deny/OPA 不可达 → 403，不触碰数据；读不写审计。
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=actor.organization_id,
            workspace_id=None,
            policy_type=ResourceType.ORG,
            policy_action=Action.READ_AUDIT,
            resource_id=actor.organization_id,
            trace_id=trace_id,
        )
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            query = select(AuditEvent).where(
                AuditEvent.organization_id == actor.organization_id
            )
            if cursor is not None:
                after_created_at, after_id = _decode_cursor(cursor)
                # keyset：严格小于 (created_at, id) 的续页（DESC 序）；
                # 显式展开为两段谓词（行值比较的方言差异不值得引入）
                query = query.where(
                    (AuditEvent.created_at < after_created_at)
                    | (
                        (AuditEvent.created_at == after_created_at)
                        & (AuditEvent.id < after_id)
                    )
                )
            query = query.order_by(
                AuditEvent.created_at.desc(), AuditEvent.id.desc()
            ).limit(limit + 1)
            rows = (await session.scalars(query)).all()
        has_more = len(rows) > limit
        rows = list(rows[:limit])
        return AuditEventPage(
            events=[_view(row) for row in rows],
            next_cursor=_encode_cursor(rows[-1]) if has_more and rows else None,
        )

    return router
