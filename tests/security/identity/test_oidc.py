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
- 多 replica refresh：数据库 CAS + 有界 lease 竞争，恰好一次 IdP refresh 调用，输家读取
  winner 的新版本；invalid_grant → 本地 revoke；winner 判定只用数据库时钟（应用/DB 时钟
  偏差不得把并发 winner 误判为 stale）；输家等待上界 = lease 剩余（不是固定 2 秒）；废弃
  lease（过期仍停在 refreshing）由下一位调用方接管完成或本地 revoke，不得永久卡死；
  envelope 改写与 session 完成处于同一原子边界（中途失败不得留下 AAD/session version 不一致）；
- logout 单调安全：同一 session 的 refresh 不得使撤销失效；API 只在服务端撤销确认后返回
  204，本地撤销失败 fail closed，不得假装成功；
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
import httpx
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
from zhiwei.identity import sessions as sessions_module
from zhiwei.identity.sessions import SessionConflictError, SessionRevokedError

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
    }


@pytest_asyncio.fixture(loop_scope="function")
async def app_and_client(
    keyring_path: Path,
    idp: FakeIdP,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, FakeIdP]]:
    app = create_app(
        load_settings(_settings(keyring_path)),
        oidc_http_client=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler), timeout=5.0),
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
            "refresh_lease_expires_at FROM auth_sessions WHERE cookie_token_hash = $1",
            token_hash,
        )
        return dict(row) if row else None
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
            "UPDATE auth_sessions SET refresh_state = 'refreshing', "
            "refresh_lease_expires_at = now() + interval '30 seconds' WHERE id = $1",
            row["id"],
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
        # 先让 loser 处于 lease 竞争失败路径：row 停在 refreshing（winner 在途）
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            await connection.execute(
                "UPDATE auth_sessions SET refresh_state = 'refreshing', "
                "refresh_lease_expires_at = now() + interval '30 seconds' "
                "WHERE id = $1",
                row["id"],
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
                        "refresh_state = 'idle', refresh_lease_expires_at = NULL "
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
        await _wait_for_refresh_state(row["id"], "refreshing")
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
    """冻结（验收阻断 2）：lease 过期且行停在 refreshing（winner 崩溃遗留）时，
    下一位调用方必须接管 ownership 完成刷新（数据库侧 attempt/lease ownership），
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
            "UPDATE auth_sessions SET refresh_state = 'refreshing', "
            "refresh_lease_expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            row["id"],
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
async def test_refresh_envelope_and_session_complete_atomically(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP]
) -> None:
    """冻结（验收阻断 2）：envelope 改写与 session 完成处于同一原子边界。

    envelope put 与 complete_refresh CAS 分属两个事务时，进程在两者之间失败会留下
    AAD/session version 不一致：旧 envelope 必须仍可用当前 session version 的 AAD
    解密，重试刷新必须成功；失败不得破坏既有 session 的可解性。
    """
    app, client, idp = app_and_client
    await _seed_alice_flow()
    cookie = await _perform_login(client, idp)
    row = await _session_row(cookie)
    assert row is not None

    store = app.state.session_service._session_store
    orig_complete = store.complete_refresh
    crashed = {"done": False}

    async def crash_after_envelope(*args: Any, **kwargs: Any) -> Any:
        if not crashed["done"]:
            crashed["done"] = True
            raise SessionConflictError(
                "simulated process death between envelope put and session CAS"
            )
        return await orig_complete(*args, **kwargs)

    store.complete_refresh = crash_after_envelope
    try:
        with pytest.raises(SessionConflictError):
            await app.state.session_service.refresh_session(
                row["id"], expected_version=1
            )
    finally:
        store.complete_refresh = orig_complete

    # 失败后 session 仍 v1 且未 revoke；旧 envelope 必须仍可用 v1 AAD 解密
    after = await _session_row(cookie)
    assert after is not None
    assert after["version"] == 1
    assert after["revoked_at"] is None
    session = await app.state.session_service.authenticate_cookie(cookie)
    assert session is not None
    payload = await app.state.session_service.decrypt_tokens(session)
    assert payload.refresh_token == f"{REFRESH_SENTINEL}.1"

    # 重试刷新必须成功 → v2，envelope 与 v2 AAD 一致
    refreshed = await app.state.session_service.refresh_session(
        row["id"], expected_version=1
    )
    assert refreshed.version == 2
    final = await _session_row(cookie)
    assert final is not None
    assert final["version"] == 2
    assert final["refresh_state"] == "idle"
    assert (await client.get("/api/v1/me")).status_code == 200
