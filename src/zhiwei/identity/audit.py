"""S1-T4 typed audit records：结构化 allow/deny/mutation 审计。

冻结契约（总设计 §9.4、PERMISSIONS §14、docs/handoffs/s1-t4-design-gap.md）：
- AuditRecord 固定 audit_schema_version=2，覆盖 actor/effective identity（分字段）、
  org/workspace、resource/version、action、decision/revision/reason、result、
  request/trace id、payload digest；
- decision_id/policy_revision 允许 NULL：fail-closed 本地拒绝绝不伪造 OPA metadata；
- append_audit 在调用方当前事务内追加 audit 行 + 同事务 outbox（与业务 mutation
  同提交或同回滚）；
- append_fail_closed_audit 为 denied mutation 开独立事务写审计（拒绝也必可审计）；
- digest/outbox 设施复用 persistence 既有实现（append_audit_chain），不复制。
"""

from __future__ import annotations

import re
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.persistence.events import AuditEventData
from zhiwei.persistence.models import AuditEvent, OutboxMessage
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
    tenant_session,
)
from zhiwei.persistence.unit_of_work import append_audit_chain

_OUTBOX_TOPIC_AUDIT_DECISION = "audit.decision"

# 与 0006 ck_audit_events_v2_payload_digest 逐字一致（digest_bytes 输出形状：
# "sha256:" + 64 位小写 hex；Pydantic 与 PostgreSQL 同款边界，direct INSERT 不可绕过）。
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class AuditRecord(BaseModel):
    """一条结构化审计命令；S1 无 delegation 时 effective_identity_ref = actor_ref。

    metadata 配对（与 0006 CHECK 逐条一致，repair addendum §3.1.6）：
    - allowed → decision_id 与 policy_revision 必须同时非空；
    - denied（OPA deny）→ 同时非空；denied（本地拒绝）→ 同时 NULL；
    - failed → 同时 NULL；禁止只有 decision_id 或只有 policy_revision。
    resource_version=0 是「unknown（mutation 未应用）」哨兵，只在 denied/failed 路径使用。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: UUID
    workspace_id: UUID | None
    action: str = Field(min_length=1, max_length=128)
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: UUID
    resource_version: int = Field(ge=0)
    actor_ref: str = Field(min_length=1, max_length=255)
    effective_identity_ref: str = Field(min_length=1, max_length=255)
    decision_id: str | None = Field(default=None, max_length=2048)
    policy_revision: str | None = Field(default=None, max_length=2048)
    decision_reason: str = Field(min_length=1, max_length=2048)
    result: Literal["allowed", "denied", "failed"]
    request_id: str = Field(min_length=1, max_length=2048)
    trace_id: str = Field(min_length=1, max_length=2048)
    payload_digest: str = Field(pattern=_SHA256_DIGEST_RE.pattern)

    @model_validator(mode="after")
    def _decision_metadata_pairing(self) -> Self:
        has_decision_id = self.decision_id is not None
        has_revision = self.policy_revision is not None
        if has_decision_id != has_revision:
            raise ValueError(
                "decision_id and policy_revision must be present together or not at all"
            )
        if self.result == "allowed" and not (has_decision_id and has_revision):
            raise ValueError("allowed audits require decision_id and policy_revision")
        if self.result == "failed" and (has_decision_id or has_revision):
            raise ValueError("failed audits must not carry decision_id or policy_revision")
        return self


async def append_audit(
    session: AsyncSession, context: TenantContext, record: AuditRecord
) -> AuditEvent:
    """在当前事务内追加结构化审计行 + 同事务 outbox；由调用方决定 commit/rollback。

    record 的 org/workspace 必须与 context 完全一致（fail closed，不静默改写 scope）。
    """
    if record.organization_id != context.organization_id:
        raise TenantScopeError("audit record organization does not match tenant context")
    if record.workspace_id != context.workspace_id:
        raise TenantScopeError("audit record workspace does not match tenant context")
    row = await append_audit_chain(
        session,
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        data=AuditEventData(
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            actor_ref=record.actor_ref,
            payload_digest=record.payload_digest,
            previous_event_digest="",
            event_digest="",
            audit_schema_version=2,
            effective_identity_ref=record.effective_identity_ref,
            resource_version=record.resource_version,
            decision_id=record.decision_id,
            policy_revision=record.policy_revision,
            decision_reason=record.decision_reason,
            result=record.result,
            request_id=record.request_id,
            trace_id=record.trace_id,
        ),
    )
    session.add(
        OutboxMessage(
            id=uuid4(),
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            topic=_OUTBOX_TOPIC_AUDIT_DECISION,
            event_key=str(row.id),
            payload={
                "action": record.action,
                "resource_type": record.resource_type,
                "resource_id": str(record.resource_id),
                "resource_version": record.resource_version,
                "result": record.result,
                "decision_id": record.decision_id,
                "policy_revision": record.policy_revision,
                "decision_reason": record.decision_reason,
                "request_id": record.request_id,
                "trace_id": record.trace_id,
                "event_digest": row.event_digest,
            },
            status="pending",
            attempts=0,
            available_at=row.created_at,
            schema_version=1,
            created_at=row.created_at,
        )
    )
    return row


async def append_fail_closed_audit(
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
    record: AuditRecord,
) -> AuditEvent:
    """denied mutation 的独立 fail-closed 审计事务：拒绝记录绝不被业务失败吞掉。

    与业务 mutation 无关：本函数自开事务（SET LOCAL tenant context）写 audit + outbox
    并提交；调用方在此之前必须已经拒绝业务写入。
    """
    if context is None:
        raise TenantContextRequired("organization context is required")
    async with tenant_session(sessions, context) as session:
        return await append_audit(session, context, record)
