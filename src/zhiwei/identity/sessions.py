"""AuthSession 存储与会话服务（S1-T2）。

契约（冻结，含验收修订 3/4）：
- AuthSession 为 principal/session 级，不含组织字段；refresh 用 expected_version CAS
  （防 ABA 重放）；revoke 单调安全：session id 永不重用，撤销按 id 生效，同一 session
  的 refresh 不得使撤销失效，版本门禁不得阻断旧 logout 重放；
- cookie 只按 SHA-256 hash 查找，每次请求不解密 provider token；
- refresh（验收阻断 4 修订）：数据库 CAS + 有界 lease + opaque owner token fencing。
  refresh_state 三态 idle / leased / calling：
  * acquire 生成每次 ownership 唯一的 owner token（DB 只存 SHA-256），进入 leased；
    过期 leased 可由下一位调用方接管（owner token 换新）；
  * 调用 IdP 前必须以 owner token CAS 进入 calling（durable calling barrier）——
    CAS 失败绝不调用 IdP；expired calling 只能 fail closed 本地 revoke，绝不二次调用；
  * complete/release 必须带 owner token：旧 owner 不得清除或提交后来 owner 的状态；
  * 失败分类：invalid_grant / 缺 refresh token / token+envelope integrity 错误 /
    绝对过期 / IdP 成功后本地提交失败 → 本地 revoke（fail closed）；单纯 loser/stale
    owner 不得撤销 winner 已提交的新版本；
  * attempt/lease 时间全部使用数据库时钟（应用/DB 偏差不得改变并发判定）；输家等待
    上界 = lease 剩余；envelope 改写与 session 完成处于同一原子边界（事务时间取自
    同一数据库连接）；进程在两者之间失败不留下 AAD/session version 不一致；
- SessionService 业务层只依赖 SecretBackend port 与类型化 SessionRefreshCommit
  adapter（LocalSessionRefreshUnitOfWork），不出现 SQLAlchemy session /
  external_session 参数——S4 Vault/KMS adapter 可实现同 port；
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from zhiwei.secrets.base import (
    SecretBackend,
    SecretIntegrityError,
    SecretRef,
    SecretRevokedError,
)
from zhiwei.secrets.local import LocalSecretBackend

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


def _active_organization_ids(memberships: list[dict[str, Any]]) -> tuple[UUID, ...]:
    """从全部权威 memberships 行构造 active 组织 id 集合：去重后稳定排序。

    同一 principal 可同时持 org 作用域与 workspace 作用域的绑定（同一 org 两行
    status=active），二者都计、去重；返回确定性排序的 tuple，保证 PolicyInput
    序列化稳定——重放候选判定（target ∈ 集合）与任何「第一个 org」的查询顺序无关。
    """
    return tuple(
        sorted(
            {
                row["organization_id"]
                for row in memberships
                if row["organization_status"] == "active"
            }
        )
    )


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

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
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
                    refresh_owner_token_hash=session.refresh_owner_token_hash,
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
        刷新后到达的旧 logout 失效（验收阻断 1）。撤销仍递增版本并清空 lease 与
        owner token，打断在途 refresh 的 CAS。
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE auth_sessions SET revoked_at = now(), "
                    "refresh_state = 'idle', refresh_lease_expires_at = NULL, "
                    "refresh_owner_token_hash = NULL, "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = :session_id AND revoked_at IS NULL RETURNING id"
                ),
                {"session_id": session_id},
            )
            return result.scalar_one_or_none() is not None

    async def db_now(self, session: AsyncSession | None = None) -> datetime:
        """数据库时钟（唯一时间源）：attempt/lease 判定不得混用应用时钟。"""
        if session is None:
            async with self._session_factory() as conn:
                return (await conn.execute(text("SELECT now()"))).scalar_one()
        return (await session.execute(text("SELECT now()"))).scalar_one()

    async def acquire_refresh_lease(
        self, session_id: UUID, expected_version: int, lease: timedelta
    ) -> tuple[str | None, datetime]:
        """数据库侧 attempt/lease ownership：返回 (owner_token 或 None, DB 时钟的 attempt 时间)。

        - 每次 ownership 生成新的 opaque owner token，DB 只存 SHA-256；
        - attempt 时间与 updated_at 同源（数据库时钟），应用/DB 时钟偏差不影响判定；
        - 仅 idle 或已过期的 leased 可取得 ownership（过期 calling 不得接管——
          刷新是否已发生不可知，必须 fail closed revoke，见 _wait_for_winner）。
        """
        owner_token = secrets.token_urlsafe(32)
        owner_hash = _sha256_hex(owner_token)
        async with self._session_factory() as session, session.begin():
            attempted_at = (await session.execute(text("SELECT now()"))).scalar_one()
            result = await session.execute(
                text(
                    "UPDATE auth_sessions SET refresh_state = 'leased', "
                    "refresh_owner_token_hash = :owner_hash, "
                    "refresh_lease_expires_at = now() + :lease, updated_at = now() "
                    "WHERE id = :session_id AND version = :expected "
                    "AND revoked_at IS NULL "
                    "AND (refresh_state = 'idle' "
                    "     OR (refresh_state = 'leased' "
                    "         AND refresh_lease_expires_at <= now())) "
                    "RETURNING id"
                ),
                {
                    "session_id": session_id,
                    "expected": expected_version,
                    "lease": lease,
                    "owner_hash": owner_hash,
                },
            )
            acquired = result.scalar_one_or_none() is not None
        return (owner_token if acquired else None), attempted_at

    async def enter_calling_phase(
        self, session_id: UUID, expected_version: int, owner_token: str
    ) -> bool:
        """durable calling barrier：调用 IdP 前以 owner token CAS 进入 calling。

        CAS 失败（被接管 / 被撤销）→ False，调用方不得调用 IdP。
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE auth_sessions SET refresh_state = 'calling', updated_at = now() "
                    "WHERE id = :session_id AND version = :expected AND revoked_at IS NULL "
                    "AND refresh_state = 'leased' "
                    "AND refresh_owner_token_hash = :owner_hash RETURNING id"
                ),
                {
                    "session_id": session_id,
                    "expected": expected_version,
                    "owner_hash": _sha256_hex(owner_token),
                },
            )
            return result.scalar_one_or_none() is not None

    async def complete_refresh(
        self,
        session_id: UUID,
        expected_version: int,
        owner_token: str,
        *,
        encrypted_token_ref: str,
        idle_expires_at: datetime,
        session: AsyncSession | None = None,
    ) -> bool:
        """session 完成 CAS（calling→idle，owner token 门禁）；可复用外部事务连接。"""
        if session is None:
            async with self._session_factory() as conn, conn.begin():
                return await _complete_refresh_in(
                    conn, session_id, expected_version, owner_token,
                    encrypted_token_ref=encrypted_token_ref,
                    idle_expires_at=idle_expires_at,
                )
        return await _complete_refresh_in(
            session, session_id, expected_version, owner_token,
            encrypted_token_ref=encrypted_token_ref,
            idle_expires_at=idle_expires_at,
        )

    async def release_refresh_lease(
        self, session_id: UUID, expected_version: int, owner_token: str
    ) -> bool:
        """租约放弃（leased→idle）：只允许自己的 owner token；calling 不得释放。"""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE auth_sessions SET refresh_state = 'idle', "
                    "refresh_lease_expires_at = NULL, "
                    "refresh_owner_token_hash = NULL, updated_at = now() "
                    "WHERE id = :session_id AND version = :expected "
                    "AND revoked_at IS NULL "
                    "AND refresh_state = 'leased' "
                    "AND refresh_owner_token_hash = :owner_hash RETURNING id"
                ),
                {
                    "session_id": session_id,
                    "expected": expected_version,
                    "owner_hash": _sha256_hex(owner_token),
                },
            )
            return result.scalar_one_or_none() is not None

    async def revoke_expired_calling(
        self,
        session_id: UUID,
        expected_version: int,
        observed_owner_token_hash: str | None,
    ) -> bool:
        """refresh 专用条件撤销（验收修订 5）：expired calling 的 fail-closed revoke。

        同时 CAS session_id + expected_version + refresh_state='calling' + 观察到的
        owner token hash + lease 已过期；任何一项不匹配（winner 已提交 / 已被其他
        路径撤销 / 状态迁移）→ False，调用方必须重读分类。

        与 logout 的 revoke_session 语义不同：后者按 session id 单调生效（旧版本
        logout 重放仍撤销）；本方法绝不撤销 winner 在观察与撤销之间提交的新版本。
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE auth_sessions SET revoked_at = now(), "
                    "refresh_state = 'idle', refresh_lease_expires_at = NULL, "
                    "refresh_owner_token_hash = NULL, "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = :session_id AND version = :expected "
                    "AND revoked_at IS NULL AND refresh_state = 'calling' "
                    "AND refresh_owner_token_hash = :owner_hash "
                    "AND refresh_lease_expires_at <= now() RETURNING id"
                ),
                {
                    "session_id": session_id,
                    "expected": expected_version,
                    "owner_hash": observed_owner_token_hash,
                },
            )
            return result.scalar_one_or_none() is not None


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
        """同一数据库事务内改写 envelope 并 CAS 完成 session。

        返回事务内 DB 时钟（idle_expires_at 的 now() 与 envelope/complete 同连接同源）；
        任何失败（含 session CAS 失败）抛错并整体回滚——旧 envelope 保持可用。
        """
        raise NotImplementedError


class LocalSessionRefreshUnitOfWork:
    """本地 PG 的 SessionRefreshCommit：envelope 改写 + session 完成同一事务。

    组合 LocalSecretBackend.put_in_session 与 session 完成 CAS，事务时间从同一
    连接取得（进程在两者之间失败不留下 AAD/session version 不一致）。完成 CAS
    统一经 session_store.complete_refresh（测试把 store 方法当关键路径 seam）。
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        secret_backend: LocalSecretBackend,
        session_store: AuthSessionStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_backend = secret_backend
        self._session_store = session_store

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
        new_version = expected_version + 1
        aad = _session_aad(session_id, issuer, subject, new_version)
        async with self._session_factory() as conn, conn.begin():
            await self._secret_backend.put_in_session(
                conn,
                SecretRef(str(session_id)),
                canonical_json(payload.model_dump(mode="json")),
                aad,
                purpose=_ENVELOPE_PURPOSE,
            )
            now = (await conn.execute(text("SELECT now()"))).scalar_one()
            idle_expires_at = min(now + SESSION_IDLE_TTL, expires_at)
            if self._session_store is not None:
                completed = await self._session_store.complete_refresh(
                    session_id,
                    expected_version,
                    owner_token,
                    encrypted_token_ref=str(session_id),
                    idle_expires_at=idle_expires_at,
                    session=conn,
                )
            else:
                completed = await _complete_refresh_in(
                    conn,
                    session_id,
                    expected_version,
                    owner_token,
                    encrypted_token_ref=str(session_id),
                    idle_expires_at=idle_expires_at,
                )
            if not completed:
                # 并发 revoke/takeover：CAS 失败 → 整体回滚，旧 envelope 保持可用
                raise SessionRevokedError(
                    "session was revoked or taken over during refresh commit"
                )
        return now


class SessionService:
    """会话编排：登录完成、cookie 认证、CSRF、refresh/revoke、membership 解析。"""

    def __init__(
        self,
        *,
        session_store: AuthSessionStore,
        secret_backend: SecretBackend,
        refresh_uow: SessionRefreshCommit,
        oidc_service: Any,
        identity_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_store = session_store
        self._secret_backend = secret_backend
        self._refresh_uow = refresh_uow
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
        """lease 竞争（owner token fencing）+ IdP refresh + envelope 轮换 + CAS 版本递增。

        失败分类（验收阻断 4）：
        - invalid_grant / 缺 refresh token / token+envelope integrity 错误 / 绝对过期 /
          IdP 成功后本地提交失败 → 本地 revoke，fail closed；
        - 单纯 loser / stale owner（CAS 被 fence）→ 不得撤销 winner 已提交的新版本，
          以 SessionConflictError 终止；
        - expired calling（owner 已进入 calling 且 lease 过期）→ 本地 revoke，
          绝不二次调用 IdP。
        attempt/lease 时间全部使用数据库时钟；输家等待上界 = lease 剩余。
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
        owner_token, _attempted_at = await self._session_store.acquire_refresh_lease(
            session_id, expected_version, REFRESH_LEASE
        )
        if owner_token is None:
            return await self._wait_for_winner(
                session_id, expected_version, initial_version=session.version
            )
        try:
            return await self._refresh_as_winner(session, expected_version, owner_token)
        except (SessionRevokedError, SessionConflictError):
            # 失败分类在 winner 内完成：revoke 类已本地 revoke；loser/stale 类
            # 不得触碰新 owner 的状态
            raise
        except Exception as exc:
            # 兜底：任何未分类错误 → 先释放自己的 lease（fenced，失败无副作用），
            # 再本地 revoke，fail closed
            await self._session_store.release_refresh_lease(
                session_id, expected_version, owner_token
            )
            await self._session_store.revoke_session(session_id, expected_version)
            raise SessionRevokedError("refresh failed; session revoked locally") from exc

    async def _refresh_as_winner(
        self, session: AuthSession, expected_version: int, owner_token: str
    ) -> AuthSession:
        # durable calling barrier：调用 IdP 前必须以 owner token CAS 进入 calling；
        # CAS 失败（被接管/撤销）→ 不调用 IdP，作为 loser 终止
        entered = await self._session_store.enter_calling_phase(
            session.id, expected_version, owner_token
        )
        if not entered:
            raise SessionConflictError("refresh ownership lost before calling IdP")

        # 读取旧 envelope：integrity / revoked / 缺 refresh token → 本地 revoke
        try:
            aad = _session_aad(session.id, session.issuer, session.subject, expected_version)
            old_plaintext = await self._secret_backend.get(
                SecretRef(str(session.id)), aad
            )
            old_payload = TokenEnvelopePayload(**json.loads(old_plaintext))
        except (SecretRevokedError, SecretIntegrityError, ValueError) as exc:
            await self._session_store.revoke_session(session.id, expected_version)
            raise SessionRevokedError(
                "refresh token envelope is unusable; session revoked"
            ) from exc
        if old_payload.refresh_token is None:
            await self._session_store.revoke_session(session.id, expected_version)
            raise SessionRevokedError(
                "session has no refresh token; session revoked"
            )

        # IdP 调用：任何失败（invalid_grant / provider revoke / 网络等不可恢复错误）
        # → 本地 revoke，fail closed；绝不重试旧 refresh token（总调用数至多一次）
        try:
            tokens = await self._oidc_service.refresh_tokens(old_payload.refresh_token)
        except Exception as exc:
            await self._session_store.revoke_session(session.id, expected_version)
            raise SessionRevokedError("IdP refresh failed; session revoked locally") from exc

        new_payload = TokenEnvelopePayload(
            purpose=_ENVELOPE_PURPOSE,
            token_kind="access_refresh",
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at=_token_expiry(tokens, session.expires_at),
            schema_version=1,
        )
        try:
            await self._refresh_uow.commit(
                session_id=session.id,
                issuer=session.issuer,
                subject=session.subject,
                expected_version=expected_version,
                owner_token=owner_token,
                payload=new_payload,
                expires_at=session.expires_at,
            )
        except SessionRevokedError:
            # 并发 revoke/takeover：事务已整体回滚，无需重复撤销
            raise
        except Exception as exc:
            # IdP 已成功但本地提交失败：外部 token 状态已不确定 → 本地 revoke
            await self._session_store.revoke_session(session.id, expected_version)
            raise SessionRevokedError(
                "refresh commit failed; session revoked locally"
            ) from exc
        refreshed = await self._session_store.get_session(session.id)
        if refreshed is None:
            raise SessionRevokedError("session disappeared during refresh")
        return refreshed

    async def _wait_for_winner(
        self, session_id: UUID, expected_version: int, *, initial_version: int
    ) -> AuthSession:
        """lease 竞争失败后的确定性判定：读取 winner 新版本或 fail closed。

        acquire 失败后立刻重读行，按状态区分四种情形：
        - version != expected_version：expected_version 已过期。若行版本恰好是
          expected+1 且本调用**发起时**读到的就是 expected（initial_version），
          则观察到的推进只能是并发赢家在本次调用窗口内完成——无论其提交早于或
          晚于本方 acquire——返回 winner 的新版本（绝不复用旧 refresh token）；
          发起时读到更高版本的是纯 stale 调用 → SessionConflictError（与顺序
          stale 契约 test_refresh_with_stale_expected_version_fails_closed 一致，
          也避免依赖跨语句时钟比较——应用/DB 时钟偏差不得影响判定）；
        - version == expected_version 且 leased/calling（未过期）：并发 refresh 在途
          → 轮询 winner，等待上界 = lease 剩余（数据库时钟），不是固定轮询预算；
        - version == expected_version 且 calling 已过期：owner 已进入 calling 但状态
          不确定（可能已调用 IdP）→ fail closed 本地 revoke，绝不二次调用 IdP；
        - revoked → SessionRevokedError。
        """
        current = await self._session_store.get_session(session_id)
        if current is None or current.revoked_at is not None:
            raise SessionRevokedError("session revoked by another replica")
        if current.version != expected_version:
            if (
                current.version == expected_version + 1
                and initial_version == expected_version
            ):
                return current
            raise SessionConflictError("stale expected_version for refresh")
        while current.refresh_state in ("leased", "calling"):
            lease_expires = current.refresh_lease_expires_at
            if lease_expires is None or lease_expires <= await self._session_store.db_now():
                break
            await asyncio.sleep(REFRESH_WAIT_INTERVAL)
            current = await self._session_store.get_session(session_id)
            if current is None or current.revoked_at is not None:
                raise SessionRevokedError("session revoked by another replica")
            if current.version > expected_version:
                return current
        # lease 已过期（owner 崩溃遗留）：
        # - calling：刷新是否已发生不可知 → refresh 专用**条件撤销**（CAS calling +
        #   observed owner hash + lease 过期），绝不调用 logout 的按 session id 单调
        #   revoke——winner 在观察与撤销之间提交 v2 时，单调 revoke 会撤销 v2（验收
        #   修订 5 竞态）；条件撤销失败后必须重读分类：
        #   * winner 已提交 expected+1 → 返回 winner 的新版本；
        #   * 已被其他路径撤销 → SessionRevokedError；
        #   * 其他状态 → SessionConflictError；
        # - leased：未进入 calling，下一位调用方通过 acquire 接管，本轮不撤销。
        if current.refresh_state == "calling":
            revoked = await self._session_store.revoke_expired_calling(
                session_id,
                expected_version,
                current.refresh_owner_token_hash,
            )
            if revoked:
                raise SessionRevokedError(
                    "refresh owner stalled in calling phase; session revoked"
                )
            winner = await self._session_store.get_session(session_id)
            if winner is None or winner.revoked_at is not None:
                raise SessionRevokedError("session revoked by another replica")
            if winner.version == expected_version + 1:
                return winner
            raise SessionConflictError(
                "refresh ownership changed without a winner result"
            )
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

        role_bindings 只含已解析 org 的 org 级绑定 + 已解析 workspace（若存在）的
        workspace 级绑定（repair addendum §3.2 provenance 裁决）：绝不携带其他 org
        的绑定，防止跨 org 遗留角色字符串在构造 PolicyInput 时按 fail closed 拒绝。

        active_organization_ids 在两个分支都来自同一权威解析（全部 memberships 的
        active 组织 id 集合，与声明的 org 无关）：principal-only context（无 org
        声明）因此可携带完整集合供 bootstrap / 重放候选规则判定（不携带其他绑定
        数据，provenance 裁决不变）。
        """
        from zhiwei.identity.domain import ActorContext, ActorRoleBinding

        if organization_id is None:
            if workspace_id is not None:
                raise MembershipScopeError("workspace context requires an organization")
            memberships = await self.memberships(principal_id)
            return ActorContext(
                principal_id=principal_id,
                active_organization_ids=_active_organization_ids(memberships),
            )
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
        active_organization_ids = _active_organization_ids(memberships)
        role_bindings = tuple(
            ActorRoleBinding(
                name=role,
                scope="org",
                organization_id=org,
            )
            for row in org_memberships
            for role in (row["role_bindings"] or [])
        )
        if workspace_id is None:
            return ActorContext(
                principal_id=principal_id,
                organization_id=org,
                role_bindings=role_bindings,
                active_organization_ids=active_organization_ids,
            )
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
        role_bindings = role_bindings + tuple(
            ActorRoleBinding(
                name=role,
                scope="workspace",
                organization_id=org,
                workspace_id=workspace,
            )
            for row in workspace_memberships
            for role in (row["role_bindings"] or [])
        )
        return ActorContext(
            principal_id=principal_id,
            organization_id=org,
            workspace_id=workspace,
            role_bindings=role_bindings,
            active_organization_ids=active_organization_ids,
        )


async def _complete_refresh_in(
    session: AsyncSession,
    session_id: UUID,
    expected_version: int,
    owner_token: str,
    *,
    encrypted_token_ref: str,
    idle_expires_at: datetime,
) -> bool:
    """calling→idle 完成 CAS（owner token 门禁）：旧 owner 不得提交新 owner 的状态。"""
    result = await session.execute(
        text(
            "UPDATE auth_sessions SET refresh_state = 'idle', "
            "refresh_lease_expires_at = NULL, refresh_owner_token_hash = NULL, "
            "encrypted_token_ref = :ref, idle_expires_at = :idle, "
            "version = version + 1, updated_at = now() "
            "WHERE id = :session_id AND version = :expected AND revoked_at IS NULL "
            "AND refresh_state = 'calling' "
            "AND refresh_owner_token_hash = :owner_hash RETURNING id"
        ),
        {
            "session_id": session_id,
            "expected": expected_version,
            "ref": encrypted_token_ref,
            "idle": idle_expires_at,
            "owner_hash": _sha256_hex(owner_token),
        },
    )
    return result.scalar_one_or_none() is not None


def _to_auth_session(row: AuthSessionRow) -> AuthSession:
    """持久化适配层投影；非法持久化状态 fail closed（验收修订 5）。

    旧实现把 idle 行残留的 owner hash 静默忽略、投影成看似合法的 domain 对象；
    DB CHECK 是最后防线，投影层也必须拒绝——任何违反统一不变量（idle ⟺
    owner/lease 皆 NULL、leased/calling ⟹ 皆非 NULL、revoked 全空）的行一律抛
    SessionRevokedError，不得掩盖。
    """
    try:
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
            refresh_owner_token_hash=row.refresh_owner_token_hash,
            created_at=row.created_at,
            updated_at=row.updated_at,
            schema_version=row.schema_version,
        )
    except ValueError as exc:
        raise SessionRevokedError("illegal persisted session state") from exc


def _token_expiry(tokens: dict[str, Any], fallback: datetime) -> datetime:
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return utc_now() + timedelta(seconds=int(expires_in))
    return fallback
