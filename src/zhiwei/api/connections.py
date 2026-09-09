"""S4-T8：Connection API——Connection CRUD + status + suspend/revoke。

P2b 重接（F-R6-03、F-R3-06 own cells、P1 残留 provider_version_id 校验）：
单例 `_store` 移除，全部端点落 PgConnectionRepository（0020 connections，
tenant-scoped + status CAS）；mutation 经 authorize_mutation（冻结矩阵行
「Connection/secret」cells）+ 同事务 allowed 审计 + denied/failed 独立事务。

cell 映射（矩阵语义，Rego 唯一事实）：
- create：workspace_service → connection_secret.create_workspace_connection；
  user_delegated → connection_secret.create_own（owner 证据 = 认证主体；
  request 自报 principal_id 不是授权事实，他主体声明直接 403）；service_account
  无矩阵 cell → 本地结构性拒绝 + denied 审计（fail closed，不借道任意 cell）；
- actions：revoke/suspend → connection_secret.revoke（suspend 无独立 cell，
  沿用 P1 止血冻结映射——PERMISSIONS §3.3 登记）；own delegated 连接的
  revoke → connection_secret.revoke_own（owner 证据取自权威记录）；
- 读路径（list/get/status）：矩阵无 member read cell（仅 auditor
  read_status_fingerprint），维持 P1 止血的租户谓词语义——PERMISSIONS §3.3
  登记，未列动作默认拒绝不影响既有只读面（golden GUARD 冻结行为）。

provider_version_id 存在性校验（P1 残留）：经组合根注入 capability 目录
查询，PEP 之后执行（不向未授权 caller 泄漏目录存在性），未知 → 422。

事实源：S4 spec §5（Connection and execution）、docs/PERMISSIONS.md §3.1 行 6、
findings F-R6-03、F-R3-06。
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    request_trace,
)
from zhiwei.capabilities.connections import (
    Connection,
    ConnectionStatus,
    SubjectMode,
)
from zhiwei.capabilities.pg_connections import (
    ConnectionTransitionConflict,
    PgConnectionRepository,
)
from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.audit import append_fail_closed_audit
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.input import ResourceContext
from zhiwei.policy.roles import Action, Purpose, ResourceType

logger = logging.getLogger(__name__)

_AUDIT_RESOURCE_TYPE = "connection"

_AUDIT_ACTION_CREATE = "connection.create"
_AUDIT_ACTION_SUSPEND = "connection.suspend"
_AUDIT_ACTION_REVOKE = "connection.revoke"


class ConnectionRecord(BaseModel):
    """Connection record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    provider_version_id: UUID
    subject_mode: str
    status: str
    principal_id: UUID | None
    version: int
    fingerprint: str


class ConnectionStatusRecord(BaseModel):
    """Connection status projection (fingerprint + credential status)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: UUID
    status: str
    fingerprint: str
    credential_status: str


class CreateConnectionRequest(BaseModel):
    """POST body for creating a connection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_version_id: UUID
    subject_mode: str = "workspace_service"
    principal_id: UUID | None = None


class ConnectionActionRequest(BaseModel):
    """POST body for connection lifecycle actions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str


class ConnectionTransitionRejected(RuntimeError):
    """业务拒绝（PEP 放行后）：revoked 终态不可再转移——failed 审计 reason。"""


class ProviderVersionUnknown(RuntimeError):
    """业务拒绝（PEP 放行后）：provider_version_id 不在 capability 目录——failed 审计。"""


def _record_response(connection: Connection) -> ConnectionRecord:
    return ConnectionRecord(
        id=connection.id,
        organization_id=connection.organization_id,
        workspace_id=connection.workspace_id,
        provider_version_id=connection.provider_version_id,
        subject_mode=connection.subject_mode.value,
        status=connection.status.value,
        principal_id=connection.principal_id,
        version=connection.version,
        fingerprint=connection.compute_fingerprint(),
    )


def create_connections_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
    provider_version_exists: Callable[[UUID, UUID, UUID], bool],
) -> APIRouter:
    """Create the connections API router.

    P2b 重接：sessions/policy_enforcer/provider_version_exists 必须由组合根
    提供（fail closed，缺失在构造期拒绝）；provider_version_exists 是 capability
    目录的租户作用域查询（组合根绑定，router 不 import 其他 router 的 store）。
    """
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    if sessions is None:
        raise TypeError("sessions must be provided (fail closed)")
    if provider_version_exists is None:
        raise TypeError("provider_version_exists must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1/connections", tags=["connections"])

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

    def _repo(context: TenantContext, session: Any) -> PgConnectionRepository:
        return PgConnectionRepository(session, context)

    async def _authorize(
        request_scope: Request,
        actor: ActorContext,
        context: TenantContext,
        *,
        audit_action: str,
        policy_action: Action,
        resource_id: UUID,
        resource_context: ResourceContext | None = None,
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
                policy_type=ResourceType.CONNECTION_SECRET,
                policy_action=policy_action,
                resource_id=resource_id,
                resource_version=1,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
                resource_context=resource_context,
            ),
            request_id,
            trace_id,
        )

    @router.get("", response_model=list[ConnectionRecord])
    async def list_connections(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ConnectionRecord]:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            connections = await _repo(context, session).list_tenant()
        return [_record_response(c) for c in connections]

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=ConnectionRecord,
    )
    async def create_connection(
        request_scope: Request,
        request: CreateConnectionRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConnectionRecord:
        context = _tenant(actor)
        assert context.workspace_id is not None  # _tenant 已收窄
        try:
            subject_mode = SubjectMode(request.subject_mode)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid subject_mode: {request.subject_mode}",
            ) from exc

        if subject_mode is SubjectMode.SERVICE_ACCOUNT:
            # 矩阵无 service_account Connection cell：结构性拒绝（PEP 无 cell
            # 可求值），denied 审计独立事务（runs.py 非 USER 决策拒绝同款）。
            request_id, trace_id = request_trace(request_scope)
            denial = policy_enforcer.deny("no_matrix_cell")
            await append_fail_closed_audit(
                sessions,
                context,
                _denied_record(
                    actor=actor,
                    context=context,
                    action=_AUDIT_ACTION_CREATE,
                    resource_id=request.provider_version_id,
                    request_id=request_id,
                    trace_id=trace_id,
                    denial=denial,
                    reason="no_matrix_cell",
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="service_account connections have no matrix cell",
            )

        if subject_mode is SubjectMode.USER_DELEGATED:
            # own 语义：owner = 认证主体（服务端事实）；request 自报他主体是
            # own 证据伪造尝试——本地拒绝 + denied 审计独立事务（与
            # service_account 结构性拒绝同口径，policy_gate「除 bootstrap 外
            # 一切 deny 必须写审计」纪律）。
            if request.principal_id is not None and request.principal_id != actor.principal_id:
                request_id, trace_id = request_trace(request_scope)
                denial = policy_enforcer.deny("own_evidence_forgery")
                await append_fail_closed_audit(
                    sessions,
                    context,
                    _denied_record(
                        actor=actor,
                        context=context,
                        action=_AUDIT_ACTION_CREATE,
                        resource_id=request.provider_version_id,
                        request_id=request_id,
                        trace_id=trace_id,
                        denial=denial,
                        reason="own_evidence_forgery",
                    ),
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="principal_id must be the authenticated principal for delegated connections",
                )
            policy_action = Action.CREATE_OWN
            resource_context = ResourceContext(owner_principal_id=actor.principal_id)
            audit_action = _AUDIT_ACTION_CREATE
        else:
            policy_action = Action.CREATE_WORKSPACE_CONNECTION
            resource_context = None
            audit_action = _AUDIT_ACTION_CREATE

        connection_id = new_id()
        authorization, request_id, trace_id = await _authorize(
            request_scope,
            actor,
            context,
            audit_action=audit_action,
            policy_action=policy_action,
            resource_id=connection_id,
            resource_context=resource_context,
        )
        # 存在性校验在 PEP 之后（不向未授权 caller 泄漏 capability 目录存在性）；
        # PEP 放行后被拒 = 业务拒绝 → failed 审计独立事务。
        if not provider_version_exists(
            request.provider_version_id, context.organization_id, context.workspace_id
        ):
            await append_failed_mutation_audit(
                sessions,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_CREATE,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=connection_id,
                error=ProviderVersionUnknown(),
                request_id=authorization.request_id,
                trace_id=authorization.trace_id,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="unknown provider_version_id",
            )
        now = datetime.now(UTC)
        connection = Connection(
            id=connection_id,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            provider_version_id=request.provider_version_id,
            subject_mode=subject_mode,
            status=ConnectionStatus.ACTIVE,
            principal_id=(
                actor.principal_id
                if subject_mode is SubjectMode.USER_DELEGATED
                else request.principal_id
            ),
            created_at=now,
            updated_at=now,
        )
        async with tenant_session(sessions, context) as session:
            stored = await _repo(context, session).create(connection)
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_CREATE,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=stored.id,
                resource_version=stored.version,
                authorization=authorization,
            )
        return _record_response(stored)

    @router.get("/{connection_id}", response_model=ConnectionRecord)
    async def get_connection(
        connection_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConnectionRecord:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            connection = await _repo(context, session).get(connection_id)
        if connection is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="connection not found",
            )
        return _record_response(connection)

    @router.get("/{connection_id}/status", response_model=ConnectionStatusRecord)
    async def get_connection_status(
        connection_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConnectionStatusRecord:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            connection = await _repo(context, session).get(connection_id)
        if connection is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="connection not found",
            )
        # credential_status is opaque — in production it would query SecretBackend
        credential_status = (
            "active"
            if connection.status == ConnectionStatus.ACTIVE
            else connection.status.value
        )
        return ConnectionStatusRecord(
            connection_id=connection.id,
            status=connection.status.value,
            fingerprint=connection.compute_fingerprint(),
            credential_status=credential_status,
        )

    @router.post(
        "/{connection_id}/actions",
        response_model=ConnectionRecord,
    )
    async def connection_action(
        request_scope: Request,
        connection_id: UUID,
        request: ConnectionActionRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConnectionRecord:
        context = _tenant(actor)
        action = request.action
        transition_map = {
            "suspend": (ConnectionStatus.SUSPENDED, _AUDIT_ACTION_SUSPEND),
            "revoke": (ConnectionStatus.REVOKED, _AUDIT_ACTION_REVOKE),
        }
        if action not in transition_map:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown action: {action}",
            )
        target_status, audit_action = transition_map[action]
        # 归属校验先于 PEP（证据取自权威记录；跨租户/未知与不存在同形 404）
        async with tenant_session(sessions, context) as session:
            connection = await _repo(context, session).get(connection_id)
        if connection is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="connection not found",
            )
        if (
            connection.subject_mode is SubjectMode.USER_DELEGATED
            and connection.principal_id == actor.principal_id
            and action == "revoke"
        ):
            # own delegated：owner 撤销自己的委托连接（owner 证据=权威记录）
            policy_action = Action.REVOKE_OWN
            resource_context = ResourceContext(
                owner_principal_id=connection.principal_id
            )
        else:
            # admin 生命周期（suspend 沿用 revoke cell——P1 冻结映射，§3.3 登记）
            policy_action = Action.REVOKE
            resource_context = None
        authorization, _request_id, _trace_id = await _authorize(
            request_scope,
            actor,
            context,
            audit_action=audit_action,
            policy_action=policy_action,
            resource_id=connection_id,
            resource_context=resource_context,
        )
        async with tenant_session(sessions, context) as session:
            repo = _repo(context, session)
            current = await repo.get(connection_id)
            if current is None:  # pragma: no cover - 归属校验先行，同请求内不变
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="connection not found",
                )
            if current.status == ConnectionStatus.REVOKED:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=audit_action,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=connection_id,
                    error=ConnectionTransitionRejected(),
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot act on revoked connection",
                )
            try:
                updated = await repo.transition_status(
                    connection_id,
                    expected_status=current.status,
                    target_status=target_status,
                    now=datetime.now(UTC),
                )
            except ConnectionTransitionConflict as exc:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=audit_action,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=connection_id,
                    error=exc,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="connection changed concurrently",
                ) from exc
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=audit_action,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=connection_id,
                resource_version=updated.version,
                authorization=authorization,
            )
        return _record_response(updated)

    return router


def _denied_record(
    *,
    actor: ActorContext,
    context: TenantContext,
    action: str,
    resource_id: UUID,
    request_id: str,
    trace_id: str,
    denial: Any,
    reason: str,
) -> Any:
    from zhiwei.api.policy_gate import denied_audit_record

    return denied_audit_record(
        actor=actor,
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        action=action,
        resource_type=_AUDIT_RESOURCE_TYPE,
        resource_id=resource_id,
        decision=denial,
        reason=reason,
        request_id=request_id,
        trace_id=trace_id,
    )
