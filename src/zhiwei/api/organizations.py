"""Organization 只读端点（S1-T1 API 基础）。

actor 与 tenant context 必须经显式 actor_dependency 注入——本模块没有默认 actor，也没有
「默认允许」占位；OIDC 真实身份依赖在 S1-T2 由 app.py 组合时提供。
"""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.repositories import OrganizationRecord, TenantRepository
from zhiwei.persistence.tenant import TenantContext, TenantScopeError, tenant_session


def create_organizations_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
) -> APIRouter:
    """组成时必须显式注入 actor dependency；缺少注入在构造期即失败（fail closed）。"""

    router = APIRouter(prefix="/organizations", tags=["organizations"])

    @router.get("/{organization_id}", response_model=OrganizationRecord)
    async def get_organization(
        organization_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> OrganizationRecord:
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                record = await TenantRepository(session, context).get_organization(
                    organization_id
                )
            except TenantScopeError as error:
                # 读操作防枚举：跨租户 org 与不存在的 id 一律 404，不泄露组织是否存在
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
                ) from error
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
            )
        return record

    return router
