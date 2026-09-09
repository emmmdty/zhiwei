"""S5 Source Ledger 的 PG 持久化仓储（F-R6-04，P2b）。

ledger.py 域不变量的异步持久版：幂等 register、version_seq 递增、重复
digest 拒绝（DuplicateVersionError，数据面唯一索引兜底）、latest 仅 ACTIVE、
stale/revoke 转移（tombstone）。事务纪律与 memory/repositories 同型：绑定
调用方事务内的 session，不自行 commit/rollback。

SyncIntent DELETE/REVOKE 消费（apply_delete_revoke）：REVOKE = 对象全部活跃
版本失权（spec §3 delete/revoke 优先——源撤销后版本不再可检索，ADR-006 可见
性拒绝）；DELETE = 全版本 tombstone。转移幂等（WHERE state <> 'revoked'，
重复 apply 零新审计），审计经 append_audit_chain 同事务落账。

ObjectStore（内容字节）不在此层：digest 寻址的 immutable key 由
object_store/ports 承担，本仓储只管 ledger 元数据（spec §3 内容事实分界）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.time import ensure_utc, utc_now
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceObject,
    SourceVersion,
    SourceVersionState,
)
from zhiwei.knowledge.ledger import (
    DuplicateVersionError,
    ObjectNotFoundError,
    VersionNotFoundError,
)
from zhiwei.knowledge.sync import SyncEventType, SyncIntent
from zhiwei.persistence.events import AuditEventData
from zhiwei.persistence.models import SourceObjectRow, SourceVersionRow
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
)
from zhiwei.persistence.unit_of_work import advisory_lock, append_audit_chain

# 与事件（0x45564E54）/审计（0x41554454）/memory（0x4D454D52）区分的独立锁族：
# create_version 的 seq 分配与 apply_delete_revoke 的失权快照共用同一串行化点
# （FOR UPDATE 行锁需要表级 UPDATE 授权，与列级最小授权契约冲突——advisory
# xact lock 按 (org, ws, object) 串行化，事务结束自动释放）。
_SOURCE_LEDGER_LOCK_NAMESPACE = 0x534C4544


def object_to_row(obj: SourceObject) -> SourceObjectRow:
    return SourceObjectRow(
        id=obj.id if obj.id != UUID(int=0) else uuid4(),
        organization_id=obj.organization_id,
        workspace_id=obj.workspace_id,
        source_type=obj.source_type,
        acl=obj.acl.model_dump(),
        classification=obj.classification.value,
        metadata_=obj.metadata,
        schema_version=1,
    )


def row_to_object(row: SourceObjectRow) -> SourceObject:
    return SourceObject(
        id=row.id,
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        source_type=row.source_type,
        acl=ACLSnapshot.model_validate(row.acl),
        classification=Classification(row.classification),
        metadata=row.metadata_,
    )


def version_to_row(version: SourceVersion, context: TenantContext) -> SourceVersionRow:
    """域版本行化：SourceVersion 契约无租户字段（经 object 归属），PG 行的
    org/ws 列由租户上下文提供（RLS + 复合 FK 需要）。"""
    return SourceVersionRow(
        id=version.id if version.id != UUID(int=0) else uuid4(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        source_object_id=version.source_object_id,
        version_seq=version.version_seq,
        locator=version.locator.model_dump(),
        content_digest=version.content_digest,
        observed_at=ensure_utc(version.observed_at),
        valid_at=ensure_utc(version.valid_at),
        acl=version.acl.model_dump(),
        classification=version.classification.value,
        state=version.state.value,
        parent_version_id=version.parent_version_id,
        tombstone=version.tombstone,
        connector_version=version.connector_version,
        parser_version=version.parser_version,
        index_version=version.index_version,
        metadata_=version.metadata,
        schema_version=version.schema_version,
    )


def row_to_version(row: SourceVersionRow) -> SourceVersion:
    return SourceVersion(
        id=row.id,
        source_object_id=row.source_object_id,
        version_seq=row.version_seq,
        locator=Locator.model_validate(row.locator),
        content_digest=row.content_digest,
        observed_at=row.observed_at,
        valid_at=row.valid_at,
        acl=ACLSnapshot.model_validate(row.acl),
        classification=Classification(row.classification),
        state=SourceVersionState(row.state),
        parent_version_id=row.parent_version_id,
        tombstone=row.tombstone,
        connector_version=row.connector_version,
        parser_version=row.parser_version,
        index_version=row.index_version,
        metadata=row.metadata_,
        schema_version=row.schema_version,
    )


@dataclass(slots=True)
class DeleteRevokeResult:
    """apply_delete_revoke 的结果（幂等重放返回空集）。"""

    revoked_version_ids: set[UUID] = field(default_factory=set)


class ApplyIntentError(ValueError):
    """DELETE/REVOKE 消费方不处理的 intent 类型（fail closed，不静默吞）。"""


class ApplyIntentInput(BaseModel):
    """SourceLedgerActivity 的输入（worker 直调形态，payload 为 JSON 安全类型）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str
    connector: str
    source_object_id: str
    event_id: str
    payload: dict[str, Any] = {}
    idempotency_key: str
    organization_id: str
    workspace_id: str


class ApplyIntentOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: bool
    revoked_version_ids: list[str] = []


class PgSourceLedger:
    """source_objects/source_versions 的租户显式仓储 + SyncIntent 消费。"""

    def __init__(self, session: AsyncSession, context: TenantContext | None) -> None:
        self._session = session
        self._context = context

    async def register_object(self, obj: SourceObject) -> SourceObject:
        """注册对象（同 id 幂等——重复 register 返回既有行，不报错）。"""
        context = self._require_context()
        self._require_scope(obj.organization_id, obj.workspace_id)
        existing = await self._session.scalar(
            select(SourceObjectRow).where(
                SourceObjectRow.organization_id == context.organization_id,
                SourceObjectRow.workspace_id == context.workspace_id,
                SourceObjectRow.id == obj.id,
            )
        )
        if existing is not None:
            return row_to_object(existing)
        row = object_to_row(obj)
        self._session.add(row)
        await self._session.flush()
        return row_to_object(row)

    async def get_object(self, object_id: UUID) -> SourceObject:
        obj = await self._find_object(object_id)
        if obj is None:
            raise ObjectNotFoundError(f"SourceObject {object_id} not found")
        return obj

    async def create_version(
        self,
        object_id: UUID,
        *,
        locator: Locator,
        content_digest: str,
        observed_at: datetime,
        valid_at: datetime,
        acl: ACLSnapshot | None = None,
        classification: Classification | None = None,
        parent_version_id: UUID | None = None,
        connector_version: str = "1",
        parser_version: str = "1",
        index_version: str = "1",
        metadata: dict | None = None,
    ) -> SourceVersion:
        """创建不可变新版本；重复 digest 抛 DuplicateVersionError（域错误
        词表与内存版一致）。对象级 advisory xact lock 串行化 seq 分配与
        DELETE/REVOKE 消费（spec §3 delete/revoke 优先：失权快照后提交的
        物化不得复活）。"""
        context = self._require_context()
        await self._lock(context, object_id)
        obj = await self.get_object(object_id)
        seq = await self._next_seq(object_id)
        version = SourceVersion(
            id=uuid4(),
            source_object_id=object_id,
            version_seq=seq,
            locator=locator,
            content_digest=content_digest,
            observed_at=ensure_utc(observed_at),
            valid_at=ensure_utc(valid_at),
            acl=acl or obj.acl,
            classification=classification or obj.classification,
            state=SourceVersionState.ACTIVE,
            parent_version_id=parent_version_id,
            connector_version=connector_version,
            parser_version=parser_version,
            index_version=index_version,
            metadata=metadata or {},
        )
        self._session.add(version_to_row(version, context))
        try:
            await self._session.flush()
        except Exception as exc:
            if _is_digest_unique_violation(exc):
                raise DuplicateVersionError(
                    f"Object {object_id} already has version with digest {content_digest}"
                ) from exc
            raise  # 其他唯一冲突（seq 撞号等）原样 fail closed，不误报语义
        return version

    async def get_version(self, version_id: UUID) -> SourceVersion:
        version = await self._find_version(version_id)
        if version is None:
            raise VersionNotFoundError(f"SourceVersion {version_id} not found")
        return version

    async def list_versions(self, object_id: UUID) -> list[SourceVersion]:
        await self.get_object(object_id)
        context = self._require_context()
        statement = (
            select(SourceVersionRow)
            .where(
                SourceVersionRow.organization_id == context.organization_id,
                SourceVersionRow.workspace_id == context.workspace_id,
                SourceVersionRow.source_object_id == object_id,
            )
            .order_by(SourceVersionRow.version_seq)
        )
        rows = list((await self._session.scalars(statement)).all())
        return [row_to_version(row) for row in rows]

    async def mark_stale(self, version_id: UUID) -> SourceVersion:
        """active → stale；revoked/tombstone 拒绝（域语义与内存版一致）。"""
        version = await self.get_version(version_id)
        if version.state is SourceVersionState.REVOKED:
            raise ValueError("Cannot mark a revoked version as stale")
        if version.tombstone:
            raise ValueError("Cannot mark a tombstone version as stale")
        return await self._transition(
            version, target=SourceVersionState.STALE, tombstone=False
        )

    async def revoke_version(self, version_id: UUID) -> SourceVersion:
        """版本失权（REVOKED + tombstone）；重复 revoke 幂等。"""
        version = await self.get_version(version_id)
        return await self._transition(
            version, target=SourceVersionState.REVOKED, tombstone=True
        )

    async def latest_version(self, object_id: UUID) -> SourceVersion | None:
        await self.get_object(object_id)
        context = self._require_context()
        row = await self._session.scalar(
            select(SourceVersionRow)
            .where(
                SourceVersionRow.organization_id == context.organization_id,
                SourceVersionRow.workspace_id == context.workspace_id,
                SourceVersionRow.source_object_id == object_id,
                SourceVersionRow.state == SourceVersionState.ACTIVE.value,
            )
            .order_by(SourceVersionRow.version_seq.desc())
            .limit(1)
        )
        return None if row is None else row_to_version(row)

    async def get_current_acls(
        self, object_ids: list[UUID]
    ) -> dict[UUID, ACLSnapshot]:
        """当前 ACL 批量查询（ADR-006 失权投影的 current_acl 权威来源）。

        只返回在租户作用域内存在的对象；缺席键 = 不可判定（调用方 fail
        closed 处理）。"""
        context = self._require_context()
        if not object_ids:
            return {}
        rows = list(
            (
                await self._session.scalars(
                    select(SourceObjectRow).where(
                        SourceObjectRow.organization_id == context.organization_id,
                        SourceObjectRow.workspace_id == context.workspace_id,
                        SourceObjectRow.id.in_(object_ids),
                    )
                )
            ).all()
        )
        return {row.id: ACLSnapshot.model_validate(row.acl) for row in rows}

    # ── 运营生命周期 / ACL（source_objects 的可变列；内容列仍不可变）──────

    async def list_objects(self) -> list[SourceObject]:
        """租户内全部 source 对象（list 端点；内容列之外的运营状态另行查询）。"""
        context = self._require_context()
        statement = (
            select(SourceObjectRow)
            .where(
                SourceObjectRow.organization_id == context.organization_id,
                SourceObjectRow.workspace_id == context.workspace_id,
            )
            .order_by(SourceObjectRow.created_at, SourceObjectRow.id)
        )
        rows = list((await self._session.scalars(statement)).all())
        return [row_to_object(row) for row in rows]

    async def get_lifecycle_status(self, object_id: UUID) -> str | None:
        """运营状态（active/disabled/error）——与版本 state 分离的运营面。"""
        context = self._require_context()
        return await self._session.scalar(
            select(SourceObjectRow.lifecycle_status).where(
                SourceObjectRow.organization_id == context.organization_id,
                SourceObjectRow.workspace_id == context.workspace_id,
                SourceObjectRow.id == object_id,
            )
        )

    async def get_last_sync_error(self, object_id: UUID) -> str | None:
        context = self._require_context()
        return await self._session.scalar(
            select(SourceObjectRow.last_sync_error).where(
                SourceObjectRow.organization_id == context.organization_id,
                SourceObjectRow.workspace_id == context.workspace_id,
                SourceObjectRow.id == object_id,
            )
        )

    async def set_lifecycle_status(
        self, object_id: UUID, status: str, *, error: str | None = None
    ) -> None:
        context = self._require_context()
        await self._session.execute(
            update(SourceObjectRow)
            .where(
                SourceObjectRow.organization_id == context.organization_id,
                SourceObjectRow.workspace_id == context.workspace_id,
                SourceObjectRow.id == object_id,
            )
            .values(
                lifecycle_status=status,
                last_sync_error=error,
                updated_at=utc_now(),
            )
        )

    async def update_acl(self, object_id: UUID, acl: ACLSnapshot) -> SourceObject:
        """当前 ACL 更新（PUT acl；触发器/guard 允许 acl+updated_at 列）。"""
        context = self._require_context()
        await self.get_object(object_id)
        await self._session.execute(
            update(SourceObjectRow)
            .where(
                SourceObjectRow.organization_id == context.organization_id,
                SourceObjectRow.workspace_id == context.workspace_id,
                SourceObjectRow.id == object_id,
            )
            .values(acl=acl.model_dump(), updated_at=utc_now())
        )
        updated = await self._find_object(object_id)
        assert updated is not None  # 同事务内刚更新
        return updated

    async def apply_delete_revoke(
        self, intent: SyncIntent, *, actor_ref: str | None = None
    ) -> DeleteRevokeResult:
        """SyncIntent DELETE/REVOKE 的生产消费（F-R6-04）。

        REVOKE 与 DELETE 同语义面：对象**全部未失权版本**（active 与 stale，
        state != revoked）→ REVOKED + tombstone——「delete/revoke 优先」要求
        源失权后其任何版本不可再被检索/物化（stale 版本对失权源同样失去
        可用性；历史 Run 的 Evidence 引用保留、可见性由 ADR-006 查询期复检
        拒绝）。转移幂等（REVOKED 行不更新），审计仅在发生实际失权时落一行
        （knowledge.source.revoke/delete）。CREATE/UPDATE 不由本消费方处理
        （connector 物化路径）——fail closed。对象级 advisory xact lock 与
        create_version 的接缝（失权快照后提交的物化在锁外排队，不复活）。
        """
        context = self._require_context()
        if intent.event_type not in (SyncEventType.DELETE, SyncEventType.REVOKE):
            raise ApplyIntentError(
                f"apply_delete_revoke does not consume {intent.event_type} intents"
            )
        await self._lock(context, intent.source_object_id)
        obj = await self.get_object(intent.source_object_id)
        now = utc_now()

        scope = (
            select(SourceVersionRow.id)
            .where(
                SourceVersionRow.organization_id == context.organization_id,
                SourceVersionRow.workspace_id == context.workspace_id,
                SourceVersionRow.source_object_id == obj.id,
                SourceVersionRow.state != SourceVersionState.REVOKED.value,
            )
        )
        target_ids = list((await self._session.scalars(scope)).all())
        if not target_ids:
            return DeleteRevokeResult()

        statement = (
            update(SourceVersionRow)
            .where(SourceVersionRow.id.in_(target_ids))
            .values(
                state=SourceVersionState.REVOKED.value,
                tombstone=True,
                updated_at=now,
            )
            .returning(SourceVersionRow.id)
        )
        revoked_ids = set((await self._session.scalars(statement)).all())
        action = (
            "knowledge.source.revoke"
            if intent.event_type is SyncEventType.REVOKE
            else "knowledge.source.delete"
        )
        await append_audit_chain(
            self._session,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            data=AuditEventData(
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=action,
                resource_type="knowledge_source",
                resource_id=obj.id,
                actor_ref=actor_ref or f"connector:{intent.connector}",
                payload_digest=_intent_digest(intent, len(revoked_ids)),
                previous_event_digest="",
                event_digest="",
            ),
        )
        return DeleteRevokeResult(revoked_version_ids=revoked_ids)

    # ── internals ─────────────────────────────────────────────────────────

    async def _transition(
        self,
        version: SourceVersion,
        *,
        target: SourceVersionState,
        tombstone: bool,
    ) -> SourceVersion:
        context = self._require_context()
        statement = (
            update(SourceVersionRow)
            .where(
                SourceVersionRow.organization_id == context.organization_id,
                SourceVersionRow.workspace_id == context.workspace_id,
                SourceVersionRow.id == version.id,
            )
            .values(
                state=target.value,
                tombstone=tombstone,
                updated_at=utc_now(),
            )
        )
        await self._session.execute(statement)
        updated = await self._find_version(version.id)
        assert updated is not None  # 同事务内刚更新
        return updated

    async def _find_object(self, object_id: UUID) -> SourceObject | None:
        context = self._require_context()
        row = await self._session.scalar(
            select(SourceObjectRow).where(
                SourceObjectRow.organization_id == context.organization_id,
                SourceObjectRow.workspace_id == context.workspace_id,
                SourceObjectRow.id == object_id,
            )
        )
        return None if row is None else row_to_object(row)

    async def _lock(self, context: TenantContext, object_id: UUID) -> None:
        """advisory xact lock：seq 分配与失权快照的串行化点（键含租户+对象，
        跨租户不互相阻塞；同对象串行）。"""
        assert context.workspace_id is not None  # _require_context 已收窄
        await advisory_lock(
            self._session,
            context.organization_id,
            namespace=_SOURCE_LEDGER_LOCK_NAMESPACE,
        )
        await advisory_lock(
            self._session,
            context.workspace_id,
            namespace=_SOURCE_LEDGER_LOCK_NAMESPACE ^ 0x0000000100000000,
        )
        await advisory_lock(
            self._session,
            object_id,
            namespace=_SOURCE_LEDGER_LOCK_NAMESPACE,
        )

    async def _find_version(self, version_id: UUID) -> SourceVersion | None:
        context = self._require_context()
        row = await self._session.scalar(
            select(SourceVersionRow).where(
                SourceVersionRow.organization_id == context.organization_id,
                SourceVersionRow.workspace_id == context.workspace_id,
                SourceVersionRow.id == version_id,
            )
        )
        return None if row is None else row_to_version(row)

    async def _next_seq(self, object_id: UUID) -> int:
        context = self._require_context()
        current = await self._session.scalar(
            select(func.max(SourceVersionRow.version_seq)).where(
                SourceVersionRow.organization_id == context.organization_id,
                SourceVersionRow.workspace_id == context.workspace_id,
                SourceVersionRow.source_object_id == object_id,
            )
        )
        return (current or 0) + 1

    def _require_context(self) -> TenantContext:
        if self._context is None:
            raise TenantContextRequired("organization context is required")
        if self._context.workspace_id is None:
            raise TenantContextRequired("source ledger requires workspace context")
        return self._context

    def _require_scope(self, organization_id: UUID, workspace_id: UUID) -> None:
        context = self._require_context()
        if organization_id != context.organization_id:
            raise TenantScopeError("source object does not match tenant context")
        if workspace_id != context.workspace_id:
            raise TenantScopeError("source object does not match workspace context")


def _is_digest_unique_violation(exc: Exception) -> bool:
    """唯一冲突消歧：uq_source_versions_digest → DuplicateVersionError 语义；
    seq 撞号等其他唯一冲突不转译（fail closed，避免全新内容被误当重复跳过）。
    asyncpg 异常经 SQLAlchemy 包装后类名保持稳定，constraint 名走原样探测。"""
    current: BaseException | None = exc
    while current is not None:
        if type(current).__name__ == "UniqueViolationError":
            constraint = getattr(current, "constraint_name", None) or getattr(
                current, "constraint", None
            )
            if constraint is None:
                # asyncpg 特有属性缺失时退化为消息探测（诊断字段非契约）
                return "uq_source_versions_digest" in str(current)
            return "uq_source_versions_digest" in str(constraint)
        current = current.__cause__
    return False


def _intent_digest(intent: SyncIntent, revoked_count: int) -> str:
    from zhiwei.contracts.canonical import digest

    return digest(
        {
            "intent_id": str(intent.id),
            "idempotency_key": intent.idempotency_key,
            "event_type": intent.event_type.value,
            "source_object_id": str(intent.source_object_id),
            "payload": intent.payload,
            "revoked_count": revoked_count,
        }
    )
