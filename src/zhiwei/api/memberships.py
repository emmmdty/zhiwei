"""Organization members 端点（docs/API.md §2 契约）。

- GET|POST /api/v1/organizations/{org_id}/members
- DELETE /api/v1/organizations/{org_id}/members/{principal_id}
- mutation 要求非空 Idempotency-Key；读跨租户/不存在资源统一 404，写越权 403；
- 命令经 application commands 执行，不直接调用 persistence repository；
- 生产纵切（S1-T4 修复）：policy_enforcer 为组合期必需注入（fail closed，缺失在构造期
  抛 TypeError），mutation 一律先经 api.policy_gate 求值（policy 先于事务；denied →
  独立事务 denied 审计 + 403；allowed 审计只在 outcome.created 时同事务追加；业务拒绝
  → 独立事务 failed 审计）。
"""

from collections.abc import Callable
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    request_trace,
)
from zhiwei.identity.commands import (
    IdempotencyRequest,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    ResourceConflictError,
    add_org_membership,
    canonical_request_digest,
    remove_org_membership,
)
from zhiwei.identity.domain import ActorContext, Membership
from zhiwei.identity.repositories import IdentityRepository
from zhiwei.persistence.repositories import IdempotencyConflict
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
    tenant_session,
)
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType

_REQUEST_ERRORS = (
    TenantContextRequired,
    TenantScopeError,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    ResourceConflictError,
    IdempotencyConflict,
)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: UUID
    role_bindings: list[str] = []


def _reject_write(error: Exception) -> NoReturn:
    """写操作异常映射；未知异常原样上抛（不吞异常）。"""
    if isinstance(error, (TenantContextRequired, TenantScopeError)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="outside tenant scope"
        ) from error
    if isinstance(error, PrincipalDisabledError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="principal is disabled"
        ) from error
    if isinstance(error, PrincipalNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="principal not found"
        ) from error
    if isinstance(error, ResourceConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="resource already exists"
        ) from error
    if isinstance(error, IdempotencyConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency key was already used for another request",
        ) from error
    raise error


def _reject_read(error: Exception) -> NoReturn:
    """读操作异常映射：跨租户目标与不存在资源统一 404（防枚举）。"""
    if isinstance(error, (TenantContextRequired, TenantScopeError)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="resource not found"
        ) from error
    raise error


def create_memberships_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """policy_enforcer 是生产纵切（repair addendum §3.2）的必需注入：create_app 组合时
    提供，任何直连调用点都不得绕过；缺失在构造期抛 TypeError（fail closed）。不得在
    端点内复制 gate 逻辑。"""
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")

    router = APIRouter(prefix="/api/v1", tags=["memberships"])

    @router.get("/organizations/{organization_id}/members", response_model=list[Membership])
    async def list_org_members(
        organization_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[Membership]:
        if actor.organization_id is None:
            _reject_read(TenantContextRequired("organization context is required"))
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                members = await IdentityRepository(session, context).list_memberships(
                    organization_id=organization_id
                )
            except (TenantContextRequired, TenantScopeError) as error:
                _reject_read(error)
        return members

    @router.post("/organizations/{organization_id}/members")
    async def add_org_member(
        organization_id: UUID,
        request: AddMemberRequest,
        request_scope: Request,
        idempotency_key: Annotated[
        str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
    ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> JSONResponse:
        if actor.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="organization context required"
            )
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=organization_id,
            workspace_id=None,
            audit_action="organization.member.add",
            resource_type="membership",
            policy_type=ResourceType.ORG,
            policy_action=Action.MANAGE,
            resource_id=request.principal_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                outcome = await add_org_membership(
                    IdentityRepository(session, context),
                    principal_id=request.principal_id,
                    organization_id=organization_id,
                    role_bindings=frozenset(request.role_bindings),
                    idempotency=IdempotencyRequest(
                        key=idempotency_key,
                        request_digest=canonical_request_digest(
                            "POST", request_scope.url.path, request.model_dump(mode="json")
                        ),
                    ),
                )
                if outcome.created:
                    await append_allowed_audit(
                        session,
                        actor=actor,
                        organization_id=actor.organization_id or organization_id,
                        workspace_id=None,
                        action="organization.member.add",
                        resource_type="membership",
                        resource_id=request.principal_id,
                        resource_version=1,
                        authorization=authorization,
                    )
            except _REQUEST_ERRORS as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=actor.organization_id or organization_id,
                    workspace_id=None,
                    action="organization.member.add",
                    resource_type="membership",
                    resource_id=request.principal_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                _reject_write(error)
        if outcome.created:
            return JSONResponse(content=outcome.response, status_code=status.HTTP_201_CREATED)
        return JSONResponse(content=outcome.response, status_code=status.HTTP_200_OK)

    @router.delete(
        "/organizations/{organization_id}/members/{principal_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def remove_org_member(
        organization_id: UUID,
        principal_id: UUID,
        request_scope: Request,
        idempotency_key: Annotated[
        str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
    ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> Response:
        if actor.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="organization context required"
            )
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=organization_id,
            workspace_id=None,
            audit_action="organization.member.remove",
            resource_type="membership",
            policy_type=ResourceType.ORG,
            policy_action=Action.MANAGE,
            resource_id=principal_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                outcome = await remove_org_membership(
                    IdentityRepository(session, context),
                    principal_id=principal_id,
                    organization_id=organization_id,
                    idempotency=IdempotencyRequest(
                        key=idempotency_key,
                        request_digest=canonical_request_digest(
                            "DELETE", request_scope.url.path, {}
                        ),
                    ),
                )
                if outcome.created:
                    await append_allowed_audit(
                        session,
                        actor=actor,
                        organization_id=actor.organization_id or organization_id,
                        workspace_id=None,
                        action="organization.member.remove",
                        resource_type="membership",
                        resource_id=principal_id,
                        resource_version=1,
                        authorization=authorization,
                    )
            except _REQUEST_ERRORS as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=actor.organization_id or organization_id,
                    workspace_id=None,
                    action="organization.member.remove",
                    resource_type="membership",
                    resource_id=principal_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                _reject_write(error)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
