"""S7 memory 的 PG 持久化仓储（plan Task 1）：tenant-scoped + status CAS。

事务纪律：本仓储绑定调用方事务内的 session（与 persistence.repositories.TenantRepository
同型），不自行 commit/rollback。生命周期转移的状态机**复用域层实现**——把已加载行
播种进 CandidateQueue，由 confirm/supersede/revoke/expire 的域方法计算转移结果，
仓储只负责行映射、租户作用域检查、status CAS 与同事务落账（ledger 可选注入）。
不允许在本模块出现第二套状态机。

并发纪律：写路径先取 workspace 级 advisory xact lock 再查再写（先锁后查，与
persistence.model_first_use 同款），同键并发写被串行化；CAS（WHERE status = 预期）
作为锁之外的第二道防线，失配即 fail closed 抛 MemoryTransitionConflict。

事实源：S7 spec §3（状态机、不原地覆盖）、§4/ADR-009（去重合并、TTL）、
S7 plan Task 1（repositories.py）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.time import ensure_utc, utc_now
from zhiwei.memory.candidates import CandidateQueue, DedupKey, merge_evidence
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    RetentionPolicy,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.events import (
    ACTION_CANDIDATE_CONFIRMED,
    ACTION_CANDIDATE_EXPIRED,
    ACTION_CANDIDATE_MERGED,
    ACTION_CANDIDATE_RECORDED,
    ACTION_RECORD_MERGED,
    ACTION_RECORD_RECORDED,
    ACTION_RECORD_REVOKED,
    ACTION_RECORD_SUPERSEDED,
    MemoryLifecycleLedger,
)
from zhiwei.persistence.models import MemoryRecordRow
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
)
from zhiwei.persistence.unit_of_work import advisory_lock

# 与事件（0x45564E54）/审计（0x41554454）/首用（0x45504655）区分的独立 lock 族。
_MEMORY_LOCK_NAMESPACE = 0x4D454D52

_ACTIVE_STATUSES = (MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED)

_TTL_EXPIRY_ACTOR = "system:memory-ttl-expiry"


class MemoryTransitionConflict(RuntimeError):
    """Raised when a lifecycle transition loses its CAS (concurrent modification)."""


class PgMemoryRepository:
    """memory_records 的租户显式仓储；生命周期转移委托域层 CandidateQueue。"""

    def __init__(
        self,
        session: AsyncSession,
        context: TenantContext | None,
        *,
        ledger: MemoryLifecycleLedger | None = None,
        retention: RetentionPolicy | None = None,
    ) -> None:
        self._session = session
        self._context = context
        self._ledger = ledger
        self._retention = retention or RetentionPolicy()

    async def add_candidate(
        self, record: MemoryRecord, *, now: datetime | None = None
    ) -> MemoryRecord:
        """Upsert a candidate by ADR-009 dedup key（同键合并证据，不新建记录）。

        同键存在活跃记录时：合并证据、状态就高（confirmed 不可降级为 candidate；
        auto-confirm 决策把 candidate 就高为 confirmed），事件如实记录 from/to；
        否则插入新 candidate 行。台账 + 审计同事务落账（注入 ledger 时）。
        """
        if record.status is not MemoryStatus.CANDIDATE:
            raise ValueError(f"only CANDIDATE records can be added, got {record.status}")
        context = self._require_context()
        self._require_scope(record)
        await self._lock(context)
        existing = await self._find_active(context, record.dedup_hash)
        if existing is not None:
            return await self._merge_active(existing, record, now=now)
        await self._insert(record)
        await self._emit(
            record,
            action=ACTION_CANDIDATE_RECORDED,
            from_status=record.status.value,
            to_status=record.status.value,
            actor_ref=_writer_actor(record),
        )
        return record

    async def write_confirmed(
        self, record: MemoryRecord, *, now: datetime | None = None
    ) -> MemoryRecord:
        """Upsert an auto-confirmed record（policy auto_confirm 写入路径）。"""
        if record.status is not MemoryStatus.CONFIRMED:
            raise ValueError(
                f"only CONFIRMED records can be written, got {record.status}"
            )
        context = self._require_context()
        self._require_scope(record)
        await self._lock(context)
        existing = await self._find_active(context, record.dedup_hash)
        if existing is not None:
            return await self._merge_active(existing, record, now=now)
        await self._insert(record)
        await self._emit(
            record,
            action=ACTION_RECORD_RECORDED,
            from_status=record.status.value,
            to_status=record.status.value,
            actor_ref=_writer_actor(record),
        )
        return record

    async def confirm_candidate(
        self,
        dedup_key: DedupKey,
        approver_id: UUID,
        *,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        """candidate → confirmed（steward 确认；状态机与 CAS 复用域语义）。"""
        context = self._require_context()
        await self._lock(context)
        existing = await self._find_active(context, _hash_of(dedup_key))
        if existing is None or existing.status is not MemoryStatus.CANDIDATE:
            return None
        confirmed = _seeded_queue(existing).confirm_candidate(
            DedupKey.from_record(existing), approver_id, now=now
        )
        if confirmed is None:  # pragma: no cover - 状态已在上面校验为 CANDIDATE
            raise MemoryTransitionConflict("candidate vanished before confirm")
        await self._persist_transition(confirmed, expected_status=existing.status)
        await self._emit(
            confirmed,
            action=ACTION_CANDIDATE_CONFIRMED,
            from_status=existing.status.value,
            to_status=confirmed.status.value,
            actor_ref=str(approver_id),
        )
        return confirmed

    async def supersede_record(
        self,
        original_key: DedupKey,
        new_record: MemoryRecord,
        *,
        now: datetime | None = None,
    ) -> tuple[MemoryRecord, MemoryRecord]:
        """纠正：原记录 → superseded（记录 superseded_by），新记录以 confirmed 插入。

        新键若与其他活跃记录冲突则 fail closed（不静默覆盖既有记录）。
        """
        context = self._require_context()
        self._require_scope(new_record)
        await self._lock(context)
        existing = await self._find_active(context, _hash_of(original_key))
        if existing is None:
            raise KeyError("no record found for dedup key")
        superseded, confirmed = _seeded_queue(existing).supersede_record(
            DedupKey.from_record(existing), new_record, now=now
        )
        await self._persist_transition(superseded, expected_status=existing.status)
        await self._emit(
            superseded,
            action=ACTION_RECORD_SUPERSEDED,
            from_status=existing.status.value,
            to_status=superseded.status.value,
            actor_ref=_writer_actor(new_record),
        )
        if confirmed.dedup_hash != existing.dedup_hash:
            collision = await self._find_active(context, confirmed.dedup_hash)
            if collision is not None:
                raise MemoryTransitionConflict(
                    "superseding record collides with an active record at the new key"
                )
        await self._insert(confirmed)
        await self._emit(
            confirmed,
            action=ACTION_RECORD_RECORDED,
            from_status=confirmed.status.value,
            to_status=confirmed.status.value,
            actor_ref=_writer_actor(new_record),
        )
        return superseded, confirmed

    async def revoke_record(
        self,
        dedup_key: DedupKey,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        """candidate/confirmed → revoked（tombstone；终态不可再转移）。"""
        context = self._require_context()
        await self._lock(context)
        existing = await self._find_active(context, _hash_of(dedup_key))
        if existing is None:
            return None
        revoked = _seeded_queue(existing).revoke_record(
            DedupKey.from_record(existing), reason, now=now
        )
        if revoked is None:  # pragma: no cover - 活跃记录必可 revoke
            raise MemoryTransitionConflict("active record refused revoke")
        await self._persist_transition(revoked, expected_status=existing.status)
        await self._emit(
            revoked,
            action=ACTION_RECORD_REVOKED,
            from_status=existing.status.value,
            to_status=revoked.status.value,
            actor_ref=_writer_actor(revoked),
            reason=reason,
        )
        return revoked

    async def expire_candidates(
        self, now: datetime, *, limit: int | None = None
    ) -> list[MemoryRecord]:
        """TTL 过期：candidate → expired + tombstone（ADR-009 自动过期）。"""
        context = self._require_context()
        await self._lock(context)
        now_utc = ensure_utc(now)
        cutoff = now_utc - self._retention.candidate_ttl
        statement = (
            select(MemoryRecordRow)
            .where(
                MemoryRecordRow.organization_id == context.organization_id,
                MemoryRecordRow.workspace_id == context.workspace_id,
                MemoryRecordRow.status == MemoryStatus.CANDIDATE.value,
                MemoryRecordRow.created_at < cutoff,
            )
            .order_by(MemoryRecordRow.created_at)
            .with_for_update()
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = list((await self._session.scalars(statement)).all())
        if not rows:
            return []
        records = [row_to_record(row) for row in rows]
        # 状态机复用：把过期窗口内的 candidate 播种进域层队列，由域方法执行转移
        seeded = CandidateQueue(
            records={DedupKey.from_record(r).as_tuple(): r for r in records},
            retention=self._retention,
        )
        expired = seeded.expire_candidates(now_utc)
        if len(expired) != len(records):  # pragma: no cover - SQL 预过滤与域判定一致
            raise MemoryTransitionConflict("TTL expiry diverged from domain retention")
        for original, expired_record in zip(records, expired, strict=True):
            await self._persist_transition(expired_record, expected_status=original.status)
            await self._emit(
                expired_record,
                action=ACTION_CANDIDATE_EXPIRED,
                from_status=original.status.value,
                to_status=expired_record.status.value,
                actor_ref=_TTL_EXPIRY_ACTOR,
            )
        return expired

    async def get_record(self, dedup_key: DedupKey) -> MemoryRecord | None:
        """按 dedup 键取记录：活跃记录优先，否则取最近更新的一条（含终态历史）。"""
        context = self._require_context()
        statement = (
            select(MemoryRecordRow)
            .where(
                MemoryRecordRow.organization_id == context.organization_id,
                MemoryRecordRow.workspace_id == context.workspace_id,
                MemoryRecordRow.dedup_hash == _hash_of(dedup_key),
            )
            .order_by(
                MemoryRecordRow.status.in_([s.value for s in _ACTIVE_STATUSES]).desc(),
                MemoryRecordRow.updated_at.desc(),
            )
            .limit(1)
        )
        row = (await self._session.scalars(statement)).first()
        return None if row is None else row_to_record(row)

    async def get_by_id(self, record_id: UUID) -> MemoryRecord | None:
        """按 id 取记录（租户作用域内）。"""
        context = self._require_context()
        row = await self._session.scalar(
            select(MemoryRecordRow).where(
                MemoryRecordRow.organization_id == context.organization_id,
                MemoryRecordRow.workspace_id == context.workspace_id,
                MemoryRecordRow.id == record_id,
            )
        )
        return None if row is None else row_to_record(row)

    # ── internals ─────────────────────────────────────────────────────────

    def _require_context(self) -> TenantContext:
        if self._context is None:
            raise TenantContextRequired("organization context is required")
        if self._context.workspace_id is None:
            raise TenantContextRequired("memory records require workspace context")
        return self._context

    def _require_scope(self, record: MemoryRecord) -> None:
        context = self._require_context()
        if record.organization_id != context.organization_id:
            raise TenantScopeError("memory record does not match tenant context")
        if record.workspace_id != context.workspace_id:
            raise TenantScopeError("memory record does not match workspace context")

    async def _lock(self, context: TenantContext) -> None:
        assert context.workspace_id is not None  # _require_context 已收窄
        await advisory_lock(
            self._session, context.workspace_id, namespace=_MEMORY_LOCK_NAMESPACE
        )

    async def _find_active(
        self, context: TenantContext, dedup_hash: str
    ) -> MemoryRecord | None:
        assert context.workspace_id is not None  # _require_context 已收窄
        row = await self._session.scalar(
            select(MemoryRecordRow)
            .where(
                MemoryRecordRow.organization_id == context.organization_id,
                MemoryRecordRow.workspace_id == context.workspace_id,
                MemoryRecordRow.dedup_hash == dedup_hash,
                MemoryRecordRow.status.in_([s.value for s in _ACTIVE_STATUSES]),
            )
            .with_for_update()
        )
        return None if row is None else row_to_record(row)

    async def _merge_active(
        self,
        existing: MemoryRecord,
        incoming: MemoryRecord,
        *,
        now: datetime | None,
    ) -> MemoryRecord:
        """同键活跃记录的证据合并；状态就高（confirmed 不降级，auto-confirm 可确认 candidate）。"""
        now_ = ensure_utc(now) if now else utc_now()
        merged = merge_evidence(existing, incoming, now=now_)
        if (
            incoming.status is MemoryStatus.CONFIRMED
            and merged.status is not MemoryStatus.CONFIRMED
        ):
            merged = merged.model_copy(update={"status": MemoryStatus.CONFIRMED})
        await self._persist_transition(merged, expected_status=existing.status)
        await self._emit(
            merged,
            action=ACTION_RECORD_MERGED
            if merged.status is MemoryStatus.CONFIRMED
            else ACTION_CANDIDATE_MERGED,
            from_status=existing.status.value,
            to_status=merged.status.value,
            actor_ref=_writer_actor(incoming),
        )
        return merged

    async def _insert(self, record: MemoryRecord) -> None:
        self._session.add(record_to_row(record))
        await self._session.flush()

    async def _persist_transition(
        self, record: MemoryRecord, *, expected_status: MemoryStatus
    ) -> None:
        """status CAS 更新：只写授权列；rowcount 失配即并发冲突（fail closed）。"""
        values: dict[str, Any] = {
            "status": record.status.value,
            "updated_at": record.updated_at,
            "tombstone": record.tombstone,
            "confidence": record.confidence,
            "observed_at": record.observed_at,
            "source_refs": [ref.model_dump() for ref in record.source_refs],
        }
        if record.approver_ref is not None:
            values["approver_ref"] = record.approver_ref
        if record.superseded_by is not None:
            values["superseded_by"] = record.superseded_by
        if record.revoked_reason is not None:
            values["revoked_reason"] = record.revoked_reason
        statement = (
            update(MemoryRecordRow)
            .where(
                MemoryRecordRow.organization_id == record.organization_id,
                MemoryRecordRow.workspace_id == record.workspace_id,
                MemoryRecordRow.id == record.id,
                MemoryRecordRow.status == expected_status.value,
            )
            .values(**values)
            .returning(MemoryRecordRow.id)
        )
        updated_id = await self._session.scalar(statement)
        if updated_id is None:
            raise MemoryTransitionConflict(
                f"memory record {record.id} transition lost a race "
                f"(expected status {expected_status.value})"
            )

    async def _emit(
        self,
        record: MemoryRecord,
        *,
        action: str,
        from_status: str,
        to_status: str,
        actor_ref: str,
        reason: str | None = None,
    ) -> None:
        if self._ledger is None:
            return
        await self._ledger.record_transition(
            record,
            action=action,
            from_status=from_status,
            to_status=to_status,
            actor_ref=actor_ref,
            reason=reason,
        )


def _writer_actor(record: MemoryRecord) -> str:
    return f"memory:write:{record.author_ref}"


def _hash_of(dedup_key: DedupKey) -> str:
    """DedupKey → 内容寻址 hash（与 MemoryRecord.dedup_hash 同一公式）。"""
    return digest_bytes(canonical_json(list(dedup_key.as_tuple())))


def _seeded_queue(record: MemoryRecord) -> CandidateQueue:
    """把已加载记录播种进域层队列——状态机唯一实现复用点。"""
    return CandidateQueue(records={DedupKey.from_record(record).as_tuple(): record})


def row_to_record(row: MemoryRecordRow) -> MemoryRecord:
    """memory_records 行 → 域模型（字段以 DATA_MODEL §6 为准）。"""
    return MemoryRecord(
        id=row.id,
        version=row.version,
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        scope=MemoryScope(row.scope),
        scope_subject_id=row.scope_subject_id,
        type=MemoryType(row.type),
        subject=row.subject,
        key=row.key,
        canonical_value=row.canonical_value,
        source_refs=tuple(SourceRef.model_validate(ref) for ref in row.source_refs),
        observed_at=row.observed_at,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        confidence=row.confidence,
        sensitivity=SensitivityLevel(row.sensitivity),
        status=MemoryStatus(row.status),
        author_ref=row.author_ref,
        approver_ref=row.approver_ref,
        conflict_refs=tuple(UUID(item) for item in row.conflict_refs),
        retention_policy=row.retention_policy,
        allowed_profile_refs=tuple(str(item) for item in row.allowed_profile_refs),
        acl_version=row.acl_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        superseded_by=row.superseded_by,
        revoked_reason=row.revoked_reason,
        tombstone=row.tombstone,
        schema_version=row.schema_version,
    )


def record_to_row(record: MemoryRecord) -> MemoryRecordRow:
    """域模型 → memory_records 行（dedup_hash 为 ADR-009 内容寻址键）。"""
    return MemoryRecordRow(
        id=record.id if record.id != UUID(int=0) else uuid4(),
        version=record.version,
        organization_id=record.organization_id,
        workspace_id=record.workspace_id,
        scope=record.scope.value,
        scope_subject_id=record.scope_subject_id,
        type=record.type.value,
        subject=record.subject,
        key=record.key,
        canonical_value=record.canonical_value,
        source_refs=[ref.model_dump() for ref in record.source_refs],
        observed_at=ensure_utc(record.observed_at),
        valid_from=ensure_utc(record.valid_from) if record.valid_from else None,
        valid_to=ensure_utc(record.valid_to) if record.valid_to else None,
        confidence=record.confidence,
        sensitivity=record.sensitivity.value,
        status=record.status.value,
        author_ref=record.author_ref,
        approver_ref=record.approver_ref,
        conflict_refs=[str(ref) for ref in record.conflict_refs],
        retention_policy=record.retention_policy,
        allowed_profile_refs=list(record.allowed_profile_refs),
        acl_version=record.acl_version,
        superseded_by=record.superseded_by,
        revoked_reason=record.revoked_reason,
        tombstone=record.tombstone,
        dedup_hash=record.dedup_hash,
        schema_version=record.schema_version,
        created_at=ensure_utc(record.created_at),
        updated_at=ensure_utc(record.updated_at),
    )
