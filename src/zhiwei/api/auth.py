"""OIDC BFF 路由与 session actor 依赖（S1-T2）。

契约（冻结）：
- GET /auth/login、GET /auth/callback、POST /auth/logout、GET /api/v1/me；
- cookie 只含高熵 opaque token（__Host- 前缀 + Secure + HttpOnly + SameSite=Lax +
  Path=/ + 无 Domain）；DB 只存 cookie token 的 SHA-256 hash；
- callback 总是签发全新 session token（防 fixation）；attempt 一次性消费；
- 所有 cookie-authenticated mutation 验证 server-side CSRF（X-CSRF-Token 与 session
  csrf hash 比对）+ 可信 Origin/Host，缺失或不匹配一律 403 且零 mutation；
- revoked / idle / absolute expired / disabled → 401 并清 cookie；
- 组织/工作区 context 必须来自已验证 membership；客户端声明只是请求，不是授权事实。
"""

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from zhiwei.contracts.time import utc_now
from zhiwei.identity.domain import ActorContext
from zhiwei.identity.oidc import OIDCValidationError, TokenExchangeError
from zhiwei.identity.sessions import (
    LoginAttemptExpiredError,
    MembershipScopeError,
    PrincipalLoginDeniedError,
    SessionService,
    UnknownPrincipalError,
)

SESSION_COOKIE = "__Host-zhiwei_session"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOGIN_ERRORS = (
    LoginAttemptExpiredError,
    UnknownPrincipalError,
    PrincipalLoginDeniedError,
    OIDCValidationError,
    TokenExchangeError,
)


def _cookie_flags() -> dict[str, Any]:
    """session cookie 固定属性：__Host- 前缀 + Secure + HttpOnly + SameSite=Lax + Path=/。

    冻结契约要求 Set-Cookie 携带 SameSite=Lax（starlette 按传入值渲染，大小写即值）；
    type ignore 是故意的：starlette 的类型只接受小写 Literal，但运行期断言只看
    lower()，传 "Lax" 是合法且契约要求的写法。
    """
    return {
        "secure": True,
        "httponly": True,
        "samesite": "Lax",  # type: ignore[typeddict-item]
        "path": "/",
    }


def _require_csrf(
    request: Request, session_service: SessionService, session: Any, cookie_token: str
) -> None:
    """cookie-authenticated mutation 的 CSRF 门禁：X-CSRF-Token + 可信 Origin/Host。"""
    origin = request.headers.get("Origin")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin is None or origin != expected_origin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="untrusted origin"
        )
    presented = request.headers.get("X-CSRF-Token")
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="missing csrf token"
        )
    expected = session_service.csrf_token(session, cookie_token)
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="csrf token mismatch"
        )
    presented_hash = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(presented_hash, session.csrf_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="csrf hash mismatch"
        )


def create_session_actor_dependency(
    session_service: SessionService,
) -> Callable[..., Awaitable[ActorContext]]:
    """真实 session actor：cookie → session → principal → CSRF/Origin → membership。

    结果写入 request.state（session / session_cookie / csrf_token）供 /me 与 logout 使用。
    """

    async def session_actor(request: Request) -> ActorContext:
        cookie_token = request.cookies.get(SESSION_COOKIE)
        if cookie_token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="session required"
            )
        session = await session_service.authenticate_cookie(cookie_token)
        if session is None:
            request.state.clear_session_cookie = True
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or expired session",
            )
        request.state.session = session
        request.state.session_cookie = cookie_token
        if request.method not in _SAFE_METHODS:
            _require_csrf(request, session_service, session, cookie_token)
        organization_id = request.headers.get("X-ZhiWei-Organization")
        workspace_id = request.headers.get("X-ZhiWei-Workspace")
        try:
            context = await session_service.resolve_context(
                session.principal_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
        except MembershipScopeError as error:
            if request.method in _SAFE_METHODS:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="resource not found"
                ) from error
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="outside tenant scope"
            ) from error
        return context

    return session_actor


def create_auth_router(
    *,
    session_service: SessionService,
    oidc_service: Any,
    session_actor: Callable[..., Awaitable[ActorContext]],
) -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.get("/auth/login")
    async def login() -> Response:
        authorization_url = await session_service.create_login_attempt()
        return RedirectResponse(authorization_url, status_code=status.HTTP_302_FOUND)

    @router.get("/auth/callback")
    async def callback(code: str, state: str) -> Response:
        try:
            session, cookie_token = await session_service.complete_login(
                code=code, state=state
            )
        except _LOGIN_ERRORS as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="login failed"
            ) from error
        response = RedirectResponse("/", status_code=status.HTTP_302_FOUND)
        max_age = max(1, int((session.expires_at - utc_now()).total_seconds()))
        response.set_cookie(SESSION_COOKIE, cookie_token, max_age=max_age, **_cookie_flags())
        return response

    @router.post("/auth/logout")
    async def logout(
        request: Request, actor: Annotated[ActorContext, Depends(session_actor)]
    ) -> Response:
        session = request.state.session
        # 先可靠本地 revoke（CAS），再 best-effort IdP revoke；IdP 不可用不影响结果
        await session_service.revoke_session(session.id, expected_version=session.version)
        try:
            payload = await session_service.decrypt_tokens(session)
            await session_service.revoke_tokens_at_idp(payload)
        except Exception:
            pass
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(SESSION_COOKIE, **_cookie_flags())
        return response

    @router.get("/api/v1/me")
    async def me(
        request: Request,
        actor: Annotated[ActorContext, Depends(session_actor)],
    ) -> dict[str, Any]:
        session = request.state.session
        cookie_token = request.state.session_cookie
        principal = await session_service.principal(session.principal_id)
        memberships = await session_service.memberships(session.principal_id)
        organizations = [
            {"id": str(row["organization_id"]), "status": row["organization_status"]}
            for row in memberships
            if row["scope"] == "organization"
        ]
        if actor.organization_id is not None:
            context = {
                "organization_id": str(actor.organization_id),
                "workspace_id": str(actor.workspace_id) if actor.workspace_id else None,
            }
        elif len(organizations) == 1:
            context = {"organization_id": organizations[0]["id"], "workspace_id": None}
        else:
            context = {"organization_id": None, "workspace_id": None}
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="principal missing"
            )
        return {
            "principal": {
                "id": str(principal.id),
                "kind": principal.kind.value,
                "status": principal.status.value,
            },
            "organizations": organizations,
            "context": context,
            "csrf_token": session_service.csrf_token(session, cookie_token),
        }

    return router
