"""S1-T5 SCIM 2.0 端点（/scim/v2）：User/Group 必需子集与 membership 生命周期。

冻结契约（docs/handoffs/s1-t5-design.md §1-§10，以冻结契约为准）：
- 子集矩阵：仅 create/update/disable + GET by id（User）、create + member
  reconciliation + GET/list（Group）；其余操作显式 501 + RFC 7644 §3.12 错误体；
- 认证：OIDC BFF 会话（复用 session_actor，表现层包装转换错误体，不造第二套
  auth）；读写统一经 api.policy_gate.authorize_mutation（policy_type=ORG、
  policy_action=MANAGE，Rego 矩阵 org.manage）——不复制 gate 逻辑、不建第二套
  权限矩阵；allowed 读不写审计、denied 读写 denied 审计；
- 错误体：全部 SCIM 4xx/5xx 使用 RFC 7644 §3.12 形状（status 为字符串）；端点
  以 JSONResponse 直接返回（不依赖 app 级 exception handler，contract 测试的裸
  FastAPI 也成立）；路径 UUID 以 str 接收手工解析（400 invalidValue，避免 422）；
  body 以 dict 接收、pydantic 手工校验（未知属性 → 400 invalidSyntax）；
- issuer = ZHIWEI_OIDC_ISSUER（组合时注入）；externalId ≡ subject（user）、
  externalId ≡ displayName（group name）；PUT userName≠subject / displayName≠
  group name → 400 mutability；PATCH 仅 op=replace+path=active；
- 审计：action 见设计 §9；business 拒绝（重复键/成员非法）→ failed 审计 +
  409/400；跨引擎边界（user 类 mutation 的 identity 写与 allowed 审计不在同一
  DB 事务）登记为设计 §13 遗留。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Body,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    request_trace,
)
from zhiwei.identity.domain import (
    ActorContext,
    ExternalIdentityConflictError,
    NameConflictError,
    PrincipalDisabledError,
    PrincipalNotFoundError,
)
from zhiwei.identity.scim import (
    GROUP_SCHEMA,
    USER_SCHEMA,
    ScimService,
)
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

_BUSINESS_ERRORS = (
    ExternalIdentityConflictError,
    NameConflictError,
    PrincipalNotFoundError,
    PrincipalDisabledError,
)

_MAX_PAGE = 1000


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schemas: list[str]
    externalId: str
    userName: str
    active: bool = True


class ReplaceUserRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schemas: list[str]
    userName: str
    active: bool


class PatchOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str
    path: str | None = None
    value: Any | None = None


class PatchUserRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schemas: list[str]
    Operations: list[PatchOperation]


class MemberRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schemas: list[str]
    externalId: str
    displayName: str
    members: list[MemberRef] = []


class ReplaceGroupRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schemas: list[str]
    externalId: str
    displayName: str
    members: list[MemberRef]


def _scim_http(
    status_code: int, *, scim_type: str | None = None, detail: str
) -> HTTPException:
    """构造 SCIM 形状的 HTTPException（detail 为 RFC 7644 §3.12 错误体 dict）。

    端点 try 块内 raise；except HTTPException 经 _scim_response 转 JSONResponse。
    """
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(status_code),
        "detail": detail,
    }
    if scim_type is not None:
        body["scimType"] = scim_type
    return HTTPException(status_code=status_code, detail=body)


def _scim_error_json(
    status_code: int, *, scim_type: str | None = None, detail: str
) -> JSONResponse:
    """直接返回 SCIM 错误体 JSONResponse（不依赖 app 级 handler）。"""
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(status_code),
        "detail": detail,
    }
    if scim_type is not None:
        body["scimType"] = scim_type
    return JSONResponse(content=body, status_code=status_code)


def _scim_response(exc: HTTPException) -> JSONResponse:
    """HTTPException → SCIM 错误体 JSONResponse。

    SCIM 形状（dict detail，来自 _scim_http）原样返回；string detail（来自
    session_actor 401/403 或 authorize_mutation 403）包裹为 SCIM 形状。状态码
    不变；session_actor 设置的 request.state.clear_session_cookie 由中间件处理。
    """
    if isinstance(exc.detail, dict) and exc.detail.get("schemas") == [ERROR_SCHEMA]:
        return JSONResponse(content=exc.detail, status_code=exc.status_code)
    return _scim_error_json(exc.status_code, detail=str(exc.detail))


def _business_scim_response(error: Exception) -> JSONResponse:
    """policy 放行后的业务拒绝 → SCIM 错误体（failed 审计已由调用方写入）。"""
    if isinstance(error, ExternalIdentityConflictError):
        return _scim_error_json(
            409, scim_type="uniqueness", detail="external identity already exists"
        )
    if isinstance(error, NameConflictError):
        return _scim_error_json(
            409, scim_type="uniqueness", detail="group external id already exists"
        )
    if isinstance(error, PrincipalNotFoundError):
        return _scim_error_json(
            400, scim_type="invalidValue", detail="member principal does not exist"
        )
    if isinstance(error, PrincipalDisabledError):
        return _scim_error_json(
            400, scim_type="invalidValue", detail="member principal is disabled"
        )
    raise error


def _parse_uuid(value: str, *, what: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _scim_http(
            400, scim_type="invalidValue", detail=f"{what} is not a valid UUID"
        ) from exc


def _validate_schemas(schemas: list[str], *expected: str) -> None:
    if not any(s in schemas for s in expected):
        raise _scim_http(
            400,
            scim_type="invalidSyntax",
            detail="schemas must include a recognized SCIM schema",
        )


def _parse_members(members: list[MemberRef]) -> list[UUID]:
    parsed: list[UUID] = []
    for ref in members:
        try:
            parsed.append(UUID(ref.value))
        except (ValueError, AttributeError) as exc:
            raise _scim_http(
                400, scim_type="invalidValue", detail="member value is not a valid UUID"
            ) from exc
    return parsed


def _create_user_validated(payload: Any) -> CreateUserRequest:
    if not isinstance(payload, dict):
        raise _scim_http(
            400, scim_type="invalidSyntax", detail="request body must be a JSON object"
        )
    try:
        body = CreateUserRequest(**payload)
    except ValidationError as exc:
        raise _scim_http(
            400, scim_type="invalidSyntax", detail=str(exc.errors())
        ) from exc
    _validate_schemas(body.schemas, USER_SCHEMA)
    if body.externalId != body.userName:
        raise _scim_http(
            400,
            scim_type="invalidValue",
            detail="userName must equal externalId in this subset",
        )
    return body


def _replace_user_validated(payload: Any) -> ReplaceUserRequest:
    if not isinstance(payload, dict):
        raise _scim_http(
            400, scim_type="invalidSyntax", detail="request body must be a JSON object"
        )
    try:
        body = ReplaceUserRequest(**payload)
    except ValidationError as exc:
        raise _scim_http(
            400, scim_type="invalidSyntax", detail=str(exc.errors())
        ) from exc
    _validate_schemas(body.schemas, USER_SCHEMA)
    return body


def _patch_user_validated(payload: Any) -> PatchUserRequest:
    if not isinstance(payload, dict):
        raise _scim_http(
            400, scim_type="invalidSyntax", detail="request body must be a JSON object"
        )
    try:
        body = PatchUserRequest(**payload)
    except ValidationError as exc:
        raise _scim_http(
            400, scim_type="invalidSyntax", detail=str(exc.errors())
        ) from exc
    _validate_schemas(body.schemas, PATCH_SCHEMA)
    if not body.Operations:
        raise _scim_http(
            400, scim_type="invalidSyntax", detail="Operations must not be empty"
        )
    for operation in body.Operations:
        if operation.op != "replace":
            raise _scim_http(501, detail=f"patch op '{operation.op}' is not supported")
        if operation.path != "active":
            raise _scim_http(
                400,
                scim_type="noTarget",
                detail=f"patch path '{operation.path}' is not supported",
            )
        if not isinstance(operation.value, bool):
            raise _scim_http(
                400,
                scim_type="invalidValue",
                detail="patch value for 'active' must be a boolean",
            )
    return body


def _create_group_validated(payload: Any) -> CreateGroupRequest:
    if not isinstance(payload, dict):
        raise _scim_http(
            400, scim_type="invalidSyntax", detail="request body must be a JSON object"
        )
    try:
        body = CreateGroupRequest(**payload)
    except ValidationError as exc:
        raise _scim_http(
            400, scim_type="invalidSyntax", detail=str(exc.errors())
        ) from exc
    _validate_schemas(body.schemas, GROUP_SCHEMA)
    if body.externalId != body.displayName:
        raise _scim_http(
            400,
            scim_type="invalidValue",
            detail="displayName must equal externalId in this subset",
        )
    return body


def _replace_group_validated(payload: Any) -> ReplaceGroupRequest:
    if not isinstance(payload, dict):
        raise _scim_http(
            400, scim_type="invalidSyntax", detail="request body must be a JSON object"
        )
    try:
        body = ReplaceGroupRequest(**payload)
    except ValidationError as exc:
        raise _scim_http(
            400, scim_type="invalidSyntax", detail=str(exc.errors())
        ) from exc
    _validate_schemas(body.schemas, GROUP_SCHEMA)
    if body.externalId != body.displayName:
        raise _scim_http(
            400,
            scim_type="invalidValue",
            detail="displayName must equal externalId in this subset",
        )
    return body


def _actor_accepts_request(dependency: Callable[..., Any]) -> bool:
    """session_actor(request) vs 零参 stub：探测首个位置形参是否存在。

    组合期一次性求值缓存，避免每请求 inspect.signature 开销。生产恒为
    session_actor(request)（POSITIONAL_OR_KEYWORD request）→ True；contract
    测试的零参 stub → False。
    """
    params = inspect.signature(dependency).parameters
    return any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params.values()
    )


def create_scim_router(
    *,
    actor_dependency: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
    issuer: str,
) -> APIRouter:
    """组合期必需注入（fail closed，同 T4 router factory）：policy_enforcer 显式
    None → TypeError；缺参数 → Python 原生 TypeError。issuer = 部署期固定的
    ZHIWEI_OIDC_ISSUER（SCIM external identity 绑定键的 issuer 侧）。

    actor_dependency 不经 FastAPI Depends：端点在 try 块内调用 _resolve_actor，
    捕获 HTTPException（401/403 来自 session_actor）并直接返回 SCIM JSONResponse
    ——不依赖 app 级 exception handler，contract 测试的裸 FastAPI 也成立。
    """
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")

    service = ScimService(
        identity_sessions=identity_sessions,
        app_sessions=sessions,
        issuer=issuer,
    )
    accepts_request = _actor_accepts_request(actor_dependency)

    async def _resolve_actor(request: Request) -> ActorContext:
        """调用 actor_dependency；HTTPException 由调用方 try/except 捕获转 SCIM。"""
        result = actor_dependency(request) if accepts_request else actor_dependency()
        if inspect.isawaitable(result):
            return await result
        return result

    router = APIRouter(prefix="/scim/v2", tags=["scim"])

    # ---------------------------------------------------------- unsupported（501 矩阵）

    @router.get("/Users")
    async def list_users() -> JSONResponse:
        return _scim_error_json(
            501, detail="GET /Users (list) is not supported in this subset"
        )

    @router.delete("/Users/{user_id}")
    async def delete_user(user_id: str) -> JSONResponse:
        return _scim_error_json(
            501,
            detail="DELETE /Users is not supported (disable via PATCH/PUT active=false)",
        )

    @router.patch("/Groups/{group_id}")
    async def patch_group(group_id: str) -> JSONResponse:
        return _scim_error_json(
            501, detail="PATCH /Groups is not supported (use PUT replace)"
        )

    @router.delete("/Groups/{group_id}")
    async def delete_group(group_id: str) -> JSONResponse:
        return _scim_error_json(501, detail="DELETE /Groups is not supported in this subset")

    @router.post("/Bulk")
    async def bulk() -> JSONResponse:
        return _scim_error_json(501, detail="Bulk is not supported in this subset")

    @router.get("/Me")
    async def me() -> JSONResponse:
        return _scim_error_json(501, detail="/Me is not supported in this subset")

    @router.get("/ServiceProviderConfig")
    async def spc() -> JSONResponse:
        return _scim_error_json(
            501, detail="ServiceProviderConfig is not supported in this subset"
        )

    @router.get("/ResourceTypes")
    async def resource_types() -> JSONResponse:
        return _scim_error_json(
            501, detail="ResourceTypes is not supported in this subset"
        )

    @router.get("/Schemas")
    async def schemas() -> JSONResponse:
        return _scim_error_json(501, detail="Schemas is not supported in this subset")

    @router.post("/.search")
    async def search() -> JSONResponse:
        return _scim_error_json(501, detail=".search is not supported in this subset")

    # ------------------------------------------------------------------ POST /Users

    @router.post("/Users")
    async def create_user_endpoint(
        request: Request,
        payload: Annotated[dict | None, Body()] = None,
    ) -> JSONResponse:
        # 双层 try：Layer 1 解析 actor+校验+authorize（HTTPException），Layer 2
        # service+audit（business errors）；authorization 等变量在 Layer 1 赋值，
        # Layer 2 的 except 引用时已绑定（pyright possibly-unbound 消除）。
        try:
            actor = await _resolve_actor(request)
            if actor.organization_id is None:
                raise _scim_http(403, detail="organization context required")
            body = _create_user_validated(payload)
            principal_id = uuid4()
            request_id, trace_id = request_trace(request)
            authorization = await authorize_mutation(
                enforcer=policy_enforcer,
                sessions=sessions,
                actor=actor,
                bootstrap=False,
                organization_id=actor.organization_id,
                workspace_id=None,
                audit_action="scim.user.create",
                resource_type="principal",
                policy_type=ResourceType.ORG,
                policy_action=Action.MANAGE,
                resource_id=principal_id,
                resource_version=1,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
            )
        except HTTPException as exc:
            return _scim_response(exc)
        try:
            resource = await service.create_user(
                principal_id=principal_id, external_id=body.externalId, active=body.active
            )
            context = TenantContext(organization_id=actor.organization_id)
            async with tenant_session(sessions, context) as session:
                await append_allowed_audit(
                    session,
                    actor=actor,
                    organization_id=actor.organization_id,
                    workspace_id=None,
                    action="scim.user.create",
                    resource_type="principal",
                    resource_id=principal_id,
                    resource_version=1,
                    authorization=authorization,
                )
        except HTTPException as exc:
            return _scim_response(exc)
        except _BUSINESS_ERRORS as error:
            await append_failed_mutation_audit(
                sessions,
                actor=actor,
                organization_id=actor.organization_id,
                workspace_id=None,
                action="scim.user.create",
                resource_type="principal",
                resource_id=principal_id,
                error=error,
                request_id=authorization.request_id,
                trace_id=authorization.trace_id,
            )
            return _business_scim_response(error)
        return JSONResponse(
            content=resource.model_dump(mode="json"),
            status_code=status.HTTP_201_CREATED,
            headers={"location": f"/scim/v2/Users/{principal_id}"},
        )

    # ----------------------------------------------------------- GET /Users/{id}

    @router.get("/Users/{user_id}")
    async def get_user_endpoint(
        request: Request,
        user_id: str,
    ) -> JSONResponse:
        try:
            actor = await _resolve_actor(request)
            if actor.organization_id is None:
                raise _scim_http(403, detail="organization context required")
            principal_id = _parse_uuid(user_id, what="user id")
            request_id, trace_id = request_trace(request)
            await authorize_mutation(
                enforcer=policy_enforcer,
                sessions=sessions,
                actor=actor,
                bootstrap=False,
                organization_id=actor.organization_id,
                workspace_id=None,
                audit_action="scim.user.read",
                resource_type="principal",
                policy_type=ResourceType.ORG,
                policy_action=Action.MANAGE,
                resource_id=principal_id,
                resource_version=0,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
            )
            resource = await service.get_user(principal_id)
        except HTTPException as exc:
            return _scim_response(exc)
        if resource is None:
            return _scim_error_json(404, detail="user not found")
        return JSONResponse(content=resource.model_dump(mode="json"))

    # ------------------------------------------------------- PUT / PATCH /Users/{id}

    @router.put("/Users/{user_id}")
    async def replace_user_endpoint(
        request: Request,
        user_id: str,
        payload: Annotated[dict | None, Body()] = None,
        if_match: Annotated[str | None, Header()] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        return await _replace_or_patch_user(
            request, user_id, payload, if_match=if_match, if_none_match=if_none_match, method="PUT"
        )

    @router.patch("/Users/{user_id}")
    async def patch_user_endpoint(
        request: Request,
        user_id: str,
        payload: Annotated[dict | None, Body()] = None,
        if_match: Annotated[str | None, Header()] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        return await _replace_or_patch_user(
            request, user_id, payload, if_match=if_match, if_none_match=if_none_match, method="PATCH"
        )

    async def _replace_or_patch_user(
        request: Request,
        user_id: str,
        payload: dict | None,
        *,
        if_match: str | None,
        if_none_match: str | None,
        method: str,
    ) -> JSONResponse:
        # 双层 try（同 create_user_endpoint）：Layer 1 绑定 actor/body/authorization，
        # Layer 2 引用时已绑定。
        body: ReplaceUserRequest | None = None
        try:
            actor = await _resolve_actor(request)
            if actor.organization_id is None:
                raise _scim_http(403, detail="organization context required")
            principal_id = _parse_uuid(user_id, what="user id")
            if if_match is not None or if_none_match is not None:
                raise _scim_http(
                    400, detail="versioning (If-Match/If-None-Match/ETag) is not supported"
                )
            if method == "PUT":
                body = _replace_user_validated(payload)
                active = body.active
            else:
                patch_body = _patch_user_validated(payload)
                active = bool(patch_body.Operations[0].value)
            audit_action = "scim.user.enable" if active else "scim.user.disable"
            request_id, trace_id = request_trace(request)
            authorization = await authorize_mutation(
                enforcer=policy_enforcer,
                sessions=sessions,
                actor=actor,
                bootstrap=False,
                organization_id=actor.organization_id,
                workspace_id=None,
                audit_action=audit_action,
                resource_type="principal",
                policy_type=ResourceType.ORG,
                policy_action=Action.MANAGE,
                resource_id=principal_id,
                resource_version=0,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
            )
        except HTTPException as exc:
            return _scim_response(exc)
        try:
            if method == "PUT":
                assert body is not None  # Layer 1 PUT 分支赋值
                existing = await service.get_user(principal_id)
                if existing is None:
                    raise _scim_http(404, detail="user not found")
                if existing.userName != body.userName:
                    raise _scim_http(
                        400,
                        scim_type="mutability",
                        detail="userName is immutable in this subset",
                    )
            resource, changed = await service.set_user_status(principal_id, active=active)
            if changed:
                context = TenantContext(organization_id=actor.organization_id)
                async with tenant_session(sessions, context) as session:
                    await append_allowed_audit(
                        session,
                        actor=actor,
                        organization_id=actor.organization_id,
                        workspace_id=None,
                        action=audit_action,
                        resource_type="principal",
                        resource_id=principal_id,
                        resource_version=1,
                        authorization=authorization,
                    )
        except HTTPException as exc:
            return _scim_response(exc)
        except _BUSINESS_ERRORS as error:
            await append_failed_mutation_audit(
                sessions,
                actor=actor,
                organization_id=actor.organization_id,
                workspace_id=None,
                action=audit_action,
                resource_type="principal",
                resource_id=principal_id,
                error=error,
                request_id=authorization.request_id,
                trace_id=authorization.trace_id,
            )
            return _business_scim_response(error)
        return JSONResponse(content=resource.model_dump(mode="json"))

    # ------------------------------------------------------------------ POST /Groups

    @router.post("/Groups")
    async def create_group_endpoint(
        request: Request,
        payload: Annotated[dict | None, Body()] = None,
    ) -> JSONResponse:
        try:
            actor = await _resolve_actor(request)
            if actor.organization_id is None:
                raise _scim_http(403, detail="organization context required")
            if actor.workspace_id is None:
                raise _scim_http(403, detail="workspace context required")
            body = _create_group_validated(payload)
            member_ids = _parse_members(body.members)
            group_id = uuid4()
            request_id, trace_id = request_trace(request)
            authorization = await authorize_mutation(
                enforcer=policy_enforcer,
                sessions=sessions,
                actor=actor,
                bootstrap=False,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                audit_action="scim.group.create",
                resource_type="group",
                policy_type=ResourceType.ORG,
                policy_action=Action.MANAGE,
                resource_id=group_id,
                resource_version=1,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
            )
        except HTTPException as exc:
            return _scim_response(exc)
        try:
            resource = await service.create_group(
                group_id=group_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                name=body.externalId,
                member_ids=member_ids,
            )
            context = TenantContext(
                organization_id=actor.organization_id, workspace_id=actor.workspace_id
            )
            async with tenant_session(sessions, context) as session:
                await append_allowed_audit(
                    session,
                    actor=actor,
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    action="scim.group.create",
                    resource_type="group",
                    resource_id=group_id,
                    resource_version=1,
                    authorization=authorization,
                )
        except HTTPException as exc:
            return _scim_response(exc)
        except _BUSINESS_ERRORS as error:
            await append_failed_mutation_audit(
                sessions,
                actor=actor,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                action="scim.group.create",
                resource_type="group",
                resource_id=group_id,
                error=error,
                request_id=authorization.request_id,
                trace_id=authorization.trace_id,
            )
            return _business_scim_response(error)
        return JSONResponse(
            content=resource.model_dump(mode="json"),
            status_code=status.HTTP_201_CREATED,
            headers={"location": f"/scim/v2/Groups/{group_id}"},
        )

    # ------------------------------------------------------------------- GET /Groups

    @router.get("/Groups")
    async def list_groups_endpoint(
        request: Request,
        scim_filter: Annotated[str | None, Query(alias="filter")] = None,
        start_index: Annotated[str | None, Query(alias="startIndex")] = None,
        count: Annotated[str | None, Query()] = None,
    ) -> JSONResponse:
        try:
            actor = await _resolve_actor(request)
            if actor.organization_id is None:
                raise _scim_http(403, detail="organization context required")
            if actor.workspace_id is None:
                raise _scim_http(403, detail="workspace context required")
            if scim_filter is not None:
                raise _scim_http(
                    400,
                    scim_type="invalidFilter",
                    detail="filter is not supported in this subset",
                )
            try:
                start = int(start_index) if start_index is not None else 1
                page = int(count) if count is not None else 100
            except (TypeError, ValueError) as exc:
                raise _scim_http(
                    400,
                    scim_type="invalidValue",
                    detail="startIndex/count must be integers",
                ) from exc
            if start < 1 or page < 1:
                raise _scim_http(
                    400,
                    scim_type="invalidValue",
                    detail="startIndex/count must be >= 1",
                )
            page = min(page, _MAX_PAGE)
            request_id, trace_id = request_trace(request)
            await authorize_mutation(
                enforcer=policy_enforcer,
                sessions=sessions,
                actor=actor,
                bootstrap=False,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                audit_action="scim.group.read",
                resource_type="group",
                policy_type=ResourceType.ORG,
                policy_action=Action.MANAGE,
                resource_id=actor.workspace_id,
                resource_version=0,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
            )
            result = await service.list_groups(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                start_index=start,
                count=page,
            )
        except HTTPException as exc:
            return _scim_response(exc)
        return JSONResponse(content=result.model_dump(mode="json"))

    # ----------------------------------------------------------- GET /Groups/{id}

    @router.get("/Groups/{group_id}")
    async def get_group_endpoint(
        request: Request,
        group_id: str,
    ) -> JSONResponse:
        try:
            actor = await _resolve_actor(request)
            if actor.organization_id is None:
                raise _scim_http(403, detail="organization context required")
            if actor.workspace_id is None:
                raise _scim_http(403, detail="workspace context required")
            gid = _parse_uuid(group_id, what="group id")
            request_id, trace_id = request_trace(request)
            await authorize_mutation(
                enforcer=policy_enforcer,
                sessions=sessions,
                actor=actor,
                bootstrap=False,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                audit_action="scim.group.read",
                resource_type="group",
                policy_type=ResourceType.ORG,
                policy_action=Action.MANAGE,
                resource_id=gid,
                resource_version=0,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
            )
            resource = await service.get_group(
                group_id=gid,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            )
        except HTTPException as exc:
            return _scim_response(exc)
        if resource is None:
            return _scim_error_json(404, detail="group not found")
        return JSONResponse(content=resource.model_dump(mode="json"))

    # ----------------------------------------------------------- PUT /Groups/{id}

    @router.put("/Groups/{group_id}")
    async def replace_group_endpoint(
        request: Request,
        group_id: str,
        payload: Annotated[dict | None, Body()] = None,
        if_match: Annotated[str | None, Header()] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        try:
            actor = await _resolve_actor(request)
            if actor.organization_id is None:
                raise _scim_http(403, detail="organization context required")
            if actor.workspace_id is None:
                raise _scim_http(403, detail="workspace context required")
            gid = _parse_uuid(group_id, what="group id")
            if if_match is not None or if_none_match is not None:
                raise _scim_http(
                    400, detail="versioning (If-Match/If-None-Match/ETag) is not supported"
                )
            body = _replace_group_validated(payload)
            member_ids = _parse_members(body.members)
            request_id, trace_id = request_trace(request)
            authorization = await authorize_mutation(
                enforcer=policy_enforcer,
                sessions=sessions,
                actor=actor,
                bootstrap=False,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                audit_action="scim.group.reconcile",
                resource_type="group",
                policy_type=ResourceType.ORG,
                policy_action=Action.MANAGE,
                resource_id=gid,
                resource_version=1,
                purpose=Purpose.GENERAL,
                request_id=request_id,
                trace_id=trace_id,
            )
        except HTTPException as exc:
            return _scim_response(exc)
        try:
            existing = await service.get_group(
                group_id=gid,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            )
            if existing is None:
                raise _scim_http(404, detail="group not found")
            if existing.displayName != body.externalId:
                raise _scim_http(
                    400,
                    scim_type="mutability",
                    detail="group displayName (externalId) is immutable in this subset",
                )
            outcome = await service.reconcile_group(
                group_id=gid,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                name=body.externalId,
                member_ids=member_ids,
            )
            assert outcome is not None
            resource, changed = outcome
            if changed:
                context = TenantContext(
                    organization_id=actor.organization_id, workspace_id=actor.workspace_id
                )
                async with tenant_session(sessions, context) as session:
                    await append_allowed_audit(
                        session,
                        actor=actor,
                        organization_id=actor.organization_id,
                        workspace_id=actor.workspace_id,
                        action="scim.group.reconcile",
                        resource_type="group",
                        resource_id=gid,
                        resource_version=1,
                        authorization=authorization,
                    )
        except HTTPException as exc:
            return _scim_response(exc)
        except _BUSINESS_ERRORS as error:
            await append_failed_mutation_audit(
                sessions,
                actor=actor,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                action="scim.group.reconcile",
                resource_type="group",
                resource_id=gid,
                error=error,
                request_id=authorization.request_id,
                trace_id=authorization.trace_id,
            )
            return _business_scim_response(error)
        return JSONResponse(content=resource.model_dump(mode="json"))

    return router
