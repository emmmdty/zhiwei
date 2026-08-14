"""Workspace 与 Group 端点（docs/API.md §2 契约）。

- GET|POST /api/v1/organizations/{org_id}/workspaces（组织级上下文）
- GET|POST /api/v1/workspaces/{workspace_id}/groups（workspace 级上下文，总设计 §3.1）
- mutation 要求非空 Idempotency-Key；读跨租户/不存在资源统一 404，写越权 403；
- 命令经 application commands 执行，不直接调用 persistence repository。
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
    NameConflictError,
    ResourceConflictError,
    canonical_request_digest,
    create_group,
    create_workspace,
)
from zhiwei.identity.domain import ActorContext, Group
from zhiwei.identity.repositories import IdentityRepository
from zhiwei.persistence.repositories import IdempotencyConflict, WorkspaceRecord
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
    tenant_session,
)

_REQUEST_ERRORS = (
    TenantContextRequired,
    TenantScopeError,
    NameConflictError,
    ResourceConflictError,
    IdempotencyConflict,
)


class CreateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID
    name: str


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: UUID
    name: str


class GroupCreated(BaseModel):
    """POST /workspaces/{workspace_id}/groups 的幂等响应（仅预知字段，重放与原响应一致）。"""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    name: str


def _reject_write(error: Exception) -> NoReturn:
    """写操作异常映射；未知异常原样上抛（不吞异常）。"""
    if isinstance(error, (TenantContextRequired, TenantScopeError)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="outside tenant scope"
        ) from error
    if isinstance(error, NameConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="resource name is already taken"
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


def _no_organization(error: TenantContextRequired | TenantScopeError) -> NoReturn:
    """组织级端点要求 actor 带 organization context（写 403）。"""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="organization context required"
    ) from error


def create_workspaces_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["workspaces"])

    @router.get("/organizations/{organization_id}/workspaces", response_model=list[WorkspaceRecord])
    async def list_workspaces(
        organization_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[WorkspaceRecord]:
        if actor.organization_id is None:
            _reject_read(TenantContextRequired("organization context is required"))
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                workspaces = await IdentityRepository(session, context).list_workspaces(
                    organization_id=organization_id
                )
            except (TenantContextRequired, TenantScopeError) as error:
                _reject_read(error)
        return [
            WorkspaceRecord(
                id=workspace.id,
                organization_id=workspace.organization_id,
                name=workspace.name,
            )
            for workspace in workspaces
        ]

    @router.post("/organizations/{organization_id}/workspaces")
    async def create_organization_workspace(
        organization_id: UUID,
        request: CreateWorkspaceRequest,
        request_scope: Request,
        idempotency_key: Annotated[
        str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
    ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> JSONResponse:
        if actor.organization_id is None:
            _no_organization(TenantContextRequired("organization context is required"))
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                outcome = await create_workspace(
                    IdentityRepository(session, context),
                    organization_id=organization_id,
                    workspace_id=request.workspace_id,
                    name=request.name,
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

    @router.get("/workspaces/{workspace_id}/groups", response_model=list[Group])
    async def list_workspace_groups(
        workspace_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[Group]:
        if actor.organization_id is None:
            _reject_read(TenantContextRequired("organization context is required"))
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                groups = await IdentityRepository(session, context).list_groups(
                    organization_id=actor.organization_id, workspace_id=workspace_id
                )
            except (TenantContextRequired, TenantScopeError) as error:
                _reject_read(error)
        return groups

    @router.post("/workspaces/{workspace_id}/groups", response_model=GroupCreated)
    async def create_workspace_group(
        workspace_id: UUID,
        request: CreateGroupRequest,
        request_scope: Request,
        idempotency_key: Annotated[
        str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
    ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> JSONResponse:
        if actor.organization_id is None:
            _no_organization(TenantContextRequired("organization context is required"))
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                outcome = await create_group(
                    IdentityRepository(session, context),
                    group_id=request.group_id,
                    organization_id=actor.organization_id,
                    workspace_id=workspace_id,
                    name=request.name,
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

    return router
