"""Organization 端点（docs/API.md §2 契约）。

- GET/POST /api/v1/organizations；GET /api/v1/organizations/{org_id}
- bootstrap 经 application command 原子创建 Organization + Owner Membership；
- mutation 要求非空 Idempotency-Key（接入 S0 idempotency 基础）；
- 读跨租户/不存在资源统一 404（防枚举）；actor 与 tenant context 必须显式注入，
  没有默认 allow（OIDC 真实身份依赖在 S1-T2 由 app.py 组合时提供）；
- 生产纵切（S1-T4 修复，repair addendum §3.1）：policy_enforcer 为组合期必需注入
  （fail closed，缺失在构造期抛 TypeError），mutation 一律先经 api.policy_gate 求值
  ——denied → 独立事务 denied 审计 + 403；allowed → 同一 tenant 事务内写 allowed
  审计 + outbox（任一写失败整体回滚）；业务拒绝 → 独立事务 failed 审计。bootstrap
  被 OPA 拒绝不写审计（schema 边界例外，见 policy_gate 文档）。
- bootstrap 的 PEP 输入使用独立 org/create 动作（二轮修复：不借用 org/manage），
  仅无 active org 的 USER 且无任何角色绑定可进入；
- 持久 bootstrap claim（四轮修复）：仅当本次确实创建新 org 时，在同一事务内声明
  claim（窄 SECURITY DEFINER 函数 + principal 级 advisory lock 串行化）；claim 已
  指向其他 org → BootstrapClaimConflict → 403、刚插入的 org 整体回滚、不写 failed
  审计（loser target 无合法 audit FK scope，pre-tenant 例外）；membership 删除不
  重置 claim 资格。
"""

from collections.abc import Callable
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    request_trace,
)
from zhiwei.identity.commands import (
    BootstrapClaimConflict,
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
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType

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
    identity_sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """组成时必须显式注入 actor dependency、identity session 工厂与 policy_enforcer；
    缺少注入在构造期即失败（fail closed，policy_enforcer 缺失抛 TypeError）。
    identity_sessions 供 T2 membership 解析使用。

    policy_enforcer 是生产纵切（repair addendum §3.2）的必需注入：create_app 组合时
    提供，任何直连调用点都不得绕过。不得在端点内复制 gate 逻辑。
    """
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")

    router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])

    @router.get("", response_model=list[OrganizationRecord])
    async def list_organizations(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[OrganizationRecord]:
        # S1-T2 完成 T1 阻断语义：返回当前 authenticated principal 的组织集合，
        # 组织选择来自已验证 membership（identity-global 窄 SECURITY DEFINER
        # resolver），不信任 actor 声明的 org，也不依赖 zhiwei_app 跨组织绕过 RLS。
        async with identity_sessions() as session:
            result = await session.execute(
                text(
                    "SELECT organization_id, organization_status "
                    "FROM public.zhiwei_principal_memberships(:pid) "
                    "WHERE scope = 'organization' ORDER BY organization_id"
                ),
                {"pid": actor.principal_id},
            )
            rows = result.mappings().all()
        return [
            OrganizationRecord(id=row["organization_id"], status=row["organization_status"])
            for row in rows
        ]

    @router.post("")
    async def bootstrap_organization(
        request: CreateOrganizationRequest,
        request_scope: Request,
        idempotency_key: Annotated[
        str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
    ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> JSONResponse:
        # 首登 principal 可以没有 active Organization：bootstrap 以新 org 为事务上下文。
        # 生产纵切：policy 先于事务求值（输入 org = 新建 org、roles 为空——首登无绑定，
        # §3.1.9）；allowed 审计只在 outcome.created 时写（重放不追加，§3.1.8）。
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=True,
            organization_id=request.organization_id,
            workspace_id=None,
            audit_action="organization.create",
            resource_type="organization",
            policy_type=ResourceType.ORG,
            policy_action=Action.CREATE,
            resource_id=request.organization_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
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
                if outcome.created:
                    await append_allowed_audit(
                        session,
                        actor=actor,
                        organization_id=request.organization_id,
                        workspace_id=None,
                        action="organization.create",
                        resource_type="organization",
                        resource_id=request.organization_id,
                        resource_version=1,
                        authorization=authorization,
                    )
            except BootstrapClaimConflict as error:
                # 持久 claim 围栏拒绝（四轮）：同一 principal 已 bootstrap 过另一个
                # org。事务已整体回滚（含刚插入的 org）；loser target 不存在，无合法
                # audit FK scope → 沿用冻结的 pre-tenant 例外：不写 failed 审计、
                # 零 audit/outbox（不得静默扩大该例外）。
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="bootstrap claim already used for another organization",
                ) from error
            except _REQUEST_ERRORS as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=request.organization_id,
                    workspace_id=None,
                    action="organization.create",
                    resource_type="organization",
                    resource_id=request.organization_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
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
