"""S1-T2 RED skeleton：AuthSession 存储与会话服务。

契约（冻结）：
- AuthSession 为 principal/session 级；所有更新用 expected_version CAS（防 ABA）；
- cookie 查找只按 SHA-256 hash，每次请求不解密 provider token；
- refresh：数据库 CAS + 有界 lease 竞争 ownership，多 replica 恰好一次 IdP refresh，
  输家读取 winner 新版本，不使用旧 refresh token；invalid_grant / provider revoke /
  绝对过期 / 不可恢复错误 → 本地 revoke，fail closed；
- logout 先可靠本地 revoke 并清 cookie；IdP revoke 不可用不能恢复本地 session；
- disabled / 非 User principal 禁止创建交互会话，既有 session 立即失效。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from zhiwei.identity.domain import AuthSession, LoginAttempt


class SessionConflictError(RuntimeError):
    """expected_version 过期 / refresh lease 竞争失败：fail closed。"""


class SessionRevokedError(RuntimeError):
    """session 已 revoke（本地或 IdP），拒绝 refresh 等操作。"""


class UnknownPrincipalError(RuntimeError):
    """(issuer, subject) 未绑定任何 principal（T2 不实现 JIT）。"""


class PrincipalLoginDeniedError(RuntimeError):
    """disabled 或非 User principal 禁止交互登录。"""


class AuthSessionStore:
    """auth_sessions / oidc_login_attempts 数据访问（identity 引擎）。"""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def create_login_attempt(self, attempt: LoginAttempt) -> None:
        raise NotImplementedError("S1-T2 login attempt 存储未实现")

    async def consume_login_attempt(self, state: str) -> LoginAttempt | None:
        """按 state 原子一次性消费；已消费 / 不存在返回 None。"""
        raise NotImplementedError("S1-T2 login attempt 消费未实现")

    async def create_session(self, session: AuthSession) -> None:
        raise NotImplementedError("S1-T2 session 创建未实现")

    async def get_session_by_token(self, cookie_token: str) -> AuthSession | None:
        raise NotImplementedError("S1-T2 session 查找未实现")

    async def get_session(self, session_id: UUID) -> AuthSession | None:
        raise NotImplementedError("S1-T2 session 读取未实现")

    async def revoke_session(self, session_id: UUID, expected_version: int) -> bool:
        raise NotImplementedError("S1-T2 session revoke 未实现")

    async def acquire_refresh_lease(self, session_id: UUID, expected_version: int, lease: timedelta) -> bool:
        raise NotImplementedError("S1-T2 refresh lease 未实现")

    async def complete_refresh(
        self,
        session_id: UUID,
        expected_version: int,
        *,
        encrypted_token_ref: str,
        idle_expires_at: Any,
    ) -> bool:
        raise NotImplementedError("S1-T2 refresh 完成未实现")

    async def release_refresh_lease(self, session_id: UUID, expected_version: int) -> bool:
        raise NotImplementedError("S1-T2 refresh lease 释放未实现")


class SessionService:
    """会话编排：登录完成、cookie 认证、CSRF、refresh/revoke、membership 解析。"""

    def __init__(
        self,
        *,
        session_store: AuthSessionStore,
        secret_backend: Any,
        oidc_service: Any,
        identity_session_factory: Any,
    ) -> None:
        self._session_store = session_store
        self._secret_backend = secret_backend
        self._oidc_service = oidc_service
        self._identity_session_factory = identity_session_factory

    async def authenticate_cookie(self, cookie_token: str) -> AuthSession | None:
        """按 cookie 查找 session 并校验 revoked / idle / absolute / principal 状态。"""
        raise NotImplementedError("S1-T2 session 认证未实现")

    async def csrf_token(self, session: AuthSession, cookie_token: str) -> str:
        """从 session 与 cookie 派生的 CSRF token（仅 session 绑定，不落库明文）。"""
        raise NotImplementedError("S1-T2 csrf 派生未实现")

    async def refresh_session(self, session_id: UUID, expected_version: int) -> AuthSession:
        """lease 竞争 + IdP refresh + envelope 轮换 + CAS 版本递增。"""
        raise NotImplementedError("S1-T2 session refresh 未实现")

    async def revoke_session(self, session_id: UUID, expected_version: int) -> bool:
        raise NotImplementedError("S1-T2 session revoke 未实现")

    async def memberships(self, principal_id: UUID) -> list[dict[str, Any]]:
        """通过窄 SECURITY DEFINER resolver 获得 principal 的组织/工作区摘要。"""
        raise NotImplementedError("S1-T2 membership 解析未实现")
