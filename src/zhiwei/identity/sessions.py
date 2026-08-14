"""AuthSession 存储与会话服务（S1-T2）。

契约（冻结，含验收修订）：
- AuthSession 为 principal/session 级，不含组织字段；refresh 用 expected_version CAS
  （防 ABA 重放）；revoke 单调安全：session id 永不重用，撤销按 id 生效，同一 session
  的 refresh 不得使撤销失效，版本门禁不得阻断旧 logout 重放；
- cookie 只按 SHA-256 hash 查找，每次请求不解密 provider token；
- refresh：数据库 CAS + 有界 lease 竞争 ownership，多 replica 恰好一次 IdP refresh，
  输家轮询读取 winner 的新版本，绝不复用旧 refresh token；attempt/lease 时间全部使用
  数据库时钟（应用/DB 偏差不得改变并发判定）；输家等待上界 = lease 剩余；废弃 lease
  （过期仍停在 refreshing）由下一位调用方接管完成，不得永久卡死；envelope 改写与
  session 完成处于同一原子边界（进程在两者之间失败不留下 AAD/session version 不一致）；
  invalid_grant / provider revoke / 绝对过期 / 任何不可恢复错误 → 本地 revoke，
  fail closed；
- logout 先可靠本地 revoke 并清 cookie；IdP revoke 不可用不能恢复本地 session；
- disabled / 非 User principal 禁止交互登录，既有 session 每次请求即时失效；
- 组织/工作区 context 必须来自已验证 membership（窄 SECURITY DEFINER resolver）；
  客户端声明只是请求，不是授权事实。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select, text

from zhiwei.contracts.canonical import canonical_json
from zhiwei.contracts.time import utc_now
from zhiwei.identity.domain import (
    AuthSession,
    LoginAttempt,
    TokenAAD,
    TokenEnvelopePayload,
)
from zhiwei.identity.oidc import OIDCValidationError
from zhiwei.identity.repositories import IdentityStore
from zhiwei.persistence.models import AuthSession as AuthSessionRow
from zhiwei.persistence.models import OidcLoginAttempt as LoginAttemptRow
from zhiwei.secrets.base import SecretRef

SESSION_ABSOLUTE_TTL = timedelta(hours=8)
SESSION_IDLE_TTL = timedelta(minutes=30)
REFRESH_LEASE = timedelta(seconds=30)
REFRESH_WAIT_INTERVAL = 0.05

_ENVELOPE_PURPOSE = "oidc_session"


class SessionConflictError(RuntimeError):
    """expected_version 过期 / refresh lease 竞争失败：fail closed。"""


class SessionRevokedError(RuntimeError):
    """session 已 revoke（本地或 IdP）/ 绝对过期 / 不可恢复错误：拒绝继续。"""


class UnknownPrincipalError(RuntimeError):
    """(issuer, subject) 未绑定任何 principal（T2 不实现 JIT）。"""


class PrincipalLoginDeniedError(RuntimeError):
    """disabled 或非 User principal 禁止交互登录。"""


class LoginAttemptExpiredError(RuntimeError):
    """state 不存在 / 已消费 / 已过期：callback 拒绝（fail closed）。"""


class MembershipScopeError(RuntimeError):
    """声明的 organization/workspace context 未通过 membership 验证。"""


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_aad(session_id: UUID, issuer: str, subject: str, session_version: int) -> bytes:
    return TokenAAD(
        purpose=_ENVELOPE_PURPOSE,
        session_id=session_id,
        issuer=issuer,
        subject=subject,
        session_version=session_version,
        schema_version=1,
    ).encode()


class AuthSessionStore:
    """auth_sessions / oidc_login_attempts 数据访问（identity 引擎，无 tenant context）。"""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def create_login_attempt(self, attempt: LoginAttempt) -> None:
        async with self._session_factory() as session, session.begin():
            session.add(
                LoginAttemptRow(
                    id=attempt.id,
                    state_hash=attempt.state_hash,
                    nonce_hash=attempt.nonce_hash,
                    code_verifier=attempt.code_verifier,
                    issuer=attempt.issuer,
                    redirect_uri=attempt.redirect_uri,
                    created_at=attempt.created_at,
                    expires_at=attempt.expires_at,
                    schema_version=attempt.schema_version,
                )
            )

    async def consume_login_attempt(self, state: str) -> LoginAttempt | None:
        """按 state 原子一次性消费；不存在 / 已消费 / 已过期返回 None。"""
        state_hash = _sha256_hex(state)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE oidc_login_attempts SET consumed_at = now() "
                    "WHERE state_hash = :state_hash AND consumed_at IS NULL "
                    "AND expires_at > now() "
                    "RETURNING id, state_hash, nonce_hash, code_verifier, issuer, "
                    "redirect_uri, created_at, expires_at, consumed_at, schema_version"
                ),
                {"state_hash": state_hash},
            )
            row = result.mappings().first()
        if row is None:
            return None
        return LoginAttempt(
            id=row["id"],
            state_hash=row["state_hash"],
            nonce_hash=row["nonce_hash"],
            code_verifier=row["code_verifier"],
            issuer=row["issuer"],
            redirect_uri=row["redirect_uri"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
            schema_version=row["schema_version"],
        )

    async def create_session(self, session: AuthSession) -> None:
        async with self._session_factory() as session_conn, session_conn.begin():
            session_conn.add(
                AuthSessionRow(
                    id=session.id,
                    cookie_token_hash=session.cookie_token_hash,
                    principal_id=session.principal_id,
                    issuer=session.issuer,
                    subject=session.subject,
                    encrypted_token_ref=session.encrypted_token_ref,
                    csrf_hash=session.csrf_hash,
                    expires_at=session.expires_at,
                    idle_expires_at=session.idle_expires_at,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                    revoked_at=session.revoked_at,
                    version=session.version,
                    refresh_state=session.refresh_state,
                    refresh_lease_expires_at=session.refresh_lease_expires_at,
                    schema_version=session.schema_version,
                )
            )

    async def get_session_by_token(self, cookie_token: str) -> AuthSession | None:
        token_hash = _sha256_hex(cookie_token)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AuthSessionRow).where(
                        AuthSessionRow.cookie_token_hash == token_hash
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else _to_auth_session(row)

    async def get_session(self, session_id: UUID) -> AuthSession | None:
        async with self._session_factory() as session:
            row = await session.get(AuthSessionRow, session_id)
        return None if row is None else _to_auth_session(row)

    async def revoke_session(self, session_id: UUID, expected_version: int) -> bool:
        """单调撤销：按 session id 生效，不依赖调用方版本快照。

        session id 永不重用（uuid4），按 id 撤销无 ABA 风险；版本门禁反而会让
        刷新后到达的旧 logout 失效（验收阻断 1）。撤销仍递增版本并清空 lease，
        打断在途 refresh 的 CAS。
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE auth_sessions SET revoked_at = now(), "
                    "refresh_state = 'idle', refresh_lease_expires_at = NULL, "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = :session_id AND revoked_at IS NULL RETURNING id"
                ),
                {"session_id": session_id},
            )
            return result.scalar_one_or_none() is not None

    async def db_now(self) -> datetime:
        """数据库时钟（唯一时间源）：attempt/lease 判定不得混用应用时钟。"""
        async with self._session_factory() as session:
            return (await session.execute(text("SELECT now()"))).scalar_one()

    async def acquire_refresh_lease(
        self, session_id: UUID, expected_version: int, lease: timedelta
    ) -> tuple[bool, datetime]:
        """数据库侧 attempt/lease ownership：返回 (是否取得, DB 时钟的 attempt 时间)。

        - attempt 时间与 updated_at 同源（数据库时钟），应用/DB 时钟偏差不影响判定；
        - lease 已过期（winner 崩溃遗留）时允许接管 ownership，行不得永久卡死。
        """
        async with self._session_factory() as session, session.begin():
            attempted_at = (await session.execute(text("SELECT now()"))).scalar_one()
            result = await session.execute(
                text(
                    "UPDATE auth_sessions SET refresh_state = 'refreshing', "
                    "refresh_lease_expires_at = now() + :lease, updated_at = now() "
                    "WHERE id = :session_id AND version = :expected "
                    "AND revoked_at IS NULL "
                    "AND (refresh_state = 'idle' OR refresh_lease_expires_at <= now()) "
                    "RETURNING id"
                ),
                {
                    "session_id": session_id,
                    "expected": expected_version,
                    "lease": lease,
                },
            )
            return result.scalar_one_or_none() is not None, attempted_at

    async def complete_refresh(
        self,
        session_id: UUID,
        expected_version: int,
        *,
        encrypted_token_ref: str,
        idle_expires_at: datetime,
        session: Any | None = None,
    ) -> bool:
        """session 完成 CAS；可复用外部事务连接（与 envelope 写入同原子边界）。"""
        if session is None:
            async with self._session_factory() as conn, conn.begin():
                return await self._complete_refresh_in(
                    conn, session_id, expected_version,
                    encrypted_token_ref=encrypted_token_ref,
                    idle_expires_at=idle_expires_at,
                )
        return await self._complete_refresh_in(
            session, session_id, expected_version,
            encrypted_token_ref=encrypted_token_ref,
            idle_expires_at=idle_expires_at,
        )

    @staticmethod
    async def _complete_refresh_in(
        session: Any,
        session_id: UUID,
        expected_version: int,
        *,
        encrypted_token_ref: str,
        idle_expires_at: datetime,
    ) -> bool:
        result = await session.execute(
            text(
                "UPDATE auth_sessions SET refresh_state = 'idle', "
                "refresh_lease_expires_at = NULL, encrypted_token_ref = :ref, "
                "idle_expires_at = :idle, version = version + 1, updated_at = now() "
                "WHERE id = :session_id AND version = :expected "
                "AND revoked_at IS NULL RETURNING id"
            ),
            {
                "session_id": session_id,
                "expected": expected_version,
                "ref": encrypted_token_ref,
                "idle": idle_expires_at,
            },
        )
        return result.scalar_one_or_none() is not None

    async def release_refresh_lease(self, session_id: UUID, expected_version: int) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE auth_sessions SET refresh_state = 'idle', "
                    "refresh_lease_expires_at = NULL, updated_at = now() "
                    "WHERE id = :session_id AND version = :expected "
                    "AND refresh_state = 'refreshing' RETURNING id"
                ),
                {"session_id": session_id, "expected": expected_version},
            )
            return result.scalar_one_or_none() is not None

    async def enter_calling_phase(
        self, session_id: UUID, expected_version: int, owner_token: str
    ) -> bool:
        """RED 骨架：durable calling barrier（owner token CAS leased→calling）。

        验收阻断 4 冻结的契约：调用 IdP 前必须进入 calling；旧 owner 的陈旧 token
        不得通过。GREEN 以 owner token 的 SHA-256 比较实现。
        """
        raise NotImplementedError("owner-token fencing 契约待 GREEN 实现")


class SessionRefreshCommit(Protocol):
    """refresh 提交的原子边界：envelope 改写 + session 完成 + DB 时钟同事务。

    SessionService 业务层只依赖本协议与 SecretBackend port；不出现 SQLAlchemy
    session / external_session 参数（S4 Vault/KMS adapter 可实现同协议）。
    """

    async def commit(
        self,
        *,
        session_id: UUID,
        issuer: str,
        subject: str,
        expected_version: int,
        owner_token: str,
        payload: TokenEnvelopePayload,
        expires_at: datetime,
    ) -> datetime:
        """同一数据库事务内改写 envelope 并 CAS 完成 session；失败=整体回滚并抛错。"""
        raise NotImplementedError


class LocalSessionRefreshUnitOfWork:
    """RED 骨架：本地 PG 的 SessionRefreshCommit（GREEN 组合 put_in_session +
    complete CAS + 同连接 DB 时钟）。"""

    def __init__(
        self,
        *,
        session_factory: Any,
        secret_backend: Any,
    ) -> None:
        self._session_factory = session_factory
        self._secret_backend = secret_backend

    async def commit(
        self,
        *,
        session_id: UUID,
        issuer: str,
        subject: str,
        expected_version: int,
        owner_token: str,
        payload: TokenEnvelopePayload,
        expires_at: datetime,
    ) -> datetime:
        raise NotImplementedError("LocalSessionRefreshUnitOfWork 待 GREEN 实现")


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

    # ------------------------------------------------------------------ login

    async def create_login_attempt(self) -> str:
        url, attempt = await self._oidc_service.create_login_attempt()
        await self._session_store.create_login_attempt(attempt)
        return url

    async def complete_login(self, *, code: str, state: str) -> tuple[AuthSession, str]:
        """消费 attempt → exchange → 验证 → principal 校验 → envelope → 新 session。

        返回 (session, cookie_token)。callback 每次签发全新 cookie（防 fixation）。
        """
        attempt = await self._session_store.consume_login_attempt(state)
        if attempt is None:
            raise LoginAttemptExpiredError("login attempt is missing, consumed or expired")
        try:
            tokens, claims = await self._oidc_service.exchange_code(code=code, attempt=attempt)
        except OIDCValidationError:
            raise
        issuer = claims["iss"]
        subject = claims["sub"]
        principal_id = await self._resolve_login_principal(issuer=issuer, subject=subject)

        session_id = uuid4()
        now = utc_now()
        expires_at = now + SESSION_ABSOLUTE_TTL
        payload = TokenEnvelopePayload(
            purpose=_ENVELOPE_PURPOSE,
            token_kind="access_refresh",
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at=_token_expiry(tokens, expires_at),
            schema_version=1,
        )
        aad = _session_aad(session_id, issuer, subject, session_version=1)
        envelope = await self._secret_backend.put(
            SecretRef(str(session_id)),
            canonical_json(payload.model_dump(mode="json")),
            aad,
            purpose=_ENVELOPE_PURPOSE,
        )
        cookie_token = secrets.token_urlsafe(32)
        csrf = self.csrf_token_value(cookie_token, session_id)
        session = AuthSession(
            id=session_id,
            cookie_token_hash=_sha256_hex(cookie_token),
            principal_id=principal_id,
            issuer=issuer,
            subject=subject,
            encrypted_token_ref=envelope.ref,
            csrf_hash=_sha256_hex(csrf),
            expires_at=expires_at,
            idle_expires_at=now + SESSION_IDLE_TTL,
            version=1,
            refresh_state="idle",
            created_at=now,
            updated_at=now,
        )
        await self._session_store.create_session(session)
        return session, cookie_token

    async def _resolve_login_principal(self, *, issuer: str, subject: str) -> UUID:
        """ExternalIdentity 稳定键 (issuer, subject) → 仅 User + active 可交互登录。"""
        async with self._identity_session_factory() as session:
            store = IdentityStore(session)
            identity = await store.get_external_identity(issuer=issuer, subject=subject)
            if identity is None:
                raise UnknownPrincipalError(
                    "external identity is not provisioned (JIT 未实现)"
                )
            principal = await store.get_principal(identity.principal_id)
            if principal is None:
                raise UnknownPrincipalError("bound principal does not exist")
            if not principal.supports_interactive_login:
                raise PrincipalLoginDeniedError("only User principals can log in interactively")
            if principal.is_disabled:
                raise PrincipalLoginDeniedError("disabled principals cannot log in")
            return principal.id

    # ------------------------------------------------------------------ cookie 认证 / CSRF

    async def authenticate_cookie(self, cookie_token: str) -> AuthSession | None:
        """按 cookie 查找 session 并校验 revoked / idle / absolute / principal 状态。

        每次请求只查 auth_sessions + principals，不解密 provider token。
        """
        session = await self._session_store.get_session_by_token(cookie_token)
        if session is None:
            return None
        now = utc_now()
        if session.revoked_at is not None:
            return None
        if session.expires_at <= now or session.idle_expires_at <= now:
            return None
        async with self._identity_session_factory() as conn:
            store = IdentityStore(conn)
            principal = await store.get_principal(session.principal_id)
        if principal is None or principal.is_disabled:
            return None
        if not principal.supports_interactive_login:
            return None
        return session

    def csrf_token(self, session: AuthSession, cookie_token: str) -> str:
        return self.csrf_token_value(cookie_token, session.id)

    @staticmethod
    def csrf_token_value(cookie_token: str, session_id: UUID) -> str:
        """CSRF token = HMAC(cookie_token, session_id)。

        不新增服务端 secret、不落库明文：cookie 是 HttpOnly 且跨源不可读，
        攻击者无法推导 X-CSRF-Token；DB 侧以 csrf_hash 做二次锚定。
        """
        return hmac.new(
            cookie_token.encode("utf-8"), session_id.bytes, hashlib.sha256
        ).hexdigest()

    # ------------------------------------------------------------------ refresh / revoke

    async def refresh_session(self, session_id: UUID, expected_version: int) -> AuthSession:
        """lease 竞争 + IdP refresh + envelope 轮换 + CAS 版本递增。

        输家轮询读取 winner 的新版本（绝不复用旧 refresh token）；任何 refresh 失败
        （invalid_grant / provider revoke / 绝对过期 / 网络等不可恢复错误）→ 本地
        revoke，fail closed。attempt/lease 时间全部使用数据库时钟，应用时钟偏差
        不得改变并发判定；输家等待上界 = lease 剩余（不是固定轮询预算）。
        """
        session = await self._session_store.get_session(session_id)
        if session is None or session.revoked_at is not None:
            raise SessionRevokedError("session is revoked or missing")
        now = await self._session_store.db_now()
        if session.expires_at <= now:
            await self._session_store.revoke_session(session_id, expected_version)
            raise SessionRevokedError("session has absolutely expired")
        if session.idle_expires_at <= now:
            raise SessionRevokedError("session is idle-expired")

        # 数据库侧 attempt：acquire 事务内取 DB now() 作为 attempt 时间，
        # 与 updated_at 同源（验收阻断 2：应用/DB 时钟偏差不得误判并发 winner）
        acquired, attempted_at = await self._session_store.acquire_refresh_lease(
            session_id, expected_version, REFRESH_LEASE
        )
        if not acquired:
            return await self._wait_for_winner(
                session_id, expected_version, attempted_at=attempted_at
            )
        try:
            return await self._refresh_as_winner(session, expected_version)
        except (SessionRevokedError, SessionConflictError):
            await self._session_store.release_refresh_lease(session_id, expected_version)
            raise
        except Exception as exc:
            # 任何不可恢复错误 → 释放 lease 后本地 revoke，fail closed
            await self._session_store.release_refresh_lease(session_id, expected_version)
            await self._session_store.revoke_session(session_id, expected_version)
            raise SessionRevokedError("refresh failed; session revoked locally") from exc

    async def _refresh_as_winner(self, session: AuthSession, expected_version: int) -> AuthSession:
        aad = _session_aad(session.id, session.issuer, session.subject, expected_version)
        old_plaintext = await self._secret_backend.get(
            SecretRef(str(session.id)), aad
        )
        old_payload = TokenEnvelopePayload(**json.loads(old_plaintext))
        if old_payload.refresh_token is None:
            raise SessionRevokedError("session has no refresh token")
        tokens = await self._oidc_service.refresh_tokens(old_payload.refresh_token)

        new_version = expected_version + 1
        new_payload = TokenEnvelopePayload(
            purpose=_ENVELOPE_PURPOSE,
            token_kind="access_refresh",
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at=_token_expiry(tokens, session.expires_at),
            schema_version=1,
        )
        new_aad = _session_aad(session.id, session.issuer, session.subject, new_version)
        # envelope 改写与 session 完成处于同一原子边界（验收阻断 2）：
        # 同一数据库事务提交，进程在两者之间失败时整体回滚，不会留下
        # AAD/session version 不一致（旧 envelope 保持可用）。
        async with self._identity_session_factory() as conn, conn.begin():
            await self._secret_backend.put(
                SecretRef(str(session.id)),
                canonical_json(new_payload.model_dump(mode="json")),
                new_aad,
                purpose=_ENVELOPE_PURPOSE,
                external_session=conn,
            )
            now = await self._session_store.db_now()
            idle_expires_at = min(now + SESSION_IDLE_TTL, session.expires_at)
            completed = await self._session_store.complete_refresh(
                session.id,
                expected_version,
                encrypted_token_ref=str(session.id),
                idle_expires_at=idle_expires_at,
                session=conn,
            )
            if not completed:
                # revoke/refresh 竞争：winner 被并发 revoke，fail closed（回滚 envelope）
                raise SessionRevokedError("session was revoked during refresh")
        refreshed = await self._session_store.get_session(session.id)
        if refreshed is None:
            raise SessionRevokedError("session disappeared during refresh")
        return refreshed

    async def _wait_for_winner(
        self, session_id: UUID, expected_version: int, *, attempted_at: datetime
    ) -> AuthSession:
        """lease 竞争失败后的确定性判定：读取 winner 新版本或 fail closed。

        acquire 失败后立刻重读行，按状态区分三种情形：
        - version != expected_version：expected_version 已过期。若行版本恰好是
          expected+1 且 updated_at 晚于本调用 attempt 的数据库时刻（attempted_at），
          说明是并发赢家在我们 acquire 与读之间完成了刷新 → 返回 winner 的新版本
          （绝不复用旧 refresh token）；否则是纯 stale 调用 → SessionConflictError；
        - version == expected_version 且 refreshing：并发 refresh 在途 → 轮询 winner，
          等待上界 = lease 剩余（数据库时钟），不是固定 2 秒；
        - revoked → SessionRevokedError。
        """
        current = await self._session_store.get_session(session_id)
        if current is None or current.revoked_at is not None:
            raise SessionRevokedError("session revoked by another replica")
        if current.version != expected_version:
            if (
                current.version == expected_version + 1
                and current.updated_at >= attempted_at
            ):
                return current
            raise SessionConflictError("stale expected_version for refresh")
        while current.refresh_state == "refreshing":
            lease_expires = current.refresh_lease_expires_at
            if lease_expires is None or lease_expires <= await self._session_store.db_now():
                break
            await asyncio.sleep(REFRESH_WAIT_INTERVAL)
            current = await self._session_store.get_session(session_id)
            if current is None or current.revoked_at is not None:
                raise SessionRevokedError("session revoked by another replica")
            if current.version > expected_version:
                return current
        raise SessionConflictError("refresh lease contention without a winner result")

    async def revoke_session(self, session_id: UUID, expected_version: int) -> bool:
        return await self._session_store.revoke_session(session_id, expected_version)

    async def decrypt_tokens(self, session: AuthSession) -> TokenEnvelopePayload:
        """受控解密 provider token（logout 的 IdP revoke 与 refresh 使用）。"""
        aad = _session_aad(session.id, session.issuer, session.subject, session.version)
        plaintext = await self._secret_backend.get(SecretRef(session.encrypted_token_ref), aad)
        return TokenEnvelopePayload(**json.loads(plaintext))

    async def revoke_tokens_at_idp(self, payload: TokenEnvelopePayload) -> None:
        """best-effort IdP revocation；失败由调用方吞掉，不影响本地 session。"""
        await self._oidc_service.revoke_tokens(payload.access_token, payload.refresh_token)

    # ------------------------------------------------------------------ membership 解析

    async def memberships(self, principal_id: UUID) -> list[dict[str, Any]]:
        """通过窄 SECURITY DEFINER resolver 获得 principal 的组织/工作区摘要。"""
        async with self._identity_session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT scope, organization_id, workspace_id, role_bindings, "
                    "organization_status FROM public.zhiwei_principal_memberships(:pid) "
                    "ORDER BY scope, organization_id, workspace_id"
                ),
                {"pid": principal_id},
            )
            rows = result.mappings().all()
        return [dict(row) for row in rows]

    async def principal(self, principal_id: UUID) -> Any | None:
        """identity 引擎直接读取 principal（zhiwei_identity 角色有权访问）。"""
        async with self._identity_session_factory() as session:
            return await IdentityStore(session).get_principal(principal_id)

    async def resolve_context(
        self,
        principal_id: UUID,
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> Any:
        """验证声明的 org/workspace context；未声明时返回 principal-only context。

        任何 membership 缺失 / org-workspace 归属不一致 → MembershipScopeError
        （API 层按读/写映射 404 / 403）。客户端声明只是请求，不是授权事实。
        """
        from zhiwei.identity.domain import ActorContext

        if organization_id is None:
            if workspace_id is not None:
                raise MembershipScopeError("workspace context requires an organization")
            return ActorContext(principal_id=principal_id)
        try:
            org = UUID(organization_id)
        except (ValueError, AttributeError) as exc:
            raise MembershipScopeError("invalid organization id") from exc
        memberships = await self.memberships(principal_id)
        org_memberships = [
            row
            for row in memberships
            if row["scope"] == "organization" and row["organization_id"] == org
        ]
        if not org_memberships:
            raise MembershipScopeError("principal is not a member of the organization")
        if workspace_id is None:
            return ActorContext(principal_id=principal_id, organization_id=org)
        try:
            workspace = UUID(workspace_id)
        except (ValueError, AttributeError) as exc:
            raise MembershipScopeError("invalid workspace id") from exc
        workspace_memberships = [
            row
            for row in memberships
            if row["scope"] == "workspace"
            and row["organization_id"] == org
            and row["workspace_id"] == workspace
        ]
        if not workspace_memberships:
            raise MembershipScopeError(
                "principal has no workspace membership in the declared organization"
            )
        return ActorContext(
            principal_id=principal_id, organization_id=org, workspace_id=workspace
        )


def _to_auth_session(row: AuthSessionRow) -> AuthSession:
    return AuthSession(
        id=row.id,
        cookie_token_hash=row.cookie_token_hash,
        principal_id=row.principal_id,
        issuer=row.issuer,
        subject=row.subject,
        encrypted_token_ref=row.encrypted_token_ref,
        csrf_hash=row.csrf_hash,
        expires_at=row.expires_at,
        idle_expires_at=row.idle_expires_at,
        revoked_at=row.revoked_at,
        version=row.version,
        refresh_state=row.refresh_state,
        refresh_lease_expires_at=row.refresh_lease_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        schema_version=row.schema_version,
    )


def _token_expiry(tokens: dict[str, Any], fallback: datetime) -> datetime:
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return utc_now() + timedelta(seconds=int(expires_in))
    return fallback
