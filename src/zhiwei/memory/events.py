"""S7 memory 生命周期事件与审计落账（plan Task 1/2）。

两个落账通道，均在**同一事务**内随记录转移提交：

- Run 内写入（Runtime WriteMemoryCandidate → Memory Activity）：candidate/refusal
  走 `CanonicalUnitOfWork` 的 canonical event 通道（canonical_events + audit + outbox），
  payload schema 在本模块注册进 SchemaRegistry，fail closed（未知 schema 拒绝）。
- Run 外生命周期转移（steward confirm/reject、纠正 supersede、TTL expire）：
  canonical event 是 Run 作用域的，不能伪造 run_id——走 `memory_lifecycle_events`
  台账 + `append_audit_chain` 审计链（append_audit_chain 是 canonical 路径共用的
  唯一审计追加实现），同样与记录 UPDATE 同事务提交。

事实源：S7 spec §3（status 状态机）、§7（candidate/refusal canonical event）、
ADR-009（去重合并）、plan Task 1/2（events.py、同事务 event/audit）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.canonical import digest
from zhiwei.contracts.envelope import SchemaRegistry
from zhiwei.contracts.time import utc_now
from zhiwei.memory.domain import MemoryRecord
from zhiwei.persistence.events import AuditEventData
from zhiwei.persistence.models import MemoryLifecycleEventRow
from zhiwei.persistence.tenant import TenantContext
from zhiwei.persistence.unit_of_work import append_audit_chain

CANDIDATE_RECORDED_EVENT = "memory.candidate.recorded"
REFUSAL_EVENT = "memory.write.refused"

PAYLOAD_SCHEMA_VERSION = 1

# 生命周期台账 action 词汇表；幂等由 (record_id, action, payload_digest) 统一承担
ACTION_CANDIDATE_RECORDED = "candidate.recorded"
ACTION_CANDIDATE_MERGED = "candidate.merged"
ACTION_RECORD_RECORDED = "record.recorded"
ACTION_RECORD_MERGED = "record.merged"
ACTION_CANDIDATE_CONFIRMED = "candidate.confirmed"
ACTION_RECORD_SUPERSEDED = "record.superseded"
ACTION_RECORD_REVOKED = "record.revoked"
ACTION_CANDIDATE_EXPIRED = "candidate.expired"


class MemoryCandidateEventPayload(BaseModel):
    """Run 内 candidate/auto-confirm 写入的 canonical event payload。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID
    organization_id: UUID
    workspace_id: UUID
    scope: str
    scope_subject_id: UUID
    type: str
    subject: str
    key: str
    dedup_hash: str
    canonical_value: str
    sensitivity: str
    status: str
    decision: str
    confidence: float
    observed_at: str
    source_refs: list[dict[str, str]] = []
    author_ref: UUID


class MemoryRefusalEventPayload(BaseModel):
    """Run 内 policy 拒绝写入的 canonical event payload（不建记录）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: UUID
    workspace_id: UUID
    scope: str
    type: str
    subject: str
    key: str
    decision: str
    reason: str


def candidate_payload(record: MemoryRecord, *, decision: str) -> dict[str, Any]:
    """Build the canonical event payload for a candidate/confirmed write."""
    return {
        "record_id": record.id,
        "organization_id": record.organization_id,
        "workspace_id": record.workspace_id,
        "scope": record.scope.value,
        "scope_subject_id": record.scope_subject_id,
        "type": record.type.value,
        "subject": record.subject,
        "key": record.key,
        "dedup_hash": record.dedup_hash,
        "canonical_value": record.canonical_value,
        "sensitivity": record.sensitivity.value,
        "status": record.status.value,
        "decision": decision,
        "confidence": record.confidence,
        "observed_at": record.observed_at.isoformat(),
        "source_refs": [ref.model_dump() for ref in record.source_refs],
        "author_ref": record.author_ref,
    }


def refusal_payload(
    record: MemoryRecord, *, decision: str, reason: str
) -> dict[str, Any]:
    """Build the canonical event payload for a policy refusal."""
    return {
        "organization_id": record.organization_id,
        "workspace_id": record.workspace_id,
        "scope": record.scope.value,
        "type": record.type.value,
        "subject": record.subject,
        "key": record.key,
        "decision": decision,
        "reason": reason,
    }


def candidate_idempotency_key(record: MemoryRecord) -> str:
    """Run 作用域幂等键：同 record 重试不产生第二行（canonical_events 唯一约束）。"""
    return f"{CANDIDATE_RECORDED_EVENT}:{record.id}"


def refusal_idempotency_key(payload: dict[str, Any]) -> str:
    """Run 作用域幂等键：同输入的重复拒绝在事件链上只落一行。

    经 payload model 归一（UUID 等类型 → JSON 安全表示）再取 digest，保证与
    canonical event 校验后的 payload 逐字节一致。
    """
    model = MemoryRefusalEventPayload.model_validate(payload)
    return f"{REFUSAL_EVENT}:{digest(model.model_dump(mode='json'))}"


def memory_event_schema_registry() -> SchemaRegistry:
    """Memory 写入路径的显式 schema registry（fail closed：未注册 schema 拒绝）。"""
    registry = SchemaRegistry()
    registry.register(
        CANDIDATE_RECORDED_EVENT, PAYLOAD_SCHEMA_VERSION, MemoryCandidateEventPayload
    )
    registry.register(REFUSAL_EVENT, PAYLOAD_SCHEMA_VERSION, MemoryRefusalEventPayload)
    return registry


class MemoryLifecycleLedger:
    """生命周期转移台账写入器：memory_lifecycle_events 行 + 审计链，同事务。

    绑定调用方事务内的 session——不自行 commit/rollback，记录转移与台账/审计
    由调用方（PgMemoryRepository 的事务持有者）决定整体命运。
    """

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        if context.workspace_id is None:
            raise ValueError("memory lifecycle ledger requires workspace context")
        self._session = session
        self._context = context
        self._workspace_id = context.workspace_id

    async def record_transition(
        self,
        record: MemoryRecord,
        *,
        action: str,
        from_status: str,
        to_status: str,
        actor_ref: str,
        reason: str | None = None,
    ) -> MemoryLifecycleEventRow:
        """Append one lifecycle row + audit chain entry inside the caller's transaction.

        幂等键 (record_id, action, payload_digest)：一次性转移重放与同证据合并重试
        命中既有行即原样返回，不重复落账、不重复追加审计（唯一约束是纵深防御
        第二层）。payload 不含 updated_at/created_at——重试的 no-op 合并保持同
        digest，天然幂等。
        """
        payload = {
            "record_id": str(record.id),
            "organization_id": str(record.organization_id),
            "workspace_id": str(record.workspace_id),
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "dedup_hash": record.dedup_hash,
            "reason": reason,
            "record_digest": _record_digest(record),
        }
        payload_digest = digest(payload)
        existing = await self._find(record.id, action, payload_digest)
        if existing is not None:
            return existing

        row = MemoryLifecycleEventRow(
            id=uuid4(),
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            record_id=record.id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            actor_ref=actor_ref,
            reason=reason,
            payload=payload,
            payload_digest=payload_digest,
            schema_version=1,
            created_at=utc_now(),
        )
        self._session.add(row)
        await append_audit_chain(
            self._session,
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            data=AuditEventData(
                organization_id=self._context.organization_id,
                workspace_id=self._workspace_id,
                action=f"memory.{action}",
                resource_type="memory_record",
                resource_id=record.id,
                actor_ref=actor_ref,
                payload_digest=payload_digest,
                previous_event_digest="",
                event_digest="",
            ),
        )
        await self._session.flush()
        return row

    async def _find(
        self, record_id: UUID, action: str, payload_digest: str
    ) -> MemoryLifecycleEventRow | None:
        stmt = select(MemoryLifecycleEventRow).where(
            MemoryLifecycleEventRow.organization_id == self._context.organization_id,
            MemoryLifecycleEventRow.workspace_id == self._workspace_id,
            MemoryLifecycleEventRow.record_id == record_id,
            MemoryLifecycleEventRow.action == action,
            MemoryLifecycleEventRow.payload_digest == payload_digest,
        )
        return (await self._session.scalars(stmt)).first()


def _record_digest(record: MemoryRecord) -> str:
    """Content digest of the record state（排除时间戳：重试 no-op 合并同 digest）。"""
    return digest(
        record.model_dump(mode="json", exclude={"created_at", "updated_at"})
    )
