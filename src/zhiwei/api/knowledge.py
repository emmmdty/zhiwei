"""S5-T7: Knowledge management API — source CRUD, sync, status, version, ACL, disable.

P2b 重接（F-R6-03、F-R6-04）：单例 `_store` 移除，全部端点落 PgSourceLedger
（0021 source_objects/source_versions，内容不可变 + 当前 ACL/运营状态可变）；
mutation 经 authorize_mutation（knowledge_source.manage cell——矩阵行 4 的
「创建/同步/授权/禁用」）+ 同事务 allowed 审计 + denied/failed 独立事务；
disable 级联 ledger 全版本失权（SyncIntent REVOKE 同事务 apply）；读路径
（list/status/versions）维持 P1 止血租户谓词语义（矩阵 read cell =
{memory_steward} 为「按 ACL 读来源」，管理面 list 消费方无 cell 承载——
PERMISSIONS §3.3 登记，与 connections 同型）。

sync 物化语义对齐 ledger 不变量：模拟物化的 digest = sha256(canonical{
source_id, uri})，同一内容重复 sync（含 force）恒 unchanged——force 不得越过
不可变 ledger 的重复 digest 拒绝（stub 期 force 重建同 digest 版本违反
spec §3「Updates create new version」，行为变更登记于 R6 台账）。status
端点 ACL 判定走 check_acl_snapshot 公共入口（消除第二套 ACL 实现；group
allow 生效）。

事实源：S5 spec §3/§7、docs/PERMISSIONS.md §3.1 行 4、findings F-R6-03/04。
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    request_trace,
)
from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.identity.domain import ActorContext
from zhiwei.knowledge.acl import ACLContext, check_acl_snapshot
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceObject,
)
from zhiwei.knowledge.freshness import FreshnessPolicy, FreshnessState, evaluate_freshness
from zhiwei.knowledge.pg_ledger import PgSourceLedger
from zhiwei.knowledge.sync import SyncEventType, SyncIntent
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType

logger = logging.getLogger(__name__)

_AUDIT_RESOURCE_TYPE = "knowledge_source"

_AUDIT_ACTION_CREATE = "knowledge.source.create"
_AUDIT_ACTION_CONNECT = "knowledge.source.connect"
_AUDIT_ACTION_SYNC = "knowledge.source.sync"
_AUDIT_ACTION_ACL = "knowledge.source.acl_update"
_AUDIT_ACTION_DISABLE = "knowledge.source.disable"

# 物化失败（F-R6-13）对 caller 与 last_sync_error 的固定通用文案：内部异常
# 细节（触发器/约束名等）不出进程边界。根因定位走审计链与服务端日志。
_SYNC_FAILURE_DETAIL = "sync materialization failed"


class SourceRecord(BaseModel):
    """Source record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    source_type: str
    connector: str
    uri: str
    classification: str
    status: str
    version_count: int
    latest_version_seq: int | None
    latest_content_digest: str | None
    acl_allowed_principals: tuple[str, ...]
    acl_denied_principals: tuple[str, ...]
    acl_allowed_groups: tuple[str, ...]


class AddSourceRequest(BaseModel):
    """POST body for adding a knowledge source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    classification: str = "PUBLIC"
    acl_allowed_principals: tuple[str, ...] = Field(default_factory=tuple)
    acl_denied_principals: tuple[str, ...] = Field(default_factory=tuple)
    acl_allowed_groups: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SyncRequest(BaseModel):
    """POST body for triggering a sync."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    force: bool = False


class UpdateACLRequest(BaseModel):
    """PUT body for updating source ACL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_principals: tuple[str, ...] = Field(default_factory=tuple)
    denied_principals: tuple[str, ...] = Field(default_factory=tuple)
    allowed_groups: tuple[str, ...] = Field(default_factory=tuple)


class SourceStatusRecord(BaseModel):
    """Source status with freshness, ACL, and score breakdown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    status: str
    version_seq: int | None
    content_digest: str | None
    locator_connector: str | None
    locator_uri: str | None
    freshness_state: str
    acl_allowed: bool
    acl_reason: str
    classification: str
    score_breakdown: dict[str, Any] = Field(default_factory=dict)


class SourceVersionRecord(BaseModel):
    """A single source version record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    source_object_id: UUID
    version_seq: int
    connector: str
    uri: str
    content_digest: str
    state: str
    classification: str
    observed_at: datetime
    valid_at: datetime
    connector_version: str
    parser_version: str
    index_version: str


class SyncResultRecord(BaseModel):
    """Result of a sync operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    sync_status: str
    versions_created: int
    versions_marked_stale: int
    connector: str
    sync_watermark: str | None
    error: str | None = None


class SourceMutationRejected(RuntimeError):
    """业务拒绝（PEP 放行后）：disabled 状态阻断 sync 等运营前置。"""


def _simulated_digest(source: SourceObject) -> str:
    """模拟物化的内容寻址 digest：同一 (source, uri) 的重复 sync 恒同 digest
    ——ledger 重复 digest 拒绝（不可变内容事实）使 force 无法重建同内容版本。
    真实物化经 connector 材料化路径落新 digest（S11 接线）。"""
    return digest_bytes(
        canonical_json(
            {
                "source_id": str(source.id),
                "uri": f"source://{source.id}",
            }
        )
    )


def create_knowledge_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """Create the knowledge sources API router.

    P2b 重接：sessions/policy_enforcer 必须由组合根提供（fail closed，缺失
    在构造期拒绝）。
    """
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    if sessions is None:
        raise TypeError("sessions must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

    def _tenant(actor: ActorContext) -> TenantContext:
        if actor.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization context required",
            )
        if actor.workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="workspace context required",
            )
        return TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )

    async def _authorize(
        request_scope: Request,
        actor: ActorContext,
        context: TenantContext,
        *,
        audit_action: str,
        resource_id: UUID,
    ):
        request_id, trace_id = request_trace(request_scope)
        return (
            await authorize_mutation(
                enforcer=policy_enforcer,
                sessions=sessions,
                actor=actor,
                bootstrap=False,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                audit_action=audit_action,
                resource_type=_AUDIT_RESOURCE_TYPE,
                policy_type=ResourceType.KNOWLEDGE_SOURCE,
                policy_action=Action.MANAGE,
                resource_id=resource_id,
                resource_version=1,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
            ),
            request_id,
            trace_id,
        )

    async def _get_object_or_404(ledger: PgSourceLedger, source_id: UUID) -> SourceObject:
        """未知/跨租户 source 与不存在同形 404（防枚举；ObjectNotFoundError
        是 ledger 域错误，不得逃逸成 500）。"""
        from zhiwei.knowledge.ledger import ObjectNotFoundError

        try:
            return await ledger.get_object(source_id)
        except ObjectNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="source not found",
            ) from exc

    def _source_record(
        obj: SourceObject,
        lifecycle_status: str,
        versions: list[Any] | None = None,
    ) -> SourceRecord:
        latest = versions[-1] if versions else None
        active = latest if latest is not None and latest.state.value == "active" else None
        return SourceRecord(
            id=obj.id,
            source_type=obj.source_type,
            connector=active.locator.connector if active else "",
            uri=active.locator.uri if active else "",
            classification=obj.classification.value,
            status=lifecycle_status,
            version_count=len(versions) if versions is not None else 0,
            latest_version_seq=active.version_seq if active else None,
            latest_content_digest=active.content_digest if active else None,
            acl_allowed_principals=obj.acl.allowed_principals,
            acl_denied_principals=obj.acl.denied_principals,
            acl_allowed_groups=obj.acl.allowed_groups,
        )

    @router.get("/sources", response_model=list[SourceRecord])
    async def list_sources(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[SourceRecord]:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            ledger = PgSourceLedger(session, context)
            objects = await ledger.list_objects()
            records = []
            for obj in objects:
                versions = await ledger.list_versions(obj.id)
                status_value = await ledger.get_lifecycle_status(obj.id)
                records.append(
                    _source_record(obj, status_value or "active", versions)
                )
        return records

    @router.post(
        "/sources",
        status_code=status.HTTP_201_CREATED,
        response_model=SourceRecord,
    )
    async def add_source(
        request_scope: Request,
        request: AddSourceRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceRecord:
        context = _tenant(actor)
        try:
            classification = Classification(request.classification)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid classification: {request.classification}",
            ) from exc
        assert context.workspace_id is not None  # _tenant 已收窄
        source_id = new_id()
        obj = SourceObject(
            id=source_id,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            source_type=request.source_type,
            acl=ACLSnapshot(
                allowed_principals=request.acl_allowed_principals,
                denied_principals=request.acl_denied_principals,
                allowed_groups=request.acl_allowed_groups,
            ),
            classification=classification,
            metadata=request.metadata,
        )
        authorization, _request_id, _trace_id = await _authorize(
            request_scope,
            actor,
            context,
            audit_action=_AUDIT_ACTION_CREATE,
            resource_id=source_id,
        )
        async with tenant_session(sessions, context) as session:
            ledger = PgSourceLedger(session, context)
            stored = await ledger.register_object(obj)
            await ledger.set_lifecycle_status(stored.id, "active")
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_CREATE,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=stored.id,
                resource_version=1,
                authorization=authorization,
            )
        return _source_record(stored, "active")

    @router.post(
        "/sources/{source_id}/connect",
        response_model=SourceRecord,
    )
    async def connect_source(
        request_scope: Request,
        source_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceRecord:
        context = _tenant(actor)
        authorization, _request_id, _trace_id = await _authorize(
            request_scope,
            actor,
            context,
            audit_action=_AUDIT_ACTION_CONNECT,
            resource_id=source_id,
        )
        async with tenant_session(sessions, context) as session:
            ledger = PgSourceLedger(session, context)
            obj = await _get_object_or_404(ledger, source_id)
            await ledger.set_lifecycle_status(source_id, "active")
            versions = await ledger.list_versions(source_id)
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_CONNECT,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=source_id,
                resource_version=1,
                authorization=authorization,
            )
        return _source_record(obj, "active", versions)

    @router.post(
        "/sources/{source_id}/sync",
        response_model=SyncResultRecord,
    )
    async def sync_source(
        request_scope: Request,
        source_id: UUID,
        request: SyncRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SyncResultRecord:
        context = _tenant(actor)
        authorization, _request_id, _trace_id = await _authorize(
            request_scope,
            actor,
            context,
            audit_action=_AUDIT_ACTION_SYNC,
            resource_id=source_id,
        )
        async with tenant_session(sessions, context) as session:
            ledger = PgSourceLedger(session, context)
            obj = await _get_object_or_404(ledger, source_id)
            lifecycle_status = await ledger.get_lifecycle_status(source_id)
            if lifecycle_status == "disabled":
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=_AUDIT_ACTION_SYNC,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=source_id,
                    error=SourceMutationRejected(),
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="source is disabled",
                )
            versions = await ledger.list_versions(source_id)
            digest = _simulated_digest(obj)
            existing_digests = {v.content_digest for v in versions}
            if digest in existing_digests:
                # 内容寻址：同一内容重复 sync（含 force）是 no-op——不可变
                # ledger 的重复 digest 拒绝使 force 无法重建同内容版本
                await append_allowed_audit(
                    session,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=_AUDIT_ACTION_SYNC,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=source_id,
                    resource_version=1,
                    authorization=authorization,
                )
                return SyncResultRecord(
                    source_id=source_id,
                    sync_status="unchanged",
                    versions_created=0,
                    versions_marked_stale=0,
                    connector=obj.source_type,
                    sync_watermark=None,
                )
            now = utc_now()
            try:
                # savepoint（F-R6-13）：物化（stale 转移 + 版本插入）原子化——
                # flush 失败回滚到 savepoint，外层事务保持可用，失败收尾
                # （lifecycle 落 error）不再踩 pending-rollback 会话。
                async with session.begin_nested():
                    if versions:
                        latest = versions[-1]
                        await ledger.mark_stale(latest.id)
                    new_version = await ledger.create_version(
                        source_id,
                        locator=Locator(connector=obj.source_type, uri=f"source://{source_id}"),
                        content_digest=digest,
                        observed_at=now,
                        valid_at=now,
                    )
            except Exception as exc:
                # 三态闭合（F-R6-03）：failed 审计走独立事务，与外层事务状态
                # 无关；caller 只见固定通用文案。except 范围仅物化段（块内无
                # HTTPException 源，BaseException 不在此列）——服务端日志留
                # 根因（含堆栈），内部细节仍不出进程边界。
                logger.warning(
                    "knowledge sync materialization failed (source %s)", source_id,
                    exc_info=True,
                )
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=_AUDIT_ACTION_SYNC,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=source_id,
                    error=exc,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                await ledger.set_lifecycle_status(
                    source_id, "error", error=_SYNC_FAILURE_DETAIL
                )
                return SyncResultRecord(
                    source_id=source_id,
                    sync_status="failed",
                    versions_created=0,
                    versions_marked_stale=0,
                    connector=obj.source_type,
                    sync_watermark=None,
                    error=_SYNC_FAILURE_DETAIL,
                )
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_SYNC,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=source_id,
                resource_version=new_version.version_seq,
                authorization=authorization,
            )
            return SyncResultRecord(
                source_id=source_id,
                sync_status="completed",
                versions_created=1,
                versions_marked_stale=1 if versions else 0,
                connector=obj.source_type,
                sync_watermark=str(new_version.id),
            )

    @router.get(
        "/sources/{source_id}/status",
        response_model=SourceStatusRecord,
    )
    async def source_status(
        source_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceStatusRecord:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            ledger = PgSourceLedger(session, context)
            obj = await _get_object_or_404(ledger, source_id)
            latest = await ledger.latest_version(source_id)
            source_status = await ledger.get_lifecycle_status(source_id)

        freshness_state = FreshnessState.EXPIRED.value
        acl_allowed = False
        acl_reason = "no_version"
        score_breakdown: dict[str, Any] = {}

        if latest:
            fp = FreshnessPolicy(connector=obj.source_type)
            fr = evaluate_freshness(latest, fp)
            freshness_state = fr.state.value

            # ADR-006 公共判定入口（deny-override/unknown 语义 + group allow），
            # 不复制第二套 ACL 判定；context ACL 以 principal 身份构造
            #（查询期 deny 列表来自身份侧，S11 接入身份组解析后扩展）
            assert context.workspace_id is not None  # _tenant 已收窄
            acl_check = check_acl_snapshot(
                obj.acl,
                ACLContext(
                    principal_id=actor.principal_id,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                ),
            )
            acl_allowed = acl_check.allowed
            acl_reason = acl_check.reason or ("allowed" if acl_check.allowed else "")

            score_breakdown = {
                "acl_score": 1.0 if acl_allowed else 0.0,
                "freshness_score": {
                    FreshnessState.FRESH: 1.0,
                    FreshnessState.AGING: 0.7,
                    FreshnessState.STALE: 0.3,
                    FreshnessState.EXPIRED: 0.0,
                }.get(fr.state, 0.0),
            }

        return SourceStatusRecord(
            source_id=source_id,
            status=source_status or "active",
            version_seq=latest.version_seq if latest else None,
            content_digest=latest.content_digest if latest else None,
            locator_connector=latest.locator.connector if latest else None,
            locator_uri=latest.locator.uri if latest else None,
            freshness_state=freshness_state,
            acl_allowed=acl_allowed,
            acl_reason=acl_reason,
            classification=obj.classification.value,
            score_breakdown=score_breakdown,
        )

    @router.get(
        "/sources/{source_id}/versions",
        response_model=list[SourceVersionRecord],
    )
    async def list_source_versions(
        source_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[SourceVersionRecord]:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            ledger = PgSourceLedger(session, context)
            await _get_object_or_404(ledger, source_id)
            versions = await ledger.list_versions(source_id)
        return [
            SourceVersionRecord(
                id=v.id,
                source_object_id=v.source_object_id,
                version_seq=v.version_seq,
                connector=v.locator.connector,
                uri=v.locator.uri,
                content_digest=v.content_digest,
                state=v.state.value,
                classification=v.classification.value,
                observed_at=v.observed_at,
                valid_at=v.valid_at,
                connector_version=v.connector_version,
                parser_version=v.parser_version,
                index_version=v.index_version,
            )
            for v in versions
        ]

    @router.put(
        "/sources/{source_id}/acl",
        response_model=SourceRecord,
    )
    async def update_source_acl(
        request_scope: Request,
        source_id: UUID,
        request: UpdateACLRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceRecord:
        context = _tenant(actor)
        authorization, _request_id, _trace_id = await _authorize(
            request_scope,
            actor,
            context,
            audit_action=_AUDIT_ACTION_ACL,
            resource_id=source_id,
        )
        async with tenant_session(sessions, context) as session:
            ledger = PgSourceLedger(session, context)
            await _get_object_or_404(ledger, source_id)
            updated = await ledger.update_acl(
                source_id,
                ACLSnapshot(
                    allowed_principals=request.allowed_principals,
                    denied_principals=request.denied_principals,
                    allowed_groups=request.allowed_groups,
                ),
            )
            versions = await ledger.list_versions(source_id)
            lifecycle_status = await ledger.get_lifecycle_status(source_id)
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_ACL,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=source_id,
                resource_version=1,
                authorization=authorization,
            )
        return _source_record(updated, lifecycle_status or "active", versions)

    @router.post(
        "/sources/{source_id}/disable",
        response_model=SourceRecord,
    )
    async def disable_source(
        request_scope: Request,
        source_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceRecord:
        context = _tenant(actor)
        authorization, _request_id, _trace_id = await _authorize(
            request_scope,
            actor,
            context,
            audit_action=_AUDIT_ACTION_DISABLE,
            resource_id=source_id,
        )
        async with tenant_session(sessions, context) as session:
            ledger = PgSourceLedger(session, context)
            obj = await _get_object_or_404(ledger, source_id)
            # F-R6-04 级联：disable 触发 SyncIntent REVOKE——ledger 全版本
            # 失权（REVOKED+tombstone）+ knowledge.source.revoke 审计，与
            # 运营状态变更同事务
            cascade = await ledger.apply_delete_revoke(
                SyncIntent(
                    event_type=SyncEventType.REVOKE,
                    connector=obj.source_type,
                    source_object_id=source_id,
                    event_id=f"disable:{source_id}",
                    payload={"reason": "source disabled via management API"},
                    idempotency_key=f"disable:{source_id}",
                ),
                # 审计归因 = 发起 disable 的认证主体（管理动作，非 connector 事件）
                actor_ref=f"user:{actor.principal_id}",
            )
            await ledger.set_lifecycle_status(source_id, "disabled")
            versions = await ledger.list_versions(source_id)
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_DISABLE,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=source_id,
                resource_version=1,
                authorization=authorization,
            )
        _ = cascade  # 失权集合经 ledger 审计与版本状态承载（响应不变）
        return _source_record(obj, "disabled", versions)

    return router
