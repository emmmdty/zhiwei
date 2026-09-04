"""S1-T2 RED：OIDC BFF 登录/回调/登出/me、CSRF、session 生命周期与多 replica refresh。

设计/验收方冻结（A 档）：
- Authorization Code + PKCE S256；state/nonce/verifier 只在短期 server-side attempt；
  attempt 原子一次性消费，callback replay 必须失败；
- 验证签名/issuer/audience/azp/exp/iat/nonce；未知 ExternalIdentity、disabled、
  ServiceAccount、AgentIdentity 全部拒绝；不实现 JIT；
- cookie 只含高熵 opaque token；DB 只存 SHA-256 hash；__Host- 前缀 + Secure + HttpOnly +
  SameSite=Lax + Path=/ + 无 Domain；callback 总是签发全新 session token（防 fixation）；
- 所有 cookie-authenticated mutation 校验 server-side CSRF + 可信 Origin/Host，缺失或不匹配
  一律 403 且零 mutation；revoked / idle / absolute expired / disabled → 401 并清 cookie；
- /me 与 GET /api/v1/organizations 的组织数据必须来自已验证 membership；
- 多 replica refresh（含验收阻断 4 的修订冻结）：
  * 数据库 CAS + 有界 lease + opaque owner token fencing：acquire 生成每次 ownership
    唯一的 owner token（DB 只存 SHA-256）；leased→calling（调用 IdP 前）与
    calling→idle（完成）以及 release 全部以 owner token CAS 门禁；
  * live owner 跨 lease：winner 在调用 IdP 前暂停到 lease 过期，replica B 接管后，
    整个 session generation 最多一次 IdP refresh；旧 owner 不得 release/complete 新
    owner 的 lease；最终只能是成功提交一个新版本，或会话被单调撤销；禁止
    「IdP 调用两次但 session 仍 active」；
  * durable calling barrier：owner 调用 IdP 前必须先以 owner token CAS 进入 calling；
    旧 owner 未进入 calling 时 lease 过期允许接管；已进入 calling 时下一请求不得再次
    调用 IdP，本地 revoke（fail closed）；成功并发路径恰好一次调用，故障不确定路径
    总调用数至多一次；
  * stale owner fencing：acquire/start-call/complete/release 全部校验 owner token；
  * envelope 中 refresh_token=None → SessionRevokedError 且本地 revoke；
  * IdP refresh 已成功但 envelope/session 本地事务失败 → 事务整体回滚 + 会话本地
    revoke，禁止重试旧 refresh token（原「保持 active 且重试成功」期望已删除）；
  * invalid_grant → 本地 revoke；winner 判定只用数据库时钟（应用/DB 时钟偏差不得把
    并发 winner 误判为 stale）；输家等待上界 = lease 剩余；envelope 改写与 session
    完成处于同一原子边界，事务时间取自同一数据库连接；
  * SessionService 业务层只依赖 SecretBackend port 与类型化 UoW adapter，不得调用
    LocalSecretBackend 的 concrete-only 参数（external_session）；
- logout 单调安全：同一 session 的 refresh 不得使撤销失效；API 只在服务端撤销确认后返回
  204，本地撤销失败 fail closed，不得假装成功；
- 验收修订 5（本 RED 冻结，expired-calling 撤销竞态）：expired calling 的 fail-closed
  revoke 必须是 refresh 专用**条件撤销**（同时 CAS session_id + expected_version +
  refresh_state='calling' + observed owner token hash + lease 已过期），不得调用 logout 的
  「按 session id 单调 revoke」——winner A 在 loser B 观察到 expired calling 之后、B 撤销
  之前提交 v2 时，B 不得撤销 v2；条件撤销失败后必须重读分类：winner 已提交 expected+1 →
  返回 winner；已被其他路径撤销 → SessionRevokedError；其他状态 → SessionConflictError；
- 验收修订 5（本 RED 冻结，DB 状态不变量）：0004/model/domain 统一约束——
  idle ⟺ refresh_owner_token_hash IS NULL AND refresh_lease_expires_at IS NULL；
  leased/calling ⟹ 两者皆非 NULL；revoked 必须 idle 且 owner/lease 全空；非法持久化
  状态必须 fail closed，不得投影成看似合法的 domain 对象（_to_auth_session 不得静默掩盖）；
  0003→0004 升级必须先归一化 legacy 'refreshing' 行（fail-closed revoke）再建新约束，
  绝不把不确定的 legacy refreshing 会话恢复为 active；
- 全程 MockTransport + 本地签名 key，不访问真实 IdP；IdP token 不出现在任何 API 响应。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import asyncpg
import httpx2 as httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from sqlalchemy.engine import make_url

from zhiwei.app import create_app
from zhiwei.config.settings import load_settings
from zhiwei.contracts.canonical import canonical_json
from zhiwei.identity import sessions as sessions_module
from zhiwei.identity.domain import TokenAAD, TokenEnvelopePayload
from zhiwei.identity.sessions import (
    REFRESH_LEASE,
    LocalSessionRefreshUnitOfWork,
    SessionConflictError,
    SessionRevokedError,
    SessionService,
)
from zhiwei.secrets.base import SecretIntegrityError, SecretRef

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
IDENTITY_DSN = os.environ.get(
    "ZHIWEI_TEST_IDENTITY_DSN", "postgresql://zhiwei_identity@127.0.0.1:55432/zhiwei_test"
)

COOKIE = "__Host-zhiwei_session"
ISSUER = "https://idp.example.com"
CLIENT_ID = "zhiwei-bff"
CLIENT_SECRET = "ZW_TEST_CLIENT_SECRET_C9D5"
REDIRECT_URI = "https://app.example.com/auth/callback"
ACCESS_SENTINEL = "ZW_TEST_ACCESS_TOKEN_A7F3"
REFRESH_SENTINEL = "ZW_TEST_REFRESH_TOKEN_B8E4"
SENTINELS = (ACCESS_SENTINEL, REFRESH_SENTINEL, CLIENT_SECRET, "ZW_TEST_MASTER_KEY_D0E6")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_jwt(claims: dict[str, Any], key: rsa.RSAPrivateKey) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": "test-kid"}
    signing_input = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64url(
        json.dumps(claims, separators=(",", ":")).encode()
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


def _jwk(key: rsa.RSAPrivateKey) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "kid": "test-kid",
        "alg": "RS256",
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


class FakeIdP:
    """本地假 IdP：discovery / JWKS / authorize / token / revoke 全走 MockTransport。"""

    def __init__(self, key: rsa.RSAPrivateKey, subject: str) -> None:
        self.key = key
        self.subject = subject
        self.authorizations: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, str] = {}
        self.token_issuance: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, str] = {}
        self.invalid_refresh: set[str] = set()
        self.token_calls = 0
        self.refresh_calls = 0
        self.revoke_calls = 0

    def record_authorization(self, auth_url: str) -> dict[str, str]:
        params = parse_qs(urlparse(auth_url).query)
        state = params["state"][0]
        self.authorizations[state] = {
            "nonce": params["nonce"][0],
            "code_challenge": params["code_challenge"][0],
            "code_challenge_method": params["code_challenge_method"][0],
            "client_id": params["client_id"][0],
            "redirect_uri": params["redirect_uri"][0],
            "response_type": params["response_type"][0],
            "scope": params["scope"][0],
        }
        return self.authorizations[state]

    def issue_code(
        self,
        state: str,
        *,
        signer: rsa.RSAPrivateKey | None = None,
        id_token_overrides: dict[str, Any] | None = None,
    ) -> str:
        code = _b64url(os.urandom(18))
        self.codes[code] = state
        self.token_issuance[code] = {
            "signer": signer or self.key,
            "overrides": id_token_overrides or {},
        }
        return code

    def invalidate_refresh(self, token: str) -> None:
        self.invalid_refresh.add(token)

    def _id_token_claims(self, auth: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": [CLIENT_ID],
            "azp": CLIENT_ID,
            "sub": self.subject,
            "exp": now + 3600,
            "iat": now - 5,
            "nonce": auth["nonce"],
        }
        claims.update(overrides)
        return claims

    def _client_ok(self, request: httpx.Request) -> bool:
        auth = request.headers.get("authorization", "")
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if hmac.compare_digest(auth, expected):
            return True
        body = parse_qs(request.read().decode("utf-8"))
        return (
            body.get("client_id", [""])[0] == CLIENT_ID
            and body.get("client_secret", [""])[0] == CLIENT_SECRET
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "userinfo_endpoint": f"{ISSUER}/userinfo",
                    "revocation_endpoint": f"{ISSUER}/revoke",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [_jwk(self.key)]})
        if request.url.path == "/authorize":
            return httpx.Response(302, headers={"location": "/"}, content=b"")
        if request.url.path == "/token":
            self.token_calls += 1
            if not self._client_ok(request):
                return httpx.Response(401, json={"error": "invalid_client"})
            body = parse_qs(request.read().decode("utf-8"))
            grant_type = body.get("grant_type", [""])[0]
            if grant_type == "authorization_code":
                code = body.get("code", [""])[0]
                state = self.codes.get(code)
                if state is None:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                auth = self.authorizations[state]
                # Keycloak 语义（RFC 6749 §4.1.3）：code 换 token 必须携带与
                # authorize 完全一致的 redirect_uri；缺失/不一致 = invalid_grant。
                # s1-t6 §5-1 的预存缺陷即客户端漏传该字段，被 MockTransport 掩盖。
                if body.get("redirect_uri", [""])[0] != auth["redirect_uri"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                verifier = body.get("code_verifier", [""])[0]
                challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
                if not hmac.compare_digest(challenge, auth["code_challenge"]):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                issuance = self.token_issuance[code]
                id_token = _sign_jwt(
                    self._id_token_claims(auth, issuance["overrides"]),
                    issuance["signer"],
                )
                refresh_token = f"{REFRESH_SENTINEL}.1"
                self.refresh_tokens[refresh_token] = self.subject
                return httpx.Response(
                    200,
                    json={
                        "access_token": ACCESS_SENTINEL,
                        "refresh_token": refresh_token,
                        "id_token": id_token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            if grant_type == "refresh_token":
                token = body.get("refresh_token", [""])[0]
                if token not in self.refresh_tokens or token in self.invalid_refresh:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.refresh_calls += 1
                refresh_token = f"{REFRESH_SENTINEL}.{self.refresh_calls + 1}"
                self.refresh_tokens[refresh_token] = self.subject
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"{ACCESS_SENTINEL}.{self.refresh_calls}",
                        "refresh_token": refresh_token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if request.url.path == "/revoke":
            self.revoke_calls += 1
            return httpx.Response(200, content=b"")
        raise AssertionError(f"unexpected IdP request: {request.url}")


# --------------------------------------------------------------------------- fixtures


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1))
    config.attributes["database_url"] = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    return config


async def _assert_safe_test_database(dsn: str) -> None:
    url = make_url(dsn)
    if url.database != "zhiwei_test" or url.username != "zhiwei_migrator":
        raise RuntimeError("destructive migration tests require the dedicated zhiwei_test database")
    connection = await asyncpg.connect(dsn)
    try:
        database, user = await connection.fetchrow("SELECT current_database(), current_user")
        if database != "zhiwei_test" or user != "zhiwei_migrator":
            raise RuntimeError("connected database identity is not the dedicated migration test target")
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[None]:
    asyncio.run(_assert_safe_test_database(ADMIN_DSN))
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idp(signing_key: rsa.RSAPrivateKey) -> FakeIdP:
    return FakeIdP(signing_key, subject="alice-oidc")


@pytest.fixture
def keyring_path(tmp_path: Path) -> Path:
    path = tmp_path / "master.key"
    material = hashlib.sha256(b"ZW_TEST_MASTER_KEY_D0E6").digest()
    path.write_text(f"k1={base64.b64encode(material).decode('ascii')}\n", encoding="utf-8")
    return path


def _settings(keyring_path: Path) -> dict[str, str]:
    return {
        "ZHIWEI_PROFILE": "test",
        "ZHIWEI_DATABASE_URL": APP_DSN,
        "ZHIWEI_IDENTITY_DATABASE_URL": IDENTITY_DSN,
        "ZHIWEI_OIDC_ISSUER": ISSUER,
        "ZHIWEI_OIDC_CLIENT_ID": CLIENT_ID,
        "ZHIWEI_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "ZHIWEI_OIDC_REDIRECT_URI": REDIRECT_URI,
        "ZHIWEI_IDENTITY_MASTER_KEY_FILE": str(keyring_path),
        "ZHIWEI_OPA_BASE_URL": "http://opa.test:8181",
    }


def _policy_allow_client() -> httpx.AsyncClient:
    """测试用策略 transport：一律 allow（本文件不冻结授权语义，只跑通组合与 auth 流程）。

    RED 修订登记（docs/handoffs/s1-t4-repair-design.md §3.3）：create_app 组合期新增必需
    ZHIWEI_OPA_BASE_URL 与 policy 组合后，本文件的 bootstrap mutation 用例经真实 gate 求值，
    必须注入可控 policy transport 才能保持 201 断言。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "decision_id": "test-oidc-decision",
                "result": {"allow": True, "reason": "allow:org_owner"},
                "provenance": {
                    "version": "1.19.0",
                    "bundles": {"/bundle.tar.gz": {"revision": "test-oidc-rev"}},
                },
            },
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


@pytest_asyncio.fixture(loop_scope="function")
async def app_and_client(
    keyring_path: Path,
    idp: FakeIdP,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, FakeIdP]]:
    app = create_app(
        load_settings(_settings(keyring_path)),
        oidc_http_client=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler), timeout=5.0),
        policy_http_client=_policy_allow_client(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test", follow_redirects=False
    ) as client:
        yield app, client, idp
    await app.state.dispose_engines()


def _new_app(keyring_path: Path, idp: FakeIdP) -> FastAPI:
    return create_app(
        load_settings(_settings(keyring_path)),
        oidc_http_client=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler), timeout=5.0),
        policy_http_client=_policy_allow_client(),
    )


# --------------------------------------------------------------------------- seed helpers


async def _seed_principal(
    subject: str, *, kind: str = "user", status: str = "active"
) -> UUID:
    principal_id = uuid4()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO principals (id, kind, status, schema_version) VALUES ($1, $2, $3, 1)",
            principal_id,
            kind,
            status,
        )
        await connection.execute(
            "INSERT INTO external_identities (issuer, subject, principal_id) "
            "VALUES ($1, $2, $3) ON CONFLICT (issuer, subject) DO NOTHING",
            ISSUER,
            subject,
            principal_id,
        )
        existing = await connection.fetchval(
            "SELECT principal_id FROM external_identities WHERE issuer = $1 AND subject = $2",
            ISSUER,
            subject,
        )
    finally:
        await connection.close()
    if existing != principal_id:
        # 同一模块多次运行/同 subject 复用：清理本次孤儿 principal，返回既有主键
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            await connection.execute(
                "DELETE FROM principals WHERE id = $1 AND NOT EXISTS "
                "(SELECT 1 FROM external_identities WHERE principal_id = $1)",
                principal_id,
            )
        finally:
            await connection.close()
        return existing
    return principal_id


async def _seed_org(*, status: str = "active") -> UUID:
    organization_id = uuid4()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) VALUES ($1, $2, 1)",
            organization_id,
            status,
        )
    finally:
        await connection.close()
    return organization_id


async def _seed_membership(principal_id: UUID, organization_id: UUID, roles: list[str]) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO memberships (principal_id, organization_id, role_bindings) "
            "VALUES ($1, $2, $3::jsonb)",
            principal_id,
            organization_id,
            json.dumps(roles),
        )
    finally:
        await connection.close()


async def _seed_workspace(organization_id: UUID, *, name: str = "sales") -> UUID:
    workspace_id = uuid4()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) "
            "VALUES ($1, $2, $3, 1)",
            workspace_id,
            organization_id,
            name,
        )
    finally:
        await connection.close()
    return workspace_id


async def _seed_workspace_membership(
    principal_id: UUID, organization_id: UUID, workspace_id: UUID, roles: list[str]
) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO workspace_memberships "
            "(principal_id, organization_id, workspace_id, role_bindings) "
            "VALUES ($1, $2, $3, $4::jsonb)",
            principal_id,
            organization_id,
            workspace_id,
            json.dumps(roles),
        )
    finally:
        await connection.close()


async def _seed_alice_flow() -> tuple[UUID, UUID, UUID, UUID]:
    """alice 属于 org A（+ workspace），同时属于 org B；subject 固定为 alice-oidc。

    同模块多次运行先清空该 principal 的既有 memberships，保证每次测试看到的
    组织集合恰好是本测试 seed 的两个（不跨测试累积）。
    """
    principal = await _seed_principal("alice-oidc")
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "DELETE FROM memberships WHERE principal_id = $1", principal
        )
        await connection.execute(
            "DELETE FROM workspace_memberships WHERE principal_id = $1", principal
        )
    finally:
        await connection.close()
    org_a = await _seed_org()
    org_b = await _seed_org()
    workspace = await _seed_workspace(org_a)
    await _seed_membership(principal, org_a, ["member"])
    await _seed_membership(principal, org_b, ["member"])
    await _seed_workspace_membership(principal, org_a, workspace, ["builder"])
    return principal, org_a, org_b, workspace


async def _perform_login(client: httpx.AsyncClient, idp: FakeIdP) -> str:
    login = await client.get("/auth/login")
    assert login.status_code == 302
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state)
    callback = await client.get(f"/auth/callback?code={code}&state={state}")
    assert callback.status_code == 302
    cookie = client.cookies.get(COOKIE)
    assert cookie, "callback 必须签发 session cookie"
    return cookie


async def _session_row(cookie_token: str) -> dict[str, Any] | None:
    token_hash = hashlib.sha256(cookie_token.encode()).hexdigest()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await connection.fetchrow(
            "SELECT id, cookie_token_hash, principal_id, issuer, subject, encrypted_token_ref, "
            "csrf_hash, expires_at, idle_expires_at, revoked_at, version, refresh_state, "
            "refresh_lease_expires_at, refresh_owner_token_hash FROM auth_sessions "
            "WHERE cookie_token_hash = $1",
            token_hash,
        )
        return dict(row) if row else None
    finally:
        await connection.close()


def _owner_hash() -> str:
    """测试用 opaque owner token 的 SHA-256（合法 64 位 hex 占位）。"""
    return hashlib.sha256(b"test-owner-token").hexdigest()


async def _seed_legacy_refreshing_rows(seeded: list[Any]) -> None:
    """0003 schema 下 seed 合法 auth_sessions 行（refreshing / idle / revoked）。"""
    principal = await _seed_principal("migration-probe")
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        for state, lease_sql, revoked_sql in [
            ("refreshing", "now() + interval '30 seconds'", "NULL"),
            ("refreshing", "now() + interval '30 seconds'", "NULL"),
            ("idle", "NULL", "NULL"),
            ("idle", "NULL", "now()"),
        ]:
            session_id = uuid4()
            seeded.append(session_id)
            await connection.execute(
                "INSERT INTO auth_sessions (id, cookie_token_hash, principal_id, "
                "issuer, subject, encrypted_token_ref, csrf_hash, expires_at, "
                "idle_expires_at, created_at, updated_at, revoked_at, version, "
                "refresh_state, refresh_lease_expires_at, schema_version) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7, now() + interval '8 hours', "
                f"now() + interval '30 minutes', now(), now(), {revoked_sql}, 1, $8, "
                f"{lease_sql}, 1)",
                session_id,
                hashlib.sha256(os.urandom(16)).hexdigest(),
                principal,
                ISSUER,
                "migration-probe",
                str(uuid4()),
                hashlib.sha256(os.urandom(16)).hexdigest(),
                state,
            )
    finally:
        await connection.close()


async def _verify_0004_normalized_rows(seeded: list[Any]) -> None:
    """升级后断言：refreshing → fail-closed revoke（idle+revoked+version+1）；idle 行不变。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            "SELECT id, revoked_at, refresh_state, refresh_lease_expires_at, "
            "refresh_owner_token_hash, version FROM auth_sessions "
            "WHERE id = ANY($1::uuid[])",
            seeded,
        )
    finally:
        await connection.close()
    assert len(rows) == len(seeded)
    by_id = {r["id"]: dict(r) for r in rows}
    for r in rows:
        assert r["refresh_state"] == "idle", (
            f"升级后不允许残留 refreshing/leased/calling: {r['refresh_state']}"
        )
    # seeded 顺序: [refreshing, refreshing, idle, revoked-idle]
    for session_id in seeded[:2]:
        row = by_id[session_id]
        assert row["revoked_at"] is not None, "legacy refreshing 必须 fail-closed revoke"
        assert row["refresh_lease_expires_at"] is None
        assert row["refresh_owner_token_hash"] is None
        assert row["version"] == 2, "fail-closed revoke 必须单调递增版本（1 → 2）"
    for session_id in seeded[2:]:
        row = by_id[session_id]
        assert row["refresh_lease_expires_at"] is None
        assert row["refresh_owner_token_hash"] is None
        assert row["version"] == 1, "idle/revoked 行必须保持不变"


async def _delete_seeded_sessions(seeded: list[Any]) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        if seeded:
            await connection.execute(
                "DELETE FROM auth_sessions WHERE id = ANY($1::uuid[])", seeded
            )
    finally:
        await connection.close()


def _assert_no_sentinels(*texts: str) -> None:
    for text in texts:
        for sentinel in SENTINELS:
            assert sentinel not in text, f"sentinel {sentinel!r} 出现在响应/输出中"


async def _wait_for_refresh_state(session_id: UUID, expected: str, timeout: float = 5.0) -> None:
    """轮询 auth_sessions.refresh_state 直到到达预期状态（并发测试的同步点）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            state = await connection.fetchval(
                "SELECT refresh_state FROM auth_sessions WHERE id = $1", session_id
            )
        finally:
            await connection.close()
        if state == expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"auth_sessions.refresh_state 未在 {timeout}s 内变为 {expected!r}")


# --------------------------------------------------------------------------- C. OIDC login / callback


@pytest.mark.asyncio
async def test_login_redirects_with_state_nonce_and_pkce(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    response = await client.get("/auth/login")
    assert response.status_code == 302
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["scope"] == ["openid"]
    assert params["code_challenge_method"] == ["S256"]
    state, nonce, challenge = params["state"][0], params["nonce"][0], params["code_challenge"][0]
    assert len(state) > 20 and len(nonce) > 20

    auth = idp.record_authorization(response.headers["location"])
    assert auth["code_challenge"] == challenge
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await connection.fetchrow(
            "SELECT state_hash, nonce_hash, code_verifier, consumed_at FROM oidc_login_attempts "
            "WHERE state_hash = $1",
            hashlib.sha256(state.encode()).hexdigest(),
        )
        assert row is not None, "state 必须保存在短期 server-side attempt"
        assert row["state_hash"] == hashlib.sha256(state.encode()).hexdigest()
        assert row["nonce_hash"] == hashlib.sha256(nonce.encode()).hexdigest()
        verifier = row["code_verifier"]
        assert verifier
        assert _b64url(hashlib.sha256(verifier.encode()).digest()) == challenge
        assert row["consumed_at"] is None
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_callback_issues_fresh_opaque_session_with_safe_cookie(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None
    assert row["issuer"] == ISSUER
    assert row["subject"] == "alice-oidc"
    assert row["version"] == 1
    assert row["revoked_at"] is None
    assert len(row["encrypted_token_ref"]) > 0

    login = await client.get("/auth/login")
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state)
    callback = await client.get(f"/auth/callback?code={code}&state={state}")
    assert callback.status_code == 302
    set_cookie = callback.headers.get("set-cookie", "")
    assert "__Host-zhiwei_session=" in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Domain=" not in set_cookie

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        attempt = await connection.fetchrow(
            "SELECT consumed_at FROM oidc_login_attempts WHERE state_hash = $1",
            hashlib.sha256(state.encode()).hexdigest(),
        )
        assert attempt["consumed_at"] is not None, "attempt 必须一次性消费"
    finally:
        await connection.close()

    _assert_no_sentinels(callback.text, set_cookie, callback.headers.get("location", ""))


@pytest.mark.asyncio
async def test_callback_replay_fails(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    login = await client.get("/auth/login")
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state)
    first = await client.get(f"/auth/callback?code={code}&state={state}")
    assert first.status_code == 302
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        before = await connection.fetchval("SELECT count(*) FROM auth_sessions")
    finally:
        await connection.close()
    replay = await client.get(f"/auth/callback?code={code}&state={state}")
    assert replay.status_code == 403
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        after = await connection.fetchval("SELECT count(*) FROM auth_sessions")
        assert after == before, "replay 不得新增 session"
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"iss": "https://evil.example.com"}, "wrong issuer"),
        ({"aud": ["other-client"]}, "wrong audience"),
        ({"azp": "other-client"}, "wrong azp"),
        ({"nonce": "tampered-nonce"}, "wrong nonce"),
        ({"exp": int(time.time()) - 60}, "expired"),
        ({"iat": int(time.time()) + 3600}, "future iat"),
    ],
)
async def test_callback_rejects_invalid_id_tokens(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    overrides: dict[str, Any],
    label: str,
) -> None:
        _, client, idp = app_and_client
        await _seed_alice_flow()
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            before = await connection.fetchval("SELECT count(*) FROM auth_sessions")
        finally:
            await connection.close()
        login = await client.get("/auth/login")
        idp.record_authorization(login.headers["location"])
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        code = idp.issue_code(state, id_token_overrides=overrides)
        callback = await client.get(f"/auth/callback?code={code}&state={state}")
        assert callback.status_code == 403, label
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            after = await connection.fetchval("SELECT count(*) FROM auth_sessions")
            assert after == before, "被拒绝的 callback 不得新增 session"
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_callback_rejects_token_signed_by_other_key(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    login = await client.get("/auth/login")
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state, signer=other_key)
    callback = await client.get(f"/auth/callback?code={code}&state={state}")
    assert callback.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "kind", "status"),
    [
        ("never-seen-user", "user", "active"),
        ("alice-disabled", "user", "disabled"),
        ("alice-service", "service_account", "active"),
        ("alice-agent", "agent_identity", "active"),
    ],
)
async def test_callback_rejects_unknown_disabled_and_non_user_principals(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    idp: FakeIdP,
    subject: str,
    kind: str,
    status: str,
) -> None:
    idp.subject = subject
    if subject != "never-seen-user":
        await _seed_principal(subject, kind=kind, status=status)
    _, client, _ = app_and_client
    login = await client.get("/auth/login")
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state)
    callback = await client.get(f"/auth/callback?code={code}&state={state}")
    assert callback.status_code == 403
    assert COOKIE not in callback.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_callback_never_reuses_preexisting_cookie(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    login = await client.get("/auth/login")
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state)
    # 预置攻击者 cookie：以请求头携带（等价浏览器已带 cookie），绕过 httpx jar
    # 的 domain 键歧义；回调后只认服务端 Set-Cookie 的新值
    callback = await client.get(
        f"/auth/callback?code={code}&state={state}",
        headers={"Cookie": f"{COOKIE}=attacker-planted-cookie"},
    )
    assert callback.status_code == 302
    set_cookie = callback.headers.get("set-cookie", "")
    new_cookie = set_cookie.split(";")[0].split("=", 1)[1]
    assert new_cookie != "attacker-planted-cookie"
    row = await _session_row(new_cookie)
    assert row is not None
    assert row["cookie_token_hash"] == hashlib.sha256(new_cookie.encode()).hexdigest()
    planted = await _session_row("attacker-planted-cookie")
    assert planted is None
    _assert_no_sentinels(new_cookie)


# --------------------------------------------------------------------------- D. session / CSRF / logout / me


@pytest.mark.asyncio
async def test_me_requires_valid_session(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, _ = app_and_client
    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    client.cookies.set(COOKIE, "random-bogus-token")
    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    set_cookie = response.headers.get("set-cookie", "")
    assert "__Host-zhiwei_session=" in set_cookie and "Max-Age=0" in set_cookie


@pytest.mark.asyncio
async def test_csrf_and_origin_gate_cookie_authenticated_mutations(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    await _perform_login(client, idp)

    org_id = uuid4()
    body = {"organization_id": str(org_id)}
    headers = {"Idempotency-Key": "csrf-org"}

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        before_orgs = await connection.fetchval("SELECT count(*) FROM organizations")
        before_idem = await connection.fetchval("SELECT count(*) FROM idempotency_records")
    finally:
        await connection.close()

    # 无 CSRF → 403
    response = await client.post("/api/v1/organizations", json=body, headers=headers)
    assert response.status_code == 403
    me = await client.get("/api/v1/me")
    csrf_token = me.json()["csrf_token"]
    # Origin 缺失 → 403
    response = await client.post(
        "/api/v1/organizations", json=body, headers={**headers, "X-CSRF-Token": csrf_token}
    )
    assert response.status_code == 403
    # Origin 不匹配 → 403
    response = await client.post(
        "/api/v1/organizations",
        json=body,
        headers={**headers, "X-CSRF-Token": csrf_token, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    # 错误 CSRF → 403
    response = await client.post(
        "/api/v1/organizations",
        json=body,
        headers={**headers, "X-CSRF-Token": "wrong-csrf", "Origin": "https://test"},
    )
    assert response.status_code == 403

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM organizations") == before_orgs
        assert (
            await connection.fetchval("SELECT count(*) FROM idempotency_records")
            == before_idem
        )
    finally:
        await connection.close()

    # 正确 CSRF + 可信 Origin → 成功
    response = await client.post(
        "/api/v1/organizations",
        json=body,
        headers={**headers, "X-CSRF-Token": csrf_token, "Origin": "https://test"},
    )
    assert response.status_code == 201
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_id)
            == 1
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_logout_revokes_locally_clears_cookie_and_replay_fails(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    me = await client.get("/api/v1/me")
    csrf_token = me.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf_token, "Origin": "https://test"}

    response = await client.post("/auth/logout", headers=headers)
    assert response.status_code == 204
    assert client.cookies.get(COOKIE) is None

    revoked = await _session_row(cookie)
    assert revoked is not None
    assert revoked["revoked_at"] is not None
    assert revoked["refresh_state"] == "idle"
    assert revoked["refresh_lease_expires_at"] is None
    assert revoked["version"] == 2

    # 重放 logout / me → 401
    response = await client.post("/auth/logout", headers=headers)
    assert response.status_code == 401
    response = await client.get("/api/v1/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_disabled_principal_invalidates_existing_session(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    idp: FakeIdP,
) -> None:
    # 独立 subject：禁用只影响本测试，不污染同模块其他用例的 alice-oidc
    idp.subject = "alice-disable-me"
    principal = await _seed_principal("alice-disable-me")
    _, client, _ = app_and_client
    await _perform_login(client, idp)
    assert (await client.get("/api/v1/me")).status_code == 200

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "UPDATE principals SET status = 'disabled' WHERE id = $1", principal
        )
    finally:
        await connection.close()

    response = await client.get("/api/v1/me")
    assert response.status_code == 401
    assert "Max-Age=0" in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_idle_and_absolute_expiry_fail_closed(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None
    session_id = row["id"]

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "UPDATE auth_sessions SET idle_expires_at = now() - interval '1 minute' WHERE id = $1",
            session_id,
        )
    finally:
        await connection.close()
    response = await client.get("/api/v1/me")
    assert response.status_code == 401

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        # 绝对过期状态必须满足不变量 idle <= expires（ck_auth_sessions_expiry_ordering）：
        # 到达绝对 deadline 时 idle 必然也已过期，401 由过期触发
        await connection.execute(
            "UPDATE auth_sessions SET idle_expires_at = now() - interval '2 minutes', "
            "expires_at = now() - interval '1 minute' WHERE id = $1",
            session_id,
        )
    finally:
        await connection.close()
    response = await client.get("/api/v1/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_context_comes_from_verified_membership(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    principal, org_a, org_b, workspace = await _seed_alice_flow()
    await _perform_login(client, idp)

    me = await client.get("/api/v1/me")
    assert me.status_code == 200
    body = me.json()
    assert body["principal"]["id"] == str(principal)
    assert body["principal"]["kind"] == "user"
    assert body["principal"]["status"] == "active"
    org_ids = {org["id"] for org in body["organizations"]}
    assert org_ids == {str(org_a), str(org_b)}

    # 声明合法 org + workspace context（workspace 属于 member org 且有 workspace membership）
    me = await client.get(
        "/api/v1/me",
        headers={"X-ZhiWei-Organization": str(org_a), "X-ZhiWei-Workspace": str(workspace)},
    )
    assert me.status_code == 200
    assert me.json()["context"] == {
        "organization_id": str(org_a),
        "workspace_id": str(workspace),
    }

    # 声明的 org 是 member 但未声明 workspace → 200，workspace 为空
    me = await client.get("/api/v1/me", headers={"X-ZhiWei-Organization": str(org_b)})
    assert me.status_code == 200
    assert me.json()["context"] == {"organization_id": str(org_b), "workspace_id": None}


@pytest.mark.asyncio
async def test_declared_context_without_membership_fails_closed(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    other_org = await _seed_org()
    other_workspace = await _seed_workspace(other_org)
    await _perform_login(client, idp)

    response = await client.get(
        "/api/v1/me", headers={"X-ZhiWei-Organization": str(other_org)}
    )
    assert response.status_code == 404
    response = await client.get(
        "/api/v1/me",
        headers={
            "X-ZhiWei-Organization": str(other_org),
            "X-ZhiWei-Workspace": str(other_workspace),
        },
    )
    assert response.status_code == 404

    # mutation：跨组织声明 → 403
    me = await client.get("/api/v1/me")
    csrf_token = me.json()["csrf_token"]
    response = await client.post(
        "/api/v1/organizations",
        json={"organization_id": str(uuid4())},
        headers={
            "Idempotency-Key": "cross-org-mutation",
            "X-CSRF-Token": csrf_token,
            "Origin": "https://test",
            "X-ZhiWei-Organization": str(other_org),
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_organizations_list_returns_all_and_only_member_orgs(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    _, org_a, org_b, _ = await _seed_alice_flow()
    non_member_org = await _seed_org()
    await _perform_login(client, idp)

    response = await client.get("/api/v1/organizations")
    assert response.status_code == 200
    listed = response.json()
    ids = {entry["id"] for entry in listed}
    assert ids == {str(org_a), str(org_b)}
    assert str(non_member_org) not in ids
    for entry in listed:
        assert entry["status"] == "active"


@pytest.mark.asyncio
async def test_me_never_leaks_tokens_or_issuer_subject(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    _, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    response = await client.get("/api/v1/me")
    assert response.status_code == 200
    body_text = response.text
    assert "issuer" not in body_text
    assert "subject" not in body_text
    assert "access_token" not in body_text and "refresh_token" not in body_text
    assert "encrypted_token_ref" not in body_text
    _assert_no_sentinels(body_text, response.headers.get("set-cookie", ""), cookie)

    org_response = await client.get("/api/v1/organizations")
    _assert_no_sentinels(org_response.text)


# --------------------------------------------------------------------------- E. refresh / CAS


@pytest.mark.asyncio
async def test_concurrent_refresh_makes_single_idp_call(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    keyring_path: Path,
    idp: FakeIdP,
) -> None:
    await _seed_alice_flow()
    app, client, _ = app_and_client
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    second = _new_app(keyring_path, idp)
    try:
        s1 = app.state.session_service
        s2 = second.state.session_service
        results = await asyncio.gather(
            s1.refresh_session(row["id"], expected_version=1),
            s2.refresh_session(row["id"], expected_version=1),
        )
        assert idp.refresh_calls == 1, "两个 replica 并发 refresh 只能有一次 IdP 调用"
        final = await _session_row(cookie)
        assert final is not None
        assert final["version"] == 2
        assert final["revoked_at"] is None
        assert final["refresh_state"] == "idle"
        assert final["refresh_lease_expires_at"] is None
        for result in results:
            assert result.id == row["id"]
            assert result.version == 2
        assert (await client.get("/api/v1/me")).status_code == 200
    finally:
        await second.state.dispose_engines()


@pytest.mark.asyncio
async def test_refresh_invalid_grant_revokes_session_locally(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None
    idp.invalidate_refresh(f"{REFRESH_SENTINEL}.1")

    with pytest.raises(SessionRevokedError):
        await app.state.session_service.refresh_session(row["id"], expected_version=1)
    revoked = await _session_row(cookie)
    assert revoked is not None
    assert revoked["revoked_at"] is not None
    assert revoked["version"] == 2
    assert (await client.get("/api/v1/me")).status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_stale_expected_version_fails_closed(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    await app.state.session_service.refresh_session(row["id"], expected_version=1)
    with pytest.raises(SessionConflictError):
        await app.state.session_service.refresh_session(row["id"], expected_version=1)
    final = await _session_row(cookie)
    assert final is not None
    assert final["version"] == 2
    assert final["revoked_at"] is None
    assert (await client.get("/api/v1/me")).status_code == 200


@pytest.mark.asyncio
async def test_revoke_during_refresh_race_fails_closed(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "UPDATE auth_sessions SET refresh_state = 'calling', "
            "refresh_owner_token_hash = $2, "
            "refresh_lease_expires_at = now() + interval '30 seconds' WHERE id = $1",
            row["id"],
            _owner_hash(),
        )
    finally:
        await connection.close()

    assert (
        await app.state.session_service.revoke_session(row["id"], expected_version=1) is True
    )
    with pytest.raises(SessionRevokedError):
        await app.state.session_service.refresh_session(row["id"], expected_version=1)
    final = await _session_row(cookie)
    assert final is not None
    assert final["revoked_at"] is not None
    assert final["version"] == 2


@pytest.mark.asyncio
async def test_stale_logout_replay_still_revokes_refreshed_session(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """修订（验收确认）：logout 是单调安全操作，旧版本 logout 重放不得被刷新击败。

    原契约冻结了「旧 logout 不撤销刷新后 session」的语义（CAS revoke 失败即放弃），
    但 session id 是永不重用的 uuid，撤销按 id 生效与版本无关：同一 session 的刷新
    不应使撤销失效。logout 必须单调：无论 expected_version 是否过期，只要 session
    仍存活，撤销必须生效并清空 lease（版本仍递增，打断在途 refresh CAS）。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    await app.state.session_service.refresh_session(row["id"], expected_version=1)
    # 旧版本 logout 重放（模拟旧请求迟到）：必须仍撤销服务端 session
    assert await app.state.session_service.revoke_session(
        row["id"], expected_version=1
    ) is True
    final = await _session_row(cookie)
    assert final is not None
    assert final["version"] == 3
    assert final["revoked_at"] is not None
    assert final["refresh_lease_expires_at"] is None
    assert (await client.get("/api/v1/me")).status_code == 401


@pytest.mark.asyncio
async def test_logout_race_with_refresh_still_revokes_server_session(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """冻结（验收阻断 1）：logout 与 refresh 竞态时，服务端 session 必须被撤销。

    竞态排列：actor 解析读到 v1 后、revoke CAS 执行前，refresh 抢先提交 v2。
    若 revoke 按 expected_version CAS 会失败；logout 是单调安全操作，必须仍撤销
    服务端 session——handler 不得返回 204 而服务端 session 存活。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    me = await client.get("/api/v1/me")
    csrf_token = me.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf_token, "Origin": "https://test"}

    store = app.state.session_service._session_store
    orig_revoke = store.revoke_session

    async def racy_revoke(session_id: UUID, expected_version: int) -> bool:
        # 模拟竞态：revoke CAS 执行前，refresh 抢先提交 v2
        await app.state.session_service.refresh_session(session_id, expected_version=1)
        return await orig_revoke(session_id, expected_version)

    store.revoke_session = racy_revoke
    try:
        response = await client.post("/auth/logout", headers=headers)
        assert response.status_code == 204
        final = await _session_row(cookie)
        assert final is not None
        assert final["revoked_at"] is not None
        assert (await client.get("/api/v1/me")).status_code == 401
    finally:
        store.revoke_session = orig_revoke


@pytest.mark.asyncio
async def test_logout_fails_closed_when_local_revoke_unavailable(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """冻结（验收阻断 1）：本地 revoke 未确认时，logout 不得返回 204 假装成功。

    revoke 返回 False（撤销未生效）而服务端 session 仍存活时，handler 必须
    fail closed（非 204），并保留 cookie 供客户端重试。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)

    me = await client.get("/api/v1/me")
    csrf_token = me.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf_token, "Origin": "https://test"}

    store = app.state.session_service._session_store
    orig_revoke = store.revoke_session

    async def failing_revoke(session_id: UUID, expected_version: int) -> bool:
        return False

    store.revoke_session = failing_revoke
    try:
        response = await client.post("/auth/logout", headers=headers)
        assert response.status_code != 204
        final = await _session_row(cookie)
        assert final is not None
        assert final["revoked_at"] is None
        assert (await client.get("/api/v1/me")).status_code == 200
    finally:
        store.revoke_session = orig_revoke


@pytest.mark.asyncio
async def test_session_and_envelope_survive_restart(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    keyring_path: Path,
    idp: FakeIdP,
) -> None:
    await _seed_alice_flow()
    app, client, _ = app_and_client
    cookie = await _perform_login(client, idp)
    await app.state.dispose_engines()

    restarted = _new_app(keyring_path, idp)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="https://test"
        ) as raw_client:
            response = await raw_client.get(
                "/api/v1/me", headers={"Cookie": f"{COOKIE}={cookie}"}
            )
            assert response.status_code == 200
        row = await _session_row(cookie)
        assert row is not None
        refreshed = await restarted.state.session_service.refresh_session(
            row["id"], expected_version=1
        )
        assert refreshed.version == 2
        assert idp.refresh_calls == 1
    finally:
        await restarted.state.dispose_engines()


# --------------------------------------------------------------------------- F. 验收阻断冻结：并发 refresh 契约


@pytest.mark.asyncio
async def test_refresh_loser_accepts_concurrent_winner_under_app_db_clock_skew(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    keyring_path: Path,
    idp: FakeIdP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结（验收阻断 2）：winner 判定不得比较应用时钟与 DB updated_at。

    独立反例：应用时钟比 DB 快 5s 时，并发 winner 的 updated_at（DB）落在 loser 的
    attempted_at（应用时钟）之前，被判为 stale。attempt 时间必须与 updated_at 同源
    （数据库时钟域），时钟偏差不得改变并发 winner 判定。

    修订（验收修订 5）：模拟 winner 完成的 UPDATE 必须同时清除 refresh_owner_token_hash
    ——idle 不变量要求 owner/lease 皆空，不得靠放宽 DB 约束通过。
    """
    await _seed_alice_flow()
    _, client, _ = app_and_client
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    # 模拟应用时钟领先 DB 时钟 5s
    real = datetime.now(UTC)
    monkeypatch.setattr(
        sessions_module, "utc_now", lambda: real + timedelta(seconds=5)
    )

    second = _new_app(keyring_path, idp)
    try:
        s2 = second.state.session_service
        # 先让 loser 处于 lease 竞争失败路径：row 停在 calling（winner 已进入 calling）
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            await connection.execute(
                "UPDATE auth_sessions SET refresh_state = 'calling', "
                "refresh_owner_token_hash = $2, "
                "refresh_lease_expires_at = now() + interval '30 seconds' "
                "WHERE id = $1",
                row["id"],
                _owner_hash(),
            )
        finally:
            await connection.close()

        # loser 的 _wait_for_winner 首次重读：模拟 winner 恰在其 I/O 期间完成
        orig_get = s2._session_store.get_session
        calls = {"n": 0}

        async def completing_get(session_id: UUID):
            calls["n"] += 1
            if calls["n"] == 2:  # _wait_for_winner 的首次重读
                await asyncio.sleep(0.2)
                connection = await asyncpg.connect(ADMIN_DSN)
                try:
                    await connection.execute(
                        "UPDATE auth_sessions SET version = 2, updated_at = now(), "
                        "refresh_state = 'idle', refresh_lease_expires_at = NULL, "
                        "refresh_owner_token_hash = NULL "
                        "WHERE id = $1",
                        session_id,
                    )
                finally:
                    await connection.close()
            return await orig_get(session_id)

        s2._session_store.get_session = completing_get
        try:
            result = await s2.refresh_session(row["id"], expected_version=1)
        finally:
            s2._session_store.get_session = orig_get
    finally:
        await second.state.dispose_engines()

    # 冻结：并发 winner 结果必须被接受，不得因时钟偏差抛 SessionConflictError
    assert result.version == 2
    assert (await client.get("/api/v1/me")).status_code == 200


@pytest.mark.asyncio
async def test_refresh_loser_waits_for_slow_winner_within_lease(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    keyring_path: Path,
    idp: FakeIdP,
) -> None:
    """冻结（验收阻断 2）：输家等待上界是 lease 剩余，不是固定 2 秒。

    winner 的 IdP 刷新耗时 3s（> 当前 40×0.05=2s 轮询预算，< 30s lease）；
    loser 必须等到 winner 完成并返回其新版本，不得提前抛 SessionConflictError。
    """
    await _seed_alice_flow()
    app, client, _ = app_and_client
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    second = _new_app(keyring_path, idp)
    try:
        s1 = app.state.session_service
        s2 = second.state.session_service
        orig_refresh = s1._oidc_service.refresh_tokens

        async def slow_refresh(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(3.0)
            return await orig_refresh(*args, **kwargs)

        s1._oidc_service.refresh_tokens = slow_refresh

        winner = asyncio.create_task(
            s1.refresh_session(row["id"], expected_version=1)
        )
        await _wait_for_refresh_state(row["id"], "calling")
        loser = asyncio.create_task(
            s2.refresh_session(row["id"], expected_version=1)
        )
        results = await asyncio.gather(winner, loser)
        assert {r.version for r in results} == {2}
        assert idp.refresh_calls == 1
        final = await _session_row(cookie)
        assert final is not None
        assert final["version"] == 2
        assert final["revoked_at"] is None
        assert (await client.get("/api/v1/me")).status_code == 200
    finally:
        await second.state.dispose_engines()


@pytest.mark.asyncio
async def test_refresh_takes_over_expired_abandoned_lease(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """冻结（验收阻断 2/4）：lease 过期且行停在 leased（winner 崩溃遗留，尚未调用 IdP）
    时，下一位调用方必须接管 ownership 完成刷新（数据库侧 attempt/lease ownership），
    不得永久卡死或只抛 SessionConflictError。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "UPDATE auth_sessions SET refresh_state = 'leased', "
            "refresh_owner_token_hash = $2, "
            "refresh_lease_expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            row["id"],
            _owner_hash(),
        )
    finally:
        await connection.close()

    result = await app.state.session_service.refresh_session(
        row["id"], expected_version=1
    )
    assert result.version == 2
    final = await _session_row(cookie)
    assert final is not None
    assert final["refresh_state"] == "idle"
    assert final["refresh_lease_expires_at"] is None
    assert final["revoked_at"] is None
    assert idp.refresh_calls == 1
    assert (await client.get("/api/v1/me")).status_code == 200


@pytest.mark.asyncio
async def test_refresh_commit_failure_rolls_back_envelope_and_revokes_session(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """修订（验收确认，A 档契约修订）：IdP refresh 已成功但 envelope/session 本地
    事务失败时，事务必须整体回滚；外部 token 状态已经不确定 → 会话必须随后本地
    revoke；禁止重试旧 refresh token。

    原契约「session 保持 active 且重试必须成功」的期望已删除：IdP 侧旧 refresh
    token 可能已被轮换，重试旧 token 会拿到 invalid_grant 或导致 double-use——
    唯一确定性安全动作是本地 revoke（fail closed）。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None
    svc = app.state.session_service
    store = svc._session_store
    orig_complete = store.complete_refresh
    crashed = {"done": False}

    async def crash_after_envelope(*args: Any, **kwargs: Any) -> Any:
        # 模拟进程死在 envelope 写入之后、session CAS 之前（同一事务内）
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError(
                "simulated crash between envelope put and session CAS"
            )
        return await orig_complete(*args, **kwargs)

    store.complete_refresh = crash_after_envelope
    try:
        with pytest.raises(SessionRevokedError):
            await svc.refresh_session(row["id"], expected_version=1)
    finally:
        store.complete_refresh = orig_complete

    # IdP 调用恰好一次：刷新成功但本地提交失败，且绝不重试旧 refresh token
    assert idp.refresh_calls == 1

    # 事务整体回滚：session 仍 v1 的 envelope 必须保持可解；v2 AAD 不得解出新 envelope
    v1_aad = TokenAAD(
        purpose="oidc_session",
        session_id=row["id"],
        issuer=row["issuer"],
        subject=row["subject"],
        session_version=1,
        schema_version=1,
    ).encode()
    old_payload = json.loads(
        await svc._secret_backend.get(SecretRef(str(row["id"])), v1_aad)
    )
    assert old_payload["refresh_token"] == f"{REFRESH_SENTINEL}.1"
    v2_aad = TokenAAD(
        purpose="oidc_session",
        session_id=row["id"],
        issuer=row["issuer"],
        subject=row["subject"],
        session_version=2,
        schema_version=1,
    ).encode()
    with pytest.raises(SecretIntegrityError):
        await svc._secret_backend.get(SecretRef(str(row["id"])), v2_aad)

    # 会话随后本地 revoke（fail closed），/me 401
    after = await _session_row(cookie)
    assert after is not None
    assert after["revoked_at"] is not None
    assert after["version"] == 2
    assert after["refresh_state"] == "idle"
    assert after["refresh_lease_expires_at"] is None
    assert after["refresh_owner_token_hash"] is None
    assert (await client.get("/api/v1/me")).status_code == 401


# --------------------------------------------------------------------------- G. 验收阻断 4 冻结：refresh fencing / durable calling barrier


class PortOnlySecretBackend:
    """只实现 SecretBackend port 契约的包装；绝无 external_session 等私有参数。

    SessionService 业务层若调用 LocalSecretBackend 的 concrete-only 签名，
    （如 put(..., external_session=...)）会在本包装上 TypeError——契约测试的证据。
    """

    def __init__(self, real: Any) -> None:
        self._real = real

    async def put(
        self,
        ref: SecretRef,
        plaintext: bytes,
        aad: bytes,
        purpose: str,
        expected_version: int | None = None,
    ) -> Any:
        return await self._real.put(ref, plaintext, aad, purpose, expected_version)

    async def get(self, ref: SecretRef, aad: bytes) -> bytes:
        return await self._real.get(ref, aad)

    async def revoke(self, ref: SecretRef) -> None:
        return await self._real.revoke(ref)

    async def rewrap(self, ref: SecretRef, aad: bytes, expected_version: int) -> Any:
        return await self._real.rewrap(ref, aad, expected_version)

    def rotate(self, *, key_id: str | None = None, key_material: bytes | None = None) -> str:
        return self._real.rotate(key_id=key_id, key_material=key_material)


@pytest.mark.asyncio
async def test_live_owner_crosses_lease_with_single_idp_call_and_fenced_stale_owner(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    keyring_path: Path,
    idp: FakeIdP,
) -> None:
    """冻结（验收阻断 4）：live owner A 跨 lease 场景。

    - A 取得 lease 但在调用 IdP 前暂停到 lease 过期（停在 leased）；
    - replica B 接管（expired leased 可接管）并成功完成刷新；
    - 整个 session generation 最多一次 IdP refresh；
    - 旧 owner A 释放后不能 release/complete 新 owner B 的 lease（enter calling CAS
      被 fence，不调用 IdP）；
    - 最终只能是一个新版本成功提交（v2 active），或会话被单调撤销；
    - 禁止「IdP 调用两次但 session 仍 active」。
    """
    await _seed_alice_flow()
    app, client, _ = app_and_client
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    second = _new_app(keyring_path, idp)
    try:
        s1 = app.state.session_service
        s2 = second.state.session_service
        a_paused = asyncio.Event()
        release_a = asyncio.Event()
        orig_enter = s1._session_store.enter_calling_phase

        async def paused_enter(session_id: UUID, expected_version: int, owner_token: str) -> Any:
            # A 取得 lease 后、调用 IdP 前暂停：停在 leased，等待 release
            a_paused.set()
            await release_a.wait()
            return await orig_enter(session_id, expected_version, owner_token)

        s1._session_store.enter_calling_phase = paused_enter
        a_task = asyncio.create_task(s1.refresh_session(row["id"], expected_version=1))
        await asyncio.wait_for(a_paused.wait(), timeout=15)
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            state = await connection.fetchval(
                "SELECT refresh_state FROM auth_sessions WHERE id = $1", row["id"]
            )
            assert state == "leased", "A 必须停在 leased（未进入 calling）"
            # 让 A 的 lease 过期
            await connection.execute(
                "UPDATE auth_sessions SET refresh_lease_expires_at = now() - interval '1 second' "
                "WHERE id = $1",
                row["id"],
            )
        finally:
            await connection.close()

        # replica B 接管 expired leased 并完成：恰好一次 IdP 调用
        b_result = await s2.refresh_session(row["id"], expected_version=1)
        assert b_result.version == 2
        assert idp.refresh_calls == 1

        # 释放 A：A 以陈旧 owner token 尝试进入 calling → 被 fence，不调用 IdP
        release_a.set()
        with pytest.raises(SessionConflictError):
            await a_task
        assert idp.refresh_calls == 1, "旧 owner 不得触发第二次 IdP 调用"

        final = await _session_row(cookie)
        assert final is not None
        assert final["version"] == 2
        assert final["revoked_at"] is None
        assert final["refresh_state"] == "idle"
        assert final["refresh_lease_expires_at"] is None
        assert final["refresh_owner_token_hash"] is None
        assert (await client.get("/api/v1/me")).status_code == 200
    finally:
        await second.state.dispose_engines()


@pytest.mark.asyncio
async def test_expired_calling_lease_revokes_locally_without_second_idp_call(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """冻结（验收阻断 4，durable calling barrier）：owner 已进入 calling 且 lease 过期
    时（进程死亡遗留），下一请求不得再次调用 IdP——本地 revoke，fail closed。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "UPDATE auth_sessions SET refresh_state = 'calling', "
            "refresh_owner_token_hash = $2, "
            "refresh_lease_expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            row["id"],
            _owner_hash(),
        )
    finally:
        await connection.close()

    with pytest.raises(SessionRevokedError):
        await app.state.session_service.refresh_session(row["id"], expected_version=1)
    assert idp.refresh_calls == 0, "expired calling 不得再次调用 IdP"
    revoked = await _session_row(cookie)
    assert revoked is not None
    assert revoked["revoked_at"] is not None
    assert revoked["version"] == 2
    assert revoked["refresh_state"] == "idle"
    assert revoked["refresh_owner_token_hash"] is None
    assert (await client.get("/api/v1/me")).status_code == 401


@pytest.mark.asyncio
async def test_owner_stuck_in_calling_after_idp_call_revoked_by_next_caller(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """冻结（验收阻断 4）：成功并发路径恰好一次调用；故障不确定路径总调用数至多一次。

    winner 已调用 IdP（1 次）但死在 complete 之前（停在 calling，lease 随后过期）；
    下一请求必须本地 revoke，不得再次调用 IdP——总调用数仍为 1。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    svc = app.state.session_service
    store = svc._session_store
    orig_complete = store.complete_refresh
    stuck = asyncio.Event()

    async def stuck_complete(*args: Any, **kwargs: Any) -> Any:
        await stuck.wait()
        return await orig_complete(*args, **kwargs)

    store.complete_refresh = stuck_complete
    try:
        winner = asyncio.create_task(svc.refresh_session(row["id"], expected_version=1))
        # winner 已进入 calling 并完成 IdP 调用，卡在 complete 前
        await _wait_for_refresh_state(row["id"], "calling")
        assert idp.refresh_calls == 1
        # lease 过期
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            await connection.execute(
                "UPDATE auth_sessions SET refresh_lease_expires_at = now() - interval '1 second' "
                "WHERE id = $1",
                row["id"],
            )
        finally:
            await connection.close()

        # 下一请求：不得再次调用 IdP，本地 revoke
        with pytest.raises(SessionRevokedError):
            await svc.refresh_session(row["id"], expected_version=1)
        assert idp.refresh_calls == 1, "故障不确定路径总调用数至多一次"
        revoked = await _session_row(cookie)
        assert revoked is not None
        assert revoked["revoked_at"] is not None
        assert revoked["version"] == 2
        assert revoked["refresh_state"] == "idle"
        assert revoked["refresh_owner_token_hash"] is None

        # 释放卡住的 winner：其 complete CAS 被 revoked 状态 fence，整体回滚
        stuck.set()
        with pytest.raises(SessionRevokedError):
            await winner
        assert idp.refresh_calls == 1
    finally:
        store.complete_refresh = orig_complete


@pytest.mark.asyncio
async def test_refresh_fencing_fences_stale_owner_token_at_store_level(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """冻结（验收阻断 4，stale owner fencing）：acquire/start-call/complete/release
    全部校验 opaque owner token；旧 owner 不得清除或提交后来 owner 的状态。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None
    store = app.state.session_service._session_store
    sid = row["id"]
    idle_expires_at = row["idle_expires_at"]

    # owner A：acquire 返回 opaque token，DB 只存其 SHA-256
    token_a, _ = await store.acquire_refresh_lease(sid, expected_version=1, lease=REFRESH_LEASE)
    assert isinstance(token_a, str) and len(token_a) > 16
    row_after_acquire = await _session_row(cookie)
    assert row_after_acquire is not None
    assert row_after_acquire["refresh_owner_token_hash"] == hashlib.sha256(
        token_a.encode("utf-8")
    ).hexdigest()
    assert row_after_acquire["refresh_state"] == "leased"

    # A 未进入 calling 且 lease 过期 → B 可接管（新 owner token）
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "UPDATE auth_sessions SET refresh_lease_expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            sid,
        )
    finally:
        await connection.close()
    token_b, _ = await store.acquire_refresh_lease(sid, expected_version=1, lease=REFRESH_LEASE)
    assert token_b != token_a

    # 旧 owner A 的所有操作都被 fence（错误 token → False，不改变行状态）
    assert await store.enter_calling_phase(sid, expected_version=1, owner_token=token_a) is False
    assert await store.complete_refresh(
        sid,
        expected_version=1,
        owner_token=token_a,
        encrypted_token_ref=str(sid),
        idle_expires_at=idle_expires_at,
    ) is False
    assert await store.release_refresh_lease(sid, expected_version=1, owner_token=token_a) is False
    still_leased = await _session_row(cookie)
    assert still_leased is not None and still_leased["refresh_state"] == "leased"

    # 新 owner B 正常完成：calling → complete
    assert await store.enter_calling_phase(sid, expected_version=1, owner_token=token_b) is True
    assert await store.complete_refresh(
        sid,
        expected_version=1,
        owner_token=token_b,
        encrypted_token_ref=str(sid),
        idle_expires_at=idle_expires_at,
    ) is True
    final = await _session_row(cookie)
    assert final is not None
    assert final["version"] == 2
    assert final["refresh_state"] == "idle"
    assert final["refresh_owner_token_hash"] is None
    assert (await client.get("/api/v1/me")).status_code == 200


@pytest.mark.asyncio
async def test_refresh_with_missing_refresh_token_revokes_session(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """冻结（验收阻断 4）：envelope 中 refresh_token=None 时抛 SessionRevokedError；
    auth_sessions.revoked_at 必须非空；后续 /me 返回 401。"""
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None
    svc = app.state.session_service

    # 用真实 backend 改写 envelope：无 refresh token（只发 access token 的会话）
    aad = TokenAAD(
        purpose="oidc_session",
        session_id=row["id"],
        issuer=row["issuer"],
        subject=row["subject"],
        session_version=1,
        schema_version=1,
    ).encode()
    payload = TokenEnvelopePayload(
        purpose="oidc_session",
        token_kind="access_refresh",
        access_token=ACCESS_SENTINEL,
        refresh_token=None,
        schema_version=1,
    )
    await svc._secret_backend.put(
        SecretRef(str(row["id"])),
        canonical_json(payload.model_dump(mode="json")),
        aad,
        purpose="oidc_session",
    )

    with pytest.raises(SessionRevokedError):
        await svc.refresh_session(row["id"], expected_version=1)
    revoked = await _session_row(cookie)
    assert revoked is not None
    assert revoked["revoked_at"] is not None
    assert revoked["version"] == 2
    assert revoked["refresh_state"] == "idle"
    assert revoked["refresh_owner_token_hash"] is None
    assert (await client.get("/api/v1/me")).status_code == 401


@pytest.mark.asyncio
async def test_session_service_depends_only_on_secret_backend_port(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    keyring_path: Path,
    idp: FakeIdP,
) -> None:
    """冻结（验收阻断 3，SecretBackend 可替换性）：SessionService 业务层只依赖
    SecretBackend port 与类型化 UoW adapter，不依赖 LocalSecretBackend 的
    concrete-only 签名（external_session）；完整 login + refresh 往返在
    port-only backend 上必须可运行（S4 Vault/KMS adapter 可实现同 port）。
    """
    await _seed_alice_flow()
    app, _, _ = app_and_client
    svc = app.state.session_service
    real_backend = svc._secret_backend
    port_only = PortOnlySecretBackend(real_backend)
    uow = LocalSessionRefreshUnitOfWork(
        session_factory=svc._identity_session_factory, secret_backend=real_backend
    )
    port_service = SessionService(
        session_store=svc._session_store,
        secret_backend=port_only,
        refresh_uow=uow,
        oidc_service=svc._oidc_service,
        identity_session_factory=svc._identity_session_factory,
    )

    auth_url = await port_service.create_login_attempt()
    idp.record_authorization(auth_url)
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    code = idp.issue_code(state)
    session, cookie_token = await port_service.complete_login(code=code, state=state)
    assert session.version == 1

    refreshed = await port_service.refresh_session(session.id, expected_version=1)
    assert refreshed.version == 2
    assert idp.refresh_calls == 1
    payload = await port_service.decrypt_tokens(refreshed)
    assert payload.refresh_token == f"{REFRESH_SENTINEL}.2"
    row = await _session_row(cookie_token)
    assert row is not None
    assert row["revoked_at"] is None
    assert row["version"] == 2


# --------------------------------------------------------------------------- H. 验收修订 5 冻结：expired-calling 条件撤销与 DB 状态不变量


@pytest.mark.asyncio
async def test_expired_calling_revoke_race_preserves_winner_commit(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    keyring_path: Path,
    idp: FakeIdP,
) -> None:
    """冻结（验收修订 5）：expired-calling 撤销竞态——loser 不得撤销 winner 的 v2。

    对抗排列：
    1. winner A 处于 calling（真实 refresh 路径），lease 到期；
    2. loser B 观察到 expired calling，准备 fail-closed revoke；
    3. 在 B 的观察与 revoke 之间，A 成功提交 v2（真实 complete 路径）；
    4. B 必须撤销失败并重读：返回 winner 的新版本 v2，**不得**撤销 v2；
    5. 最终必须保持 version=2、revoked_at=NULL；IdP refresh 调用仍为 1。

    refresh 故障处理不得调用 logout 的「按 session id 单调 revoke」：单调 revoke 会
    无视版本撤销 v2。loser 必须使用 refresh 专用条件撤销（CAS session_id +
    expected_version + calling + observed owner hash + lease 过期），失败后重读分类。
    """
    await _seed_alice_flow()
    app, client, _ = app_and_client
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None
    sid = row["id"]

    second = _new_app(keyring_path, idp)
    try:
        s1 = app.state.session_service
        s2 = second.state.session_service
        a_in_complete = asyncio.Event()
        release_a = asyncio.Event()

        # winner A：调用 IdP 后卡在 complete 之前（停在 calling）
        orig_complete = s1._session_store.complete_refresh

        async def gated_complete(*args: Any, **kwargs: Any) -> Any:
            a_in_complete.set()
            await release_a.wait()
            return await orig_complete(*args, **kwargs)

        s1._session_store.complete_refresh = gated_complete
        a_task = asyncio.create_task(s1.refresh_session(sid, expected_version=1))
        await asyncio.wait_for(a_in_complete.wait(), timeout=15)
        assert idp.refresh_calls == 1, "A 进入 calling 前必须恰好调用一次 IdP"
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            state = await connection.fetchval(
                "SELECT refresh_state FROM auth_sessions WHERE id = $1", sid
            )
            assert state == "calling"
            await connection.execute(
                "UPDATE auth_sessions SET refresh_lease_expires_at = now() - interval '1 second' "
                "WHERE id = $1",
                sid,
            )
        finally:
            await connection.close()

        # loser B：在「观察到 expired calling」与「revoke」之间释放 A 提交 v2
        orig_revoke = s2._session_store.revoke_session
        orig_expired_revoke = getattr(s2._session_store, "revoke_expired_calling", None)

        async def racy_revoke(*args: Any, **kwargs: Any) -> Any:
            release_a.set()
            await a_task
            return await orig_revoke(*args, **kwargs)

        async def racy_expired_revoke(*args: Any, **kwargs: Any) -> Any:
            assert orig_expired_revoke is not None
            release_a.set()
            await a_task
            return await orig_expired_revoke(*args, **kwargs)

        s2._session_store.revoke_session = racy_revoke
        if orig_expired_revoke is not None:
            s2._session_store.revoke_expired_calling = racy_expired_revoke
        try:
            result = await s2.refresh_session(sid, expected_version=1)
        finally:
            s2._session_store.revoke_session = orig_revoke
            if orig_expired_revoke is not None:
                s2._session_store.revoke_expired_calling = orig_expired_revoke
            s1._session_store.complete_refresh = orig_complete

        # B 必须重读并接受 winner 的 v2，不得撤销
        assert result.id == sid
        assert result.version == 2
        assert idp.refresh_calls == 1, "B 是 loser，不得触发第二次 IdP 调用"
        final = await _session_row(cookie)
        assert final is not None
        assert final["version"] == 2
        assert final["revoked_at"] is None
        assert final["refresh_state"] == "idle"
        assert final["refresh_lease_expires_at"] is None
        assert final["refresh_owner_token_hash"] is None
        assert (await client.get("/api/v1/me")).status_code == 200
    finally:
        await second.state.dispose_engines()


@pytest.mark.asyncio
async def test_expired_calling_revoke_race_reclassified_when_other_path_revoked(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
    keyring_path: Path,
    idp: FakeIdP,
) -> None:
    """冻结（验收修订 5）：条件撤销失败后的重读分类——已被其他路径撤销 → SessionRevokedError。

    loser 观察到 expired calling 后、条件撤销执行前，logout 抢先单调撤销（version+1、
    revoked_at 非空）：条件撤销 CAS 失败，重读发现已撤销 → SessionRevokedError，
    不得把已撤销 session 当成 winner 返回。
    """
    await _seed_alice_flow()
    _, client, _ = app_and_client
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None
    sid = row["id"]

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "UPDATE auth_sessions SET refresh_state = 'calling', "
            "refresh_owner_token_hash = $2, "
            "refresh_lease_expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            sid,
            _owner_hash(),
        )
    finally:
        await connection.close()

    second = _new_app(keyring_path, idp)
    try:
        s2 = second.state.session_service
        orig_revoke = s2._session_store.revoke_session
        orig_expired_revoke = getattr(s2._session_store, "revoke_expired_calling", None)

        async def racy_expired_revoke(*args: Any, **kwargs: Any) -> Any:
            # 条件撤销执行前，logout 路径抢先单调撤销（v1 → v2 revoked）
            await orig_revoke(sid, expected_version=1)
            assert orig_expired_revoke is not None
            return await orig_expired_revoke(*args, **kwargs)

        if orig_expired_revoke is not None:
            s2._session_store.revoke_expired_calling = racy_expired_revoke
        try:
            with pytest.raises(SessionRevokedError):
                await s2.refresh_session(sid, expected_version=1)
        finally:
            if orig_expired_revoke is not None:
                s2._session_store.revoke_expired_calling = orig_expired_revoke
    finally:
        await second.state.dispose_engines()

    final = await _session_row(cookie)
    assert final is not None
    assert final["revoked_at"] is not None
    assert final["version"] == 2
    assert (await client.get("/api/v1/me")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refresh_state", "owner_hash", "lease_sql", "revoked_sql", "label"),
    [
        ("idle", _owner_hash(), "NULL", "NULL", "idle + owner hash"),
        ("idle", None, "now() + interval '30 seconds'", "NULL", "idle + lease"),
        ("leased", None, "now() + interval '30 seconds'", "NULL", "leased + null owner"),
        ("calling", None, "now() + interval '30 seconds'", "NULL", "calling + null owner"),
        ("leased", _owner_hash(), "NULL", "NULL", "leased + null lease"),
        ("calling", _owner_hash(), "NULL", "NULL", "calling + null lease"),
        ("idle", _owner_hash(), "NULL", "now()", "revoked + owner"),
        ("idle", None, "now() + interval '30 seconds'", "now()", "revoked + lease"),
    ],
)
async def test_db_rejects_violating_refresh_state_invariants(
    migrated_database: None,
    refresh_state: str,
    owner_hash: str | None,
    lease_sql: str,
    revoked_sql: str,
    label: str,
) -> None:
    """冻结（验收修订 5）：raw SQL 反例——非法持久化状态必须被 DB CHECK 拒绝。

    统一不变量（0004/model/domain）：idle ⟺ owner/lease 皆 NULL；leased/calling ⟹
    owner/lease 皆非 NULL；revoked 必须 idle 且 owner/lease 全空。任何绕过应用层
    的非法 INSERT 必须收到 CheckViolationError。
    """
    principal = await _seed_principal("invariant-probe")
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        session_id = uuid4()
        try:
            await connection.execute(
                "INSERT INTO auth_sessions (id, cookie_token_hash, principal_id, issuer, "
                "subject, encrypted_token_ref, csrf_hash, expires_at, idle_expires_at, "
                "created_at, updated_at, revoked_at, version, refresh_state, "
                "refresh_lease_expires_at, refresh_owner_token_hash, schema_version) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7, now() + interval '8 hours', "
                f"now() + interval '30 minutes', now(), now(), {revoked_sql}, 1, $8, "
                f"{lease_sql}, $9, 1)",
                session_id,
                hashlib.sha256(os.urandom(16)).hexdigest(),
                principal,
                ISSUER,
                "invariant-probe",
                str(uuid4()),
                hashlib.sha256(os.urandom(16)).hexdigest(),
                refresh_state,
                owner_hash,
            )
        except asyncpg.CheckViolationError:
            return
        pytest.fail(f"非法状态未被 DB CHECK 拒绝: {label}")
    finally:
        await connection.close()


def test_to_auth_session_fails_closed_on_illegal_persisted_state() -> None:
    """冻结（验收修订 5）：投影层不得静默掩盖非法持久化状态。

    DB CHECK 是最后防线；_to_auth_session 也必须 fail closed——idle 残留 owner
    hash/lease、leased/calling 缺 owner/lease、revoked 携带 owner/lease 一律抛
    SessionRevokedError，不得投影成看似合法的 AuthSession（旧实现把 idle 残留 owner
    hash 静默忽略）。
    """
    from zhiwei.identity.sessions import _to_auth_session
    from zhiwei.persistence.models import AuthSession as AuthSessionRow

    now = datetime.now(UTC)
    owner_hash = _owner_hash()

    def row(**overrides: Any) -> AuthSessionRow:
        values: dict[str, Any] = {
            "id": uuid4(),
            "cookie_token_hash": hashlib.sha256(b"cookie-token").hexdigest(),
            "principal_id": uuid4(),
            "issuer": ISSUER,
            "subject": "alice-oidc",
            "encrypted_token_ref": str(uuid4()),
            "csrf_hash": hashlib.sha256(b"csrf-secret").hexdigest(),
            "expires_at": now + timedelta(hours=8),
            "idle_expires_at": now + timedelta(minutes=30),
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
            "version": 1,
            "refresh_state": "idle",
            "refresh_lease_expires_at": None,
            "refresh_owner_token_hash": None,
            "schema_version": 1,
        }
        values.update(overrides)
        return AuthSessionRow(**values)

    cases = [
        ("idle + owner hash", {"refresh_owner_token_hash": owner_hash}),
        ("idle + lease", {"refresh_lease_expires_at": now + timedelta(seconds=30)}),
        (
            "leased + null owner",
            {
                "refresh_state": "leased",
                "refresh_lease_expires_at": now + timedelta(seconds=30),
            },
        ),
        (
            "calling + null owner",
            {
                "refresh_state": "calling",
                "refresh_lease_expires_at": now + timedelta(seconds=30),
            },
        ),
        (
            "leased + null lease",
            {"refresh_state": "leased", "refresh_owner_token_hash": owner_hash},
        ),
        (
            "calling + null lease",
            {"refresh_state": "calling", "refresh_owner_token_hash": owner_hash},
        ),
        ("revoked + owner", {"revoked_at": now, "refresh_owner_token_hash": owner_hash}),
        (
            "revoked + lease",
            {"revoked_at": now, "refresh_lease_expires_at": now + timedelta(seconds=30)},
        ),
        (
            "revoked + leased state",
            {
                "revoked_at": now,
                "refresh_state": "leased",
                "refresh_owner_token_hash": owner_hash,
                "refresh_lease_expires_at": now + timedelta(seconds=30),
            },
        ),
    ]
    for _label, overrides in cases:
        with pytest.raises(SessionRevokedError, match="illegal persisted session state"):
            _to_auth_session(row(**overrides))


def test_migration_0003_to_0004_normalizes_legacy_refreshing_fail_closed(
    migrated_database: None,
) -> None:
    """冻结（验收修订 5）：0003→0004 升级必须归一化 legacy 'refreshing' 行再建新约束。

    - downgrade 到 0003，seed 合法的 refresh_state='refreshing' 行（0003 语义：
      refreshing = 刷新在途，lease 非空）；
    - upgrade 到 head 必须成功（旧实现直接建新 CHECK 会因 refreshing 行失败）；
    - 旧状态无法判断 IdP 是否已调用 → fail closed：revoked_at 非空、state=idle、
      lease/owner 均为空、version 单调递增（seeded 1 → 2）；
    - 绝不把不确定的 legacy refreshing 会话恢复为 active；idle / revoked 行保持合法。

    同步测试：alembic env.py 在模块层 asyncio.run()，不能在已运行的事件循环里调用
    command.downgrade/upgrade（与 migrated_database fixture 同一模式）。
    """
    config = _alembic_config()
    command.downgrade(config, "0003_auth_sessions")
    seeded: list[Any] = []
    try:
        asyncio.run(_seed_legacy_refreshing_rows(seeded))
        # 升级必须成功（旧实现会在创建新 CHECK 时因 refreshing 行失败）
        command.upgrade(config, "head")
        asyncio.run(_verify_0004_normalized_rows(seeded))
    finally:
        # 无论测试结果如何都恢复环境：删除 seed 行并把数据库恢复到 head
        asyncio.run(_delete_seeded_sessions(seeded))
        command.upgrade(config, "head")
