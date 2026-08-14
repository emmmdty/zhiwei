"""Organization 端点（docs/API.md §2 契约）。

- GET/POST /api/v1/organizations；GET /api/v1/organizations/{org_id}
- bootstrap 经 application command 原子创建 Organization + Owner Membership；
- mutation 要求非空 Idempotency-Key（接入 S0 idempotency 基础）；
- 读跨租户/不存在资源统一 404（防枚举）；actor 与 tenant context 必须显式注入，
  没有默认 allow（OIDC 真实身份依赖在 S1-T2 由 app.py 组合时提供）。
"""

from collections.abc import Callable
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.identity.commands import (
    IdempotencyRequest,
    OrganizationExistsError,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    canonical_request_digest,
    create_organization,
)
from zhiwei.identity.domain import ActorContext
from zhiwei.identity.repositories import IdentityRepository
from zhiwei.persistence.repositories import IdempotencyConflict, OrganizationRecord
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
    tenant_session,
)

_REQUEST_ERRORS = (
    TenantContextRequired,
    TenantScopeError,
    OrganizationExistsError,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    IdempotencyConflict,
)


class CreateOrganizationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: UUID


def _reject_write(error: Exception) -> NoReturn:
    """写操作异常映射；未知异常原样上抛（不吞异常）。"""
    if isinstance(error, (TenantContextRequired, TenantScopeError)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="outside tenant scope"
        ) from error
    if isinstance(error, OrganizationExistsError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="organization already exists"
        ) from error
    if isinstance(error, PrincipalDisabledError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="principal is disabled"
        ) from error
    if isinstance(error, PrincipalNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="principal not found"
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


def create_organizations_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
) -> APIRouter:
    """组成时必须显式注入 actor dependency；缺少注入在构造期即失败（fail closed）。"""

    router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])

    @router.get("", response_model=list[OrganizationRecord])
    async def list_organizations(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[OrganizationRecord]:
        # 只返回当前 tenant context 的组织；跨租户枚举属于 T2 identity-global 设计阻断项
        if actor.organization_id is None or actor.workspace_id is not None:
            return []
        context = TenantContext(organization_id=actor.organization_id)
        async with tenant_session(sessions, context) as session:
            organization = await IdentityRepository(session, context).get_organization(
                actor.organization_id
            )
        if organization is None:
            return []
        return [OrganizationRecord(id=organization.id, status=organization.status)]

    @router.post("")
    async def bootstrap_organization(
        request: CreateOrganizationRequest,
        request_scope: Request,
        idempotency_key: Annotated[
        str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
    ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> JSONResponse:
        # 首登 principal 可以没有 active Organization：bootstrap 以新 org 为事务上下文
        context = TenantContext(organization_id=request.organization_id)
        async with tenant_session(sessions, context) as session:
            try:
                outcome = await create_organization(
                    IdentityRepository(session, context),
                    organization_id=request.organization_id,
                    owner_principal_id=actor.principal_id,
                    idempotency=IdempotencyRequest(
                        key=idempotency_key,
                        request_digest=canonical_request_digest(
                            "POST", request_scope.url.path, request.model_dump(mode="json")
                        ),
                    ),
                )
            except _REQUEST_ERRORS as error:
                _reject_write(error)
        if outcome.created:
            return JSONResponse(content=outcome.response, status_code=status.HTTP_201_CREATED)
        return JSONResponse(content=outcome.response, status_code=status.HTTP_200_OK)

    @router.get("/{organization_id}", response_model=OrganizationRecord)
    async def get_organization(
        organization_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> OrganizationRecord:
        if actor.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
            )
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                organization = await IdentityRepository(session, context).get_organization(
                    organization_id
                )
            except (TenantContextRequired, TenantScopeError) as error:
                _reject_read(error)
        if organization is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
            )
        return OrganizationRecord(id=organization.id, status=organization.status)

    return router
