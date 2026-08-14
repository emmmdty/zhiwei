"""S1-T2 RED skeleton：OIDC BFF 路由与 session actor 依赖。

契约（冻结）：
- GET /auth/login、GET /auth/callback、POST /auth/logout、GET /api/v1/me；
- cookie 只含高熵 opaque token（__Host- 前缀 + Secure + HttpOnly + SameSite=Lax +
  Path=/ + 无 Domain）；DB 只存 cookie token 的 SHA-256 hash；
- callback 总是签发全新 session token（防 fixation）；attempt 一次性消费；
- 所有 cookie-authenticated mutation 验证 server-side CSRF（X-CSRF-Token 与 session
  csrf hash 比对）+ 可信 Origin/Host，缺失或不匹配一律 403；
- revoked / idle / absolute expired / disabled → 401 并清 cookie；
- 组织/工作区 context 必须来自已验证 membership；客户端声明只是请求，不是授权事实。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request

from zhiwei.identity.domain import ActorContext

SESSION_COOKIE = "__Host-zhiwei_session"


def create_session_actor_dependency(
    session_service: Any,
) -> Callable[..., Awaitable[ActorContext]]:
    """真实 session actor：cookie → session → principal → 校验 CSRF/Origin → membership。

    GREEN 实现；RED 阶段占位（每次请求即失败，保证行为测试在安全边界上失败）。
    """

    async def session_actor(request: Request) -> ActorContext:
        raise NotImplementedError("S1-T2 session actor 未实现")

    return session_actor


def create_auth_router(
    *,
    session_service: Any,
    oidc_service: Any,
    session_actor: Callable[..., ActorContext],
) -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.get("/auth/login")
    async def login() -> Any:
        raise NotImplementedError("S1-T2 login 未实现")

    @router.get("/auth/callback")
    async def callback() -> Any:
        raise NotImplementedError("S1-T2 callback 未实现")

    @router.post("/auth/logout")
    async def logout() -> Any:
        raise NotImplementedError("S1-T2 logout 未实现")

    @router.get("/api/v1/me")
    async def me() -> Any:
        raise NotImplementedError("S1-T2 me 未实现")

    return router
