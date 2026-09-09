"""Workspace 与 Group 端点（docs/API.md §2 契约）。

- GET|POST /api/v1/organizations/{org_id}/workspaces（组织级上下文）
- GET|POST /api/v1/workspaces/{workspace_id}/groups（workspace 级上下文，总设计 §3.1）
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

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    authorize_read,
    request_trace,
)
from zhiwei.identity.commands import (
    IDEMPOTENCY_SCOPE_WORKSPACE_MEMBER_ADD,
    IdempotencyRequest,
    NameConflictError,
    ResourceConflictError,
    add_workspace_membership,
    canonical_request_digest,
    create_group,
    create_workspace,
)
from zhiwei.identity.domain import (
    ActorContext,
    Group,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    WorkspaceMembership,
)
from zhiwei.identity.repositories import IdentityRepository
from zhiwei.persistence.repositories import IdempotencyConflict, WorkspaceRecord
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
    set_tenant_context,
    tenant_session,
)
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import (
    WORKSPACE_SCOPED_ROLES,
    Action,
    Purpose,
    ResourceType,
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


class GrantWorkspaceMembershipRequest(BaseModel):
    """workspace membership 授予请求（角色词汇 fail closed：仅 workspace 作用域角色）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: UUID
    role_bindings: frozenset[str]


class WorkspaceMembershipView(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal_id: UUID
    organization_id: UUID
    workspace_id: UUID
    role_bindings: tuple[str, ...]


# workspace 作用域角色词汇：单一事实源 policy/roles.py（rego 的
# workspace_scoped_roles 与之同源）；未知角色在写路径早失败，不进库等读时 403
_WORKSPACE_ROLE_VOCABULARY = frozenset(
    role.value for role in WORKSPACE_SCOPED_ROLES
)


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
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """policy_enforcer 是生产纵切（repair addendum §3.2）的必需注入：create_app 组合时
    提供，任何直连调用点都不得绕过；缺失在构造期抛 TypeError（fail closed）。不得在
    端点内复制 gate 逻辑。"""
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")

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
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=organization_id,
            workspace_id=None,
            audit_action="organization.workspace.create",
            resource_type="workspace",
            policy_type=ResourceType.WORKSPACE_POLICY,
            # workspace 创建是 org 作用域动作（PERMISSIONS §3.1 行 2「Org Owner 配置」）。
            # 不得使用 configure_workspace：唯一允许角色 workspace_admin 是 workspace
            # 作用域，创建时目标尚不存在 → 矩阵结构性恒 deny（ADR-012 反例 4）。
            policy_action=Action.CONFIGURE,
            resource_id=request.workspace_id,
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
                if outcome.created:
                    # bootstrap：同事务内授予创建者 workspace_admin（与 org 创建
                    # 自动授予 owner 对称）。workspace_memberships 的 RLS 要求
                    # workspace GUC——同一事务内重设 GUC 后经 workspace 级 repo
                    # 写入；不授予则 manage_workspace_members 永远无人持有，
                    # workspace 生命周期不可运营（s1 spec §3 2026-09-03 增补）。
                    ws_context = TenantContext(
                        organization_id=actor.organization_id,
                        workspace_id=request.workspace_id,
                    )
                    await set_tenant_context(session, ws_context)
                    ws_repository = IdentityRepository(session, ws_context)
                    if (
                        await ws_repository.get_workspace_membership(
                            principal_id=actor.principal_id,
                            workspace_id=request.workspace_id,
                        )
                        is None
                    ):
                        await ws_repository.add_workspace_membership(
                            principal_id=actor.principal_id,
                            organization_id=actor.organization_id,
                            workspace_id=request.workspace_id,
                            role_bindings=frozenset({"workspace_admin"}),
                        )
                    await append_allowed_audit(
                        session,
                        actor=actor,
                        organization_id=actor.organization_id or organization_id,
                        workspace_id=None,
                        action="organization.workspace.create",
                        resource_type="workspace",
                        resource_id=request.workspace_id,
                        resource_version=1,
                        authorization=authorization,
                    )
            except _REQUEST_ERRORS as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=actor.organization_id or organization_id,
                    workspace_id=None,
                    action="organization.workspace.create",
                    resource_type="workspace",
                    resource_id=request.workspace_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
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
        # RLS 上下文：workspace 作用域 actor 必须与路径对齐（跨 scope 读 404，
        # 纵深防御，不依赖 PEP 配置正确）；org 作用域 actor（无 workspace 声明）
        # 落到路径 workspace——其授权由 PEP 的 configure_workspace 矩阵按
        # org 角色覆盖判定（ADR-014），RLS 与被读资源对齐。
        context = TenantContext(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id or workspace_id,
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
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=actor.organization_id,
            workspace_id=workspace_id,
            audit_action="workspace.group.create",
            resource_type="group",
            policy_type=ResourceType.WORKSPACE_POLICY,
            policy_action=Action.CONFIGURE_WORKSPACE,
            resource_id=request.group_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        # RLS 上下文：workspace 作用域 actor 必须与路径对齐（跨 scope 写 403）；
        # org 作用域 actor 落到路径 workspace——授权由 PEP 矩阵按 org 角色判定
        # （ADR-014），RLS 与被写资源对齐（语义同 list_workspace_groups）。
        context = TenantContext(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id or workspace_id,
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
                if outcome.created:
                    await append_allowed_audit(
                        session,
                        actor=actor,
                        organization_id=actor.organization_id,
                        workspace_id=actor.workspace_id,
                        action="workspace.group.create",
                        resource_type="group",
                        resource_id=request.group_id,
                        resource_version=1,
                        authorization=authorization,
                    )
            except _REQUEST_ERRORS as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    action="workspace.group.create",
                    resource_type="group",
                    resource_id=request.group_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                _reject_write(error)
        if outcome.created:
            return JSONResponse(content=outcome.response, status_code=status.HTTP_201_CREATED)
        return JSONResponse(content=outcome.response, status_code=status.HTTP_200_OK)

    # ------------------------------------------------- workspace membership 管理（ADR-012）

    @router.get(
        "/workspaces/{workspace_id}/memberships",
        response_model=list[WorkspaceMembershipView],
    )
    async def list_workspace_memberships(
        workspace_id: UUID,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[WorkspaceMembershipView]:
        """membership 列表的读路径授权（ADR-012 决策 4；D-1 修复）。

        - workspace 上下文 actor 声明的 workspace 必须与路径一致（GUC 纪律，
          与 groups 端点同款；不一致 404 防枚举）；
        - PEP read cell（org.read_memberships）：org_owner/security_admin/
          本 workspace 的 workspace_admin 可读，member 只有读自身（403）；
        - GUC 绑定路径 workspace：授权读者包含 org 作用域角色（org_owner/
          security_admin 的 actor.workspace_id 可为空），不能沿用
          actor.workspace_id 绑定。
        """
        if actor.organization_id is None:
            _reject_read(TenantContextRequired("organization context is required"))
        if actor.workspace_id is not None and actor.workspace_id != workspace_id:
            _reject_read(
                TenantScopeError("workspace context does not match target")
            )
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=actor.organization_id,
            workspace_id=workspace_id,
            policy_type=ResourceType.ORG,
            policy_action=Action.READ_MEMBERSHIPS,
            resource_id=workspace_id,
            trace_id=trace_id,
        )
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=workspace_id
        )
        async with tenant_session(sessions, context) as session:
            try:
                memberships = await IdentityRepository(
                    session, context
                ).list_workspace_memberships(workspace_id=workspace_id)
            except (TenantContextRequired, TenantScopeError) as error:
                _reject_read(error)
        return [
            WorkspaceMembershipView(
                principal_id=m.principal_id,
                organization_id=m.organization_id,
                workspace_id=m.workspace_id,
                role_bindings=tuple(sorted(m.role_bindings)),
            )
            for m in memberships
        ]

    @router.post("/workspaces/{workspace_id}/memberships")
    async def grant_workspace_membership(
        workspace_id: UUID,
        request: GrantWorkspaceMembershipRequest,
        request_scope: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
        ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> JSONResponse:
        """workspace_admin 授予 workspace membership（角色词汇 fail closed）。

        矩阵语义：org 资源的 manage_workspace_members（workspace_admin、workspace
        作用域）——这是 workspace_admin 的产生路径之外的管理面（s1 spec §3
        2026-09-03 增补）。
        """
        if actor.organization_id is None:
            _no_organization(TenantContextRequired("organization context is required"))
        unknown_roles = sorted(request.role_bindings - _WORKSPACE_ROLE_VOCABULARY)
        if unknown_roles:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown workspace roles: {unknown_roles}",
            )
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=actor.organization_id,
            workspace_id=workspace_id,
            audit_action="workspace.membership.grant",
            resource_type="membership",
            policy_type=ResourceType.ORG,
            policy_action=Action.MANAGE_WORKSPACE_MEMBERS,
            resource_id=request.principal_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=workspace_id
        )
        request_digest = _membership_grant_digest(request_scope.url.path, request)
        async with tenant_session(sessions, context) as session:
            repository = IdentityRepository(session, context)
            # 幂等消费（D-2）：同 key 同 digest 重放原结果；同 key 异 digest 409，
            # 不触发授予（与 add_org_membership 的命令级语义同构，因自然键早退
            # 位于端点而留在此处）
            idem_lookup = await repository.lookup_idempotency(
                scope=IDEMPOTENCY_SCOPE_WORKSPACE_MEMBER_ADD, key=idempotency_key
            )
            if idem_lookup is not None:
                if idem_lookup.request_digest != request_digest:
                    await append_failed_mutation_audit(
                        sessions,
                        actor=actor,
                        organization_id=actor.organization_id,
                        workspace_id=workspace_id,
                        action="workspace.membership.grant",
                        resource_type="membership",
                        resource_id=request.principal_id,
                        error=IdempotencyConflict(
                            "idempotency key was already used for another request"
                        ),
                        request_id=authorization.request_id,
                        trace_id=authorization.trace_id,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="idempotency key was already used for another request",
                    )
                return JSONResponse(
                    content=idem_lookup.response, status_code=status.HTTP_200_OK
                )
            existing = await repository.get_workspace_membership(
                principal_id=request.principal_id, workspace_id=workspace_id
            )
            if existing is not None:
                view = _membership_view(existing)
                return JSONResponse(
                    content=view, status_code=status.HTTP_200_OK
                )
            try:
                membership = await add_workspace_membership(
                    repository,
                    principal_id=request.principal_id,
                    organization_id=actor.organization_id,
                    workspace_id=workspace_id,
                    role_bindings=request.role_bindings,
                )
                view = _membership_view(membership)
                claimed = await repository.claim_idempotency(
                    scope=IDEMPOTENCY_SCOPE_WORKSPACE_MEMBER_ADD,
                    key=idempotency_key,
                    request_digest=request_digest,
                    response=view,
                )
                if not claimed.created:
                    return JSONResponse(
                        content=claimed.response, status_code=status.HTTP_200_OK
                    )
                await append_allowed_audit(
                    session,
                    actor=actor,
                    organization_id=actor.organization_id,
                    workspace_id=workspace_id,
                    action="workspace.membership.grant",
                    resource_type="membership",
                    resource_id=request.principal_id,
                    resource_version=1,
                    authorization=authorization,
                )
            except (PrincipalNotFoundError, PrincipalDisabledError) as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=actor.organization_id,
                    workspace_id=workspace_id,
                    action="workspace.membership.grant",
                    resource_type="membership",
                    resource_id=request.principal_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="principal not found"
                ) from error
            except _REQUEST_ERRORS as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=actor.organization_id,
                    workspace_id=workspace_id,
                    action="workspace.membership.grant",
                    resource_type="membership",
                    resource_id=request.principal_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                _reject_write(error)
        return JSONResponse(
            content=view, status_code=status.HTTP_201_CREATED
        )

    return router


def _membership_view(membership: WorkspaceMembership) -> dict[str, object]:
    return {
        "principal_id": str(membership.principal_id),
        "organization_id": str(membership.organization_id),
        "workspace_id": str(membership.workspace_id),
        "role_bindings": sorted(membership.role_bindings),
    }


def _membership_grant_digest(
    path: str, request: GrantWorkspaceMembershipRequest
) -> str:
    """授予请求的幂等 digest（A2-1）：逻辑请求的纯函数。

    role_bindings 是 frozenset——model_dump 的数组序取决于进程哈希种子，
    重启/滚动发布后同 key 同 body 的合法重放会得到假 409。digest 输入
    先排序，与序列化顺序无关（跨 PYTHONHASHSEED 逐字节一致）。
    """
    return canonical_request_digest(
        "POST",
        path,
        {
            "principal_id": str(request.principal_id),
            "role_bindings": sorted(request.role_bindings),
        },
    )
