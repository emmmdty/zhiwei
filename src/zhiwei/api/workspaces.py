"""Workspace 创建端点（S1-T1 API 基础）。

workspace 创建要求 actor 处于 organization 级上下文（workspace_id 为空）；带 workspace
作用域的 actor 创建新 workspace 会被拒绝（fail closed）。
"""

from collections.abc import Callable
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.repositories import TenantRepository, WorkspaceRecord
from zhiwei.persistence.tenant import TenantContext, TenantScopeError, tenant_session


class CreateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str


def create_workspaces_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
) -> APIRouter:
    router = APIRouter(prefix="/workspaces", tags=["workspaces"])

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=WorkspaceRecord)
    async def create_workspace(
        request: CreateWorkspaceRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> WorkspaceRecord:
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                return await TenantRepository(session, context).create_workspace(
                    uuid4(), name=request.name
                )
            except TenantScopeError as error:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="workspace creation requires an organization-level actor",
                ) from error

    return router
