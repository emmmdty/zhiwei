"""Membership / Group 端点（S1-T1 API 基础）。

所有端点都在显式 tenant context 下读写；tenant-owned repository 查询显式携带
tenant predicate，RLS 只是纵深防御。缺少 actor 注入在构造期即失败。
"""

from collections.abc import Callable
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.identity.commands import (
    ExternalIdentityConflictError,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    add_group_member,
    add_org_membership,
    add_workspace_membership,
    create_group,
    remove_org_membership,
)
from zhiwei.identity.domain import (
    ActorContext,
    Group,
    GroupMember,
    Membership,
    WorkspaceMembership,
)
from zhiwei.identity.repositories import IdentityRepository
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
    tenant_session,
)

_REQUEST_ERRORS = (
    TenantContextRequired,
    TenantScopeError,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    ExternalIdentityConflictError,
)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal_id: UUID
    role_bindings: list[str] = []


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str


class AddGroupMemberRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal_id: UUID


def _reject(error: Exception) -> NoReturn:
    """写操作异常映射；未知异常原样上抛（不吞异常）。"""
    if isinstance(error, (TenantContextRequired, TenantScopeError)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="outside tenant scope"
        ) from error
    if isinstance(error, PrincipalDisabledError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="principal is disabled"
        ) from error
    if isinstance(error, ExternalIdentityConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="external identity is already bound",
        ) from error
    if isinstance(error, PrincipalNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="principal not found"
        ) from error
    raise error


def _reject_read(error: Exception) -> NoReturn:
    """读操作异常映射：跨租户目标与不存在资源统一 404（防枚举）；未知异常原样上抛。"""
    if isinstance(error, (TenantContextRequired, TenantScopeError)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="resource not found"
        ) from error
    raise error


def _context(actor: ActorContext) -> TenantContext:
    return TenantContext(
        organization_id=actor.organization_id, workspace_id=actor.workspace_id
    )


def create_memberships_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
) -> APIRouter:
    router = APIRouter(tags=["memberships"])

    @router.get("/organizations/{organization_id}/members", response_model=list[Membership])
    async def list_org_members(
        organization_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[Membership]:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                return await IdentityRepository(session, _context(actor)).list_memberships(
                    organization_id=organization_id
                )
            except _REQUEST_ERRORS as error:
                _reject_read(error)

    @router.post(
        "/organizations/{organization_id}/members",
        status_code=status.HTTP_201_CREATED,
        response_model=Membership,
    )
    async def add_org_member(
        organization_id: UUID,
        request: AddMemberRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> Membership:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                return await add_org_membership(
                    IdentityRepository(session, _context(actor)),
                    principal_id=request.principal_id,
                    organization_id=organization_id,
                    role_bindings=frozenset(request.role_bindings),
                )
            except _REQUEST_ERRORS as error:
                _reject(error)

    @router.delete(
        "/organizations/{organization_id}/members/{principal_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def remove_org_member(
        organization_id: UUID,
        principal_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> Response:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                await remove_org_membership(
                    IdentityRepository(session, _context(actor)),
                    principal_id=principal_id,
                    organization_id=organization_id,
                )
            except _REQUEST_ERRORS as error:
                _reject(error)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/organizations/{organization_id}/groups",
        status_code=status.HTTP_201_CREATED,
        response_model=Group,
    )
    async def create_org_group(
        organization_id: UUID,
        request: CreateGroupRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> Group:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                return await create_group(
                    IdentityRepository(session, _context(actor)),
                    organization_id=organization_id,
                    name=request.name,
                )
            except _REQUEST_ERRORS as error:
                _reject(error)

    @router.get("/organizations/{organization_id}/groups", response_model=list[Group])
    async def list_org_groups(
        organization_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[Group]:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                return await IdentityRepository(session, _context(actor)).list_groups(
                    organization_id=organization_id
                )
            except _REQUEST_ERRORS as error:
                _reject_read(error)

    @router.post(
        "/groups/{group_id}/members",
        status_code=status.HTTP_201_CREATED,
        response_model=GroupMember,
    )
    async def add_group_member_endpoint(
        group_id: UUID,
        request: AddGroupMemberRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> GroupMember:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                return await add_group_member(
                    IdentityRepository(session, _context(actor)),
                    group_id=group_id,
                    organization_id=actor.organization_id,
                    principal_id=request.principal_id,
                )
            except _REQUEST_ERRORS as error:
                _reject(error)

    @router.get("/groups/{group_id}/members", response_model=list[GroupMember])
    async def list_group_members_endpoint(
        group_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[GroupMember]:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                return await IdentityRepository(session, _context(actor)).list_group_members(
                    group_id=group_id, organization_id=actor.organization_id
                )
            except _REQUEST_ERRORS as error:
                _reject_read(error)

    @router.get("/workspaces/{workspace_id}/members", response_model=list[WorkspaceMembership])
    async def list_workspace_members(
        workspace_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[WorkspaceMembership]:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                return await IdentityRepository(session, _context(actor)).list_workspace_memberships(
                    workspace_id=workspace_id
                )
            except _REQUEST_ERRORS as error:
                _reject_read(error)

    @router.post(
        "/workspaces/{workspace_id}/members",
        status_code=status.HTTP_201_CREATED,
        response_model=WorkspaceMembership,
    )
    async def add_workspace_member(
        workspace_id: UUID,
        request: AddMemberRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> WorkspaceMembership:
        async with tenant_session(sessions, _context(actor)) as session:
            try:
                return await add_workspace_membership(
                    IdentityRepository(session, _context(actor)),
                    principal_id=request.principal_id,
                    organization_id=actor.organization_id,
                    workspace_id=workspace_id,
                    role_bindings=frozenset(request.role_bindings),
                )
            except _REQUEST_ERRORS as error:
                _reject(error)

    return router
