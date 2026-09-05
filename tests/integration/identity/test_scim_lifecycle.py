"""S1-T5 RED：SCIM 2.0 子集与 membership 生命周期（真实 DB + create_app + 真实登录）。

冻结契约（docs/handoffs/s1-t5-design.md，以冻结契约为准）：
- 子集：User create/update/disable + 重复 external identity 409 + GET by id；
  Group create + member reconciliation（PUT replace 双向 diff）+ 重复 409；
  显式 unsupported → 501 + RFC 7644 §3.12 错误体；
- 认证：OIDC BFF 会话 + org/manage 矩阵；读写统一经 policy gate（ORG/MANAGE）；
  allowed 读不写审计、denied 读/写 denied 审计；
- issuer = ZHIWEI_OIDC_ISSUER（部署期固定）；externalId ≡ subject、externalId ≡
  displayName（group name）；
- disable：阻断新登录（403）与既有 session 下次请求（401）与新 command
  （disabled 成员入组 400 invalidValue）；不删除历史 actor 引用（行存活断言）；
  re-enable 对称恢复；
- 幂等：reconcile 重复 payload 零副作用（不写 audit/outbox、无 INSERT/DELETE）；
  并发同键双 POST → 一方 201 一方 409，败方 identity 事务整体回滚（零残留）；
- 审计三类 metadata 与 T4 语义逐字一致（allowed 真实决策 / denied 真实 deny
  决策 / failed 双 NULL + 映射码）。

RED 状态：create_app 尚未注册 scim router——/scim/v2/* 全部返回真实 404，与断言
201/200/501/400/401/403/404 冲突。本文件**不 import zhiwei.api.scim /
zhiwei.identity.scim**（只 import create_app 等既有模块），确保 RED 失败点落在真实
HTTP 行为而非 ImportError。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
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
OPA_BASE_URL = "http://opa.test:8181"
REAL_OPA_BASE_URL = "http://127.0.0.1:8181"

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_jwt(claims: dict[str, Any], key: rsa.RSAPrivateKey) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": "test-kid"}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
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
    """本地假 IdP：discovery / JWKS / authorize / token / revoke 全走 MockTransport。

    subject 可经 issue_code 的 id_token_overrides 覆盖（同 IdP 多主体登录）。
    """

    def __init__(self, key: rsa.RSAPrivateKey, subject: str) -> None:
        self.key = key
        self.subject = subject
        self.authorizations: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, str] = {}
        self.token_issuance: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, str] = {}

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
                if token not in self.refresh_tokens:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                refresh_token = f"{REFRESH_SENTINEL}.2"
                self.refresh_tokens[refresh_token] = self.subject
                return httpx.Response(
                    200,
                    json={
                        "access_token": ACCESS_SENTINEL,
                        "refresh_token": refresh_token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if request.url.path == "/revoke":
            return httpx.Response(200, content=b"")
        raise AssertionError(f"unexpected IdP request: {request.url}")


class FakeOPA:
    """本地假 OPA：响应严格符合 T3 client 校验；records inputs 供 policy input 断言。"""

    ALLOW_DECISION_ID = "decision-allow-1"
    ALLOW_REVISION = "bundle-rev-1"
    ALLOW_REASON = "allow:matrix"
    DENY_DECISION_ID = "decision-deny-1"
    DENY_REVISION = "bundle-rev-1"
    DENY_REASON = "deny:default_deny:no_rule_matched"

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.deny = False
        self.fail: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.inputs.append(json.loads(request.read())["input"])
        if self.fail is not None:
            raise self.fail
        if self.deny:
            return httpx.Response(
                200,
                json={
                    "decision_id": self.DENY_DECISION_ID,
                    "result": {"allow": False, "reason": self.DENY_REASON},
                    "provenance": {
                        "version": "1.19.0",
                        "bundles": {"/bundle.tar.gz": {"revision": self.DENY_REVISION}},
                    },
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "decision_id": self.ALLOW_DECISION_ID,
                "result": {"allow": True, "reason": self.ALLOW_REASON},
                "provenance": {
                    "version": "1.19.0",
                    "bundles": {"/bundle.tar.gz": {"revision": self.ALLOW_REVISION}},
                },
            },
            request=request,
        )


# --------------------------------------------------------------------------- fixtures


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    config.attributes["database_url"] = ADMIN_DSN.replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    return config


async def _assert_safe_test_database(dsn: str) -> None:
    url = make_url(dsn)
    if url.database != "zhiwei_test" or url.username != "zhiwei_migrator":
        raise RuntimeError("destructive migration tests require the dedicated zhiwei_test database")
    connection = await asyncpg.connect(dsn)
    try:
        database, user = await connection.fetchrow("SELECT current_database(), current_user")
        if database != "zhiwei_test" or user != "zhiwei_migrator":
            raise RuntimeError(
                "connected database identity is not the dedicated migration test target"
            )
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[None]:
    """从 base 重建到 head（GREEN 后含 0009）；供本文件所有用例使用。"""
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
def opa() -> FakeOPA:
    return FakeOPA()


@pytest.fixture
def keyring_path(tmp_path: Path) -> Path:
    path = tmp_path / "master.key"
    material = hashlib.sha256(b"ZW_TEST_MASTER_KEY_D0E6").digest()
    path.write_text(f"k1={base64.b64encode(material).decode('ascii')}\n", encoding="utf-8")
    return path


def _settings(keyring_path: Path, *, opa_base_url: str = OPA_BASE_URL) -> dict[str, str]:
    return {
        "ZHIWEI_PROFILE": "test",
        "ZHIWEI_DATABASE_URL": APP_DSN,
        "ZHIWEI_IDENTITY_DATABASE_URL": IDENTITY_DSN,
        "ZHIWEI_OIDC_ISSUER": ISSUER,
        "ZHIWEI_OIDC_CLIENT_ID": CLIENT_ID,
        "ZHIWEI_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "ZHIWEI_OIDC_REDIRECT_URI": REDIRECT_URI,
        "ZHIWEI_IDENTITY_MASTER_KEY_FILE": str(keyring_path),
        "ZHIWEI_OPA_BASE_URL": opa_base_url,
    }


async def _make_app(
    keyring_path: Path,
    idp: FakeIdP,
    *,
    opa: FakeOPA | None,
) -> FastAPI:
    if opa is not None:
        return create_app(
            load_settings(_settings(keyring_path)),
            oidc_http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(idp.handler), timeout=5.0
            ),
            policy_http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(opa.handler), timeout=5.0
            ),
        )
    # 真实 OPA 路径必须指向 opa_service fixture 拉起的 sidecar（ADR-013 反例 7：
    # 此前沿用 FakeOPA 场景的 opa.test 假地址，「真实栈」测试从未连上真实 OPA）。
    return create_app(
        load_settings(_settings(keyring_path, opa_base_url=REAL_OPA_BASE_URL)),
        oidc_http_client=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler), timeout=5.0),
    )


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def app_and_client(
    keyring_path: Path,
    idp: FakeIdP,
    opa: FakeOPA,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]]:
    app = await _make_app(keyring_path, idp, opa=opa)
    async for client in _client(app):
        yield app, client, idp, opa
    await app.state.dispose_engines()


# --------------------------------------------------------------------------- docker helpers（slow）


def _wait_healthy(deadline: float = 120.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        try:
            resp = httpx.get(f"{REAL_OPA_BASE_URL}/health?bundles", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("opa 服务未在期限内通过 /health?bundles")


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*("docker", "compose", "-f", "deploy/compose/compose.test.yaml"), *args],
        capture_output=True,
        text=True,
        timeout=300,
        env=env or os.environ,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture()
def opa_service() -> Iterator[None]:
    """确保 opa 容器以当前仓库 policies 重建并就绪；无 docker 时跳过（slow 显式）。"""
    if shutil.which("docker") is None:
        pytest.skip("环境守卫：无 docker 无法执行真实 OPA 纵切（slow 显式运行）")
    _run("rm", "-sf", "opa")
    up = _run("up", "-d", "--force-recreate", "--wait", "opa")
    assert up.returncode == 0, f"opa 启动失败:\n{up.stdout}\n{up.stderr}"
    _wait_healthy()
    yield
    _run("rm", "-sf", "opa")
    up = _run("up", "-d", "--wait", "opa")
    assert up.returncode == 0, f"opa 还原失败:\n{up.stdout}\n{up.stderr}"
    _wait_healthy()


@pytest_asyncio.fixture(loop_scope="function")
async def app_and_client_real_opa(
    keyring_path: Path,
    idp: FakeIdP,
    opa_service: None,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, FakeIdP]]:
    app = await _make_app(keyring_path, idp, opa=None)
    async for client in _client(app):
        yield app, client, idp
    await app.state.dispose_engines()


# --------------------------------------------------------------------------- seed / helpers


async def _seed_principal(subject: str, *, kind: str = "user", status: str = "active") -> UUID:
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


async def _seed_membership(principal_id: UUID, organization_id: UUID, roles: list[str]) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO memberships (principal_id, organization_id, role_bindings) "
            "VALUES ($1, $2, $3::jsonb) ON CONFLICT DO NOTHING",
            principal_id,
            organization_id,
            json.dumps(roles),
        )
    finally:
        await connection.close()


async def _seed_workspace_membership(
    principal_id: UUID, organization_id: UUID, workspace_id: UUID, roles: list[str]
) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO workspace_memberships "
            "(principal_id, organization_id, workspace_id, role_bindings) "
            "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT DO NOTHING",
            principal_id,
            organization_id,
            workspace_id,
            json.dumps(roles),
        )
    finally:
        await connection.close()


async def _reset_alice() -> UUID:
    """清空 alice-oidc 的 memberships / workspace_memberships，返回 principal id。"""
    principal = await _seed_principal("alice-oidc")
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute("DELETE FROM memberships WHERE principal_id = $1", principal)
        await connection.execute(
            "DELETE FROM workspace_memberships WHERE principal_id = $1", principal
        )
    finally:
        await connection.close()
    return principal


async def _perform_login(
    client: httpx.AsyncClient, idp: FakeIdP, *, subject: str | None = None
) -> str:
    login = await client.get("/auth/login")
    assert login.status_code == 302
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state, id_token_overrides={"sub": subject} if subject else None)
    callback = await client.get(f"/auth/callback?code={code}&state={state}")
    assert callback.status_code == 302
    cookie = client.cookies.get(COOKIE)
    assert cookie, "callback 必须签发 session cookie"
    return cookie


async def _login_attempt(client: httpx.AsyncClient, idp: FakeIdP, *, subject: str) -> int:
    """发起登录并返回 callback 状态码（不断言 302，供 disabled 主体 403 断言）。"""
    login = await client.get("/auth/login")
    assert login.status_code == 302
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state, id_token_overrides={"sub": subject})
    callback = await client.get(f"/auth/callback?code={code}&state={state}")
    return callback.status_code


async def _csrf_token(client: httpx.AsyncClient) -> str:
    me = await client.get("/api/v1/me")
    assert me.status_code == 200
    return me.json()["csrf_token"]


def _scim_headers(
    csrf_token: str,
    *,
    organization_id: UUID,
    workspace_id: UUID | None = None,
) -> dict[str, str]:
    headers = {
        "X-CSRF-Token": csrf_token,
        "Origin": "https://test",
        "X-ZhiWei-Organization": str(organization_id),
    }
    if workspace_id is not None:
        headers["X-ZhiWei-Workspace"] = str(workspace_id)
    return headers


async def _read_audit_rows(
    organization_id: UUID, workspace_id: UUID | None = None
) -> list[dict[str, Any]]:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        if workspace_id is None:
            rows = await connection.fetch(
                "SELECT * FROM audit_events WHERE organization_id = $1 "
                "AND workspace_id IS NULL ORDER BY created_at",
                organization_id,
            )
        else:
            rows = await connection.fetch(
                "SELECT * FROM audit_events WHERE organization_id = $1 "
                "AND workspace_id = $2 ORDER BY created_at",
                organization_id,
                workspace_id,
            )
    finally:
        await connection.close()
    return [dict(row) for row in rows]


async def _count(table: str, *, where: str, params: list[Any]) -> int:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        return await connection.fetchval(f"SELECT count(*) FROM {table} WHERE {where}", *params)
    finally:
        await connection.close()


def _assert_scim_error(response, status: int, *, scim_type: str | None = None) -> dict:
    assert response.status_code == status
    error = response.json()
    assert error["schemas"] == [ERROR_SCHEMA]
    assert error["status"] == str(status)
    if scim_type is not None:
        assert error["scimType"] == scim_type
    assert error["detail"]
    return error


def _user_body(external_id: str) -> dict:
    return {
        "schemas": [USER_SCHEMA],
        "externalId": external_id,
        "userName": external_id,
    }


def _group_body(name: str) -> dict:
    return {
        "schemas": [GROUP_SCHEMA],
        "externalId": name,
        "displayName": name,
    }


def _patch_active(value: bool) -> dict:
    return {
        "schemas": [PATCH_SCHEMA],
        "Operations": [{"op": "replace", "path": "active", "value": value}],
    }


async def _create_user(
    client: httpx.AsyncClient,
    csrf: str,
    org: UUID,
    external_id: str,
) -> dict:
    response = await client.post(
        "/scim/v2/Users",
        headers=_scim_headers(csrf, organization_id=org),
        json=_user_body(external_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_group(
    client: httpx.AsyncClient,
    csrf: str,
    org: UUID,
    ws: UUID,
    name: str,
) -> dict:
    response = await client.post(
        "/scim/v2/Groups",
        headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
        json=_group_body(name),
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- User 生命周期


@pytest.mark.asyncio
async def test_scim_user_create_writes_principal_identity_and_audit(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    await _seed_membership(owner_id, org, ["org_owner"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    response = await client.post(
        "/scim/v2/Users",
        headers=_scim_headers(csrf, organization_id=org),
        json=_user_body("u1"),
    )
    assert response.status_code == 201, response.text
    assert response.headers["location"].startswith("/scim/v2/Users/")
    body = response.json()
    assert body["schemas"] == [USER_SCHEMA]
    assert body["externalId"] == "u1"
    assert body["userName"] == "u1"
    assert body["active"] is True
    user_id = UUID(body["id"])
    assert body["meta"]["resourceType"] == "User"
    assert body["meta"]["location"] == f"/scim/v2/Users/{user_id}"

    assert await _count("principals", where="id = $1", params=[user_id]) == 1
    assert await _count(
        "external_identities", where="issuer = $1 AND subject = $2", params=[ISSUER, "u1"]
    ) == 1
    assert await _count(
        "external_identities",
        where="principal_id = $1 AND issuer = $2 AND subject = $3",
        params=[user_id, ISSUER, "u1"],
    ) == 1

    audit = await _read_audit_rows(org)
    created = [row for row in audit if row["action"] == "scim.user.create"]
    assert len(created) == 1
    row = created[0]
    assert row["result"] == "allowed"
    assert row["resource_type"] == "principal"
    assert row["resource_id"] == user_id
    assert row["resource_version"] == 1
    assert row["actor_ref"] == f"user:{owner_id}"
    assert row["effective_identity_ref"] == f"user:{owner_id}"
    assert row["decision_id"] == opa.ALLOW_DECISION_ID
    assert row["policy_revision"] == opa.ALLOW_REVISION
    assert row["decision_reason"] == opa.ALLOW_REASON
    assert row["request_id"]
    assert row["trace_id"]
    assert _SHA256_DIGEST_RE.fullmatch(row["payload_digest"])

    assert await _count(
        "outbox", where="topic = 'audit.decision' AND organization_id = $1", params=[org]
    ) == 1


@pytest.mark.asyncio
async def test_scim_user_duplicate_external_id_conflict_409(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, _opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    await _seed_membership(owner_id, org, ["org_owner"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    first = await client.post(
        "/scim/v2/Users",
        headers=_scim_headers(csrf, organization_id=org),
        json=_user_body("dup"),
    )
    assert first.status_code == 201

    response = await client.post(
        "/scim/v2/Users",
        headers=_scim_headers(csrf, organization_id=org),
        json=_user_body("dup"),
    )
    _assert_scim_error(response, 409, scim_type="uniqueness")

    assert await _count(
        "external_identities", where="issuer = $1 AND subject = $2", params=[ISSUER, "dup"]
    ) == 1
    assert await _count(
        "principals",
        where="id IN (SELECT principal_id FROM external_identities "
        "WHERE issuer = $1 AND subject = $2)",
        params=[ISSUER, "dup"],
    ) == 1

    audit = await _read_audit_rows(org)
    failed = [
        row
        for row in audit
        if row["action"] == "scim.user.create" and row["result"] == "failed"
    ]
    assert len(failed) == 1
    assert failed[0]["decision_id"] is None
    assert failed[0]["policy_revision"] is None
    assert failed[0]["decision_reason"] == "business_rejection"


@pytest.mark.asyncio
async def test_scim_user_concurrent_duplicate_external_id_one_wins(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, _opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    await _seed_membership(owner_id, org, ["org_owner"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    async def post() -> int:
        response = await client.post(
            "/scim/v2/Users",
            headers=_scim_headers(csrf, organization_id=org),
            json=_user_body("race"),
        )
        return response.status_code

    codes = await asyncio.gather(post(), post())
    assert sorted(codes) == [201, 409]

    assert await _count(
        "external_identities", where="issuer = $1 AND subject = $2", params=[ISSUER, "race"]
    ) == 1
    assert await _count(
        "principals",
        where="id IN (SELECT principal_id FROM external_identities "
        "WHERE issuer = $1 AND subject = $2)",
        params=[ISSUER, "race"],
    ) == 1


@pytest.mark.asyncio
async def test_scim_user_get_by_id_and_unknown_404(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, _opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    await _seed_membership(owner_id, org, ["org_owner"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    created = await _create_user(client, csrf, org, "getme")
    user_id = UUID(created["id"])

    response = await client.get(
        f"/scim/v2/Users/{user_id}",
        headers=_scim_headers(csrf, organization_id=org),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["externalId"] == "getme"
    assert body["userName"] == "getme"
    assert body["active"] is True

    _assert_scim_error(
        await client.get(
            f"/scim/v2/Users/{uuid4()}",
            headers=_scim_headers(csrf, organization_id=org),
        ),
        404,
    )


@pytest.mark.asyncio
async def test_scim_user_disable_blocks_sessions_and_commands_and_reenable(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    app, client, idp, opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    ws = await _seed_workspace(org)
    await _seed_membership(owner_id, org, ["org_owner"])
    await _seed_workspace_membership(owner_id, org, ws, ["workspace_admin"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    victim = await _create_user(client, csrf, org, "victim")
    victim_id = UUID(victim["id"])
    await _seed_membership(victim_id, org, ["member"])

    # victim 登录（独立 cookie jar）
    async for victim_client in _client(app):
        await _perform_login(victim_client, idp, subject="victim")
        me = await victim_client.get("/api/v1/me")
        assert me.status_code == 200

        # owner disable victim
        response = await client.patch(
            f"/scim/v2/Users/{victim_id}",
            headers=_scim_headers(csrf, organization_id=org),
            json=_patch_active(False),
        )
        assert response.status_code == 200, response.text
        assert response.json()["active"] is False
        assert await _count("principals", where="id = $1", params=[victim_id]) == 1
        assert await _count(
            "principals", where="id = $1 AND status = 'disabled'", params=[victim_id]
        ) == 1
        # 历史 actor 引用存活：external identity / membership 不被删除
        assert await _count(
            "external_identities", where="principal_id = $1", params=[victim_id]
        ) == 1
        assert await _count("memberships", where="principal_id = $1", params=[victim_id]) == 1

        disable_audit = [
            row
            for row in await _read_audit_rows(org)
            if row["action"] == "scim.user.disable" and row["result"] == "allowed"
        ]
        assert len(disable_audit) == 1
        assert disable_audit[0]["resource_id"] == victim_id
        assert disable_audit[0]["decision_id"] == opa.ALLOW_DECISION_ID

        # 既有 session 下次请求 → 401
        me = await victim_client.get("/api/v1/me")
        assert me.status_code == 401

        # 新登录 → 403 login failed
        async for fresh_client in _client(app):
            assert await _login_attempt(fresh_client, idp, subject="victim") == 403

        # 新 command：disabled 成员入组 → 400 invalidValue（SCIM reconcile）
        group = await _create_group(client, csrf, org, ws, "eng")
        group_id = group["id"]
        _assert_scim_error(
            await client.put(
                f"/scim/v2/Groups/{group_id}",
                headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
                json={
                    "schemas": [GROUP_SCHEMA],
                    "externalId": "eng",
                    "displayName": "eng",
                    "members": [{"value": str(victim_id)}],
                },
            ),
            400,
            scim_type="invalidValue",
        )

        # re-enable：登录恢复
        response = await client.put(
            f"/scim/v2/Users/{victim_id}",
            headers=_scim_headers(csrf, organization_id=org),
            json={"schemas": [USER_SCHEMA], "userName": "victim", "active": True},
        )
        assert response.status_code == 200
        assert response.json()["active"] is True
        enable_audit = [
            row
            for row in await _read_audit_rows(org)
            if row["action"] == "scim.user.enable" and row["result"] == "allowed"
        ]
        assert len(enable_audit) == 1

        async for recovered_client in _client(app):
            assert await _login_attempt(recovered_client, idp, subject="victim") == 302
        break


@pytest.mark.asyncio
async def test_scim_user_put_username_mismatch_is_400(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, _opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    await _seed_membership(owner_id, org, ["org_owner"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    created = await _create_user(client, csrf, org, "fixed")
    user_id = created["id"]
    _assert_scim_error(
        await client.put(
            f"/scim/v2/Users/{user_id}",
            headers=_scim_headers(csrf, organization_id=org),
            json={"schemas": [USER_SCHEMA], "userName": "other", "active": True},
        ),
        400,
        scim_type="mutability",
    )


# --------------------------------------------------------------------------- Group 生命周期


@pytest.mark.asyncio
async def test_scim_group_reconciliation_idempotent_and_bidirectional(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    ws = await _seed_workspace(org)
    await _seed_membership(owner_id, org, ["org_owner"])
    await _seed_workspace_membership(owner_id, org, ws, ["workspace_admin"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    user_a = await _create_user(client, csrf, org, "a")
    user_b = await _create_user(client, csrf, org, "b")
    a_id, b_id = UUID(user_a["id"]), UUID(user_b["id"])

    group = await _create_group(client, csrf, org, ws, "eng")
    group_id = UUID(group["id"])
    assert group["externalId"] == "eng"
    assert group["displayName"] == "eng"
    assert group["members"] == []
    assert group["meta"]["resourceType"] == "Group"

    def reconcile_body(member_ids: list[UUID]) -> dict:
        return {
            "schemas": [GROUP_SCHEMA],
            "externalId": "eng",
            "displayName": "eng",
            "members": [{"value": str(mid)} for mid in member_ids],
        }

    async def reconcile(member_ids: list[UUID]) -> Any:
        return await client.put(
            f"/scim/v2/Groups/{group_id}",
            headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
            json=reconcile_body(member_ids),
        )

    response = await reconcile([a_id])
    assert response.status_code == 200
    assert [m["value"] for m in response.json()["members"]] == [str(a_id)]
    assert await _count("group_members", where="group_id = $1", params=[group_id]) == 1

    # 幂等重放：同 payload 零副作用（audit/outbox/group_members 全部不变）
    before = await _read_audit_rows(org, ws)
    response = await reconcile([a_id])
    assert response.status_code == 200
    assert await _count("group_members", where="group_id = $1", params=[group_id]) == 1
    assert await _read_audit_rows(org, ws) == before

    # 增员
    response = await reconcile([a_id, b_id])
    assert response.status_code == 200
    assert await _count("group_members", where="group_id = $1", params=[group_id]) == 2
    reconcile_audit = [
        row
        for row in await _read_audit_rows(org, ws)
        if row["action"] == "scim.group.reconcile" and row["result"] == "allowed"
    ]
    # 设计 §9：PUT /Groups 成员 diff 非空（changed）→ scim.group.reconcile
    # allowed 审计。首次 [a]（空→{a}）与 [a,b]（{a}→{a,b}）diff 均非空，各写一条。
    assert len(reconcile_audit) == 2
    assert reconcile_audit[0]["resource_id"] == group_id
    assert reconcile_audit[0]["decision_id"] == opa.ALLOW_DECISION_ID

    # 删员（0009 DELETE 授权实证）
    response = await reconcile([a_id])
    assert response.status_code == 200
    assert await _count("group_members", where="group_id = $1", params=[group_id]) == 1

    # 再幂等
    before = await _read_audit_rows(org, ws)
    response = await reconcile([a_id])
    assert response.status_code == 200
    assert await _count("group_members", where="group_id = $1", params=[group_id]) == 1
    assert await _read_audit_rows(org, ws) == before

    # GET by id
    response = await client.get(
        f"/scim/v2/Groups/{group_id}",
        headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
    )
    assert response.status_code == 200
    assert [m["value"] for m in response.json()["members"]] == [str(a_id)]

    # GET list（分页）
    response = await client.get(
        "/scim/v2/Groups?startIndex=1&count=10",
        headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
    )
    assert response.status_code == 200
    listed = response.json()
    assert listed["schemas"] == [LIST_SCHEMA]
    assert listed["totalResults"] == 1
    assert listed["startIndex"] == 1
    assert listed["itemsPerPage"] == 1
    assert [r["id"] for r in listed["Resources"]] == [str(group_id)]


@pytest.mark.asyncio
async def test_scim_group_duplicate_external_id_conflict_409(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, _opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    ws = await _seed_workspace(org)
    await _seed_membership(owner_id, org, ["org_owner"])
    await _seed_workspace_membership(owner_id, org, ws, ["workspace_admin"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    first = await _create_group(client, csrf, org, ws, "dup")
    assert first["externalId"] == "dup"

    response = await client.post(
        "/scim/v2/Groups",
        headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
        json=_group_body("dup"),
    )
    _assert_scim_error(response, 409, scim_type="uniqueness")
    assert await _count(
        "groups",
        where="organization_id = $1 AND workspace_id = $2 AND name = 'dup'",
        params=[org, ws],
    ) == 1
    failed = [
        row
        for row in await _read_audit_rows(org, ws)
        if row["action"] == "scim.group.create" and row["result"] == "failed"
    ]
    assert len(failed) == 1
    assert failed[0]["decision_id"] is None
    assert failed[0]["policy_revision"] is None
    assert failed[0]["decision_reason"] == "name_conflict"


@pytest.mark.asyncio
async def test_scim_group_unsupported_operations_and_validation(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, _opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    ws = await _seed_workspace(org)
    await _seed_membership(owner_id, org, ["org_owner"])
    await _seed_workspace_membership(owner_id, org, ws, ["workspace_admin"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    created = await _create_group(client, csrf, org, ws, "g")
    group_id = created["id"]

    _assert_scim_error(
        await client.patch(
            f"/scim/v2/Groups/{group_id}",
            headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "add", "path": "members", "value": []}],
            },
        ),
        501,
    )
    _assert_scim_error(
        await client.request(
            "DELETE",
            f"/scim/v2/Groups/{group_id}",
            headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
        ),
        501,
    )
    _assert_scim_error(
        await client.post(
            "/scim/v2/Groups",
            headers=_scim_headers(csrf, organization_id=org, workspace_id=ws),
            json={
                "schemas": [GROUP_SCHEMA],
                "externalId": "x",
                "displayName": "y",
            },
        ),
        400,
        scim_type="invalidValue",
    )


@pytest.mark.asyncio
async def test_scim_group_cross_tenant_idor_404(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    app, client, idp, _opa = app_and_client
    owner_a = await _reset_alice()
    org_a = await _seed_org()
    ws_a = await _seed_workspace(org_a, name="a")
    await _seed_membership(owner_a, org_a, ["org_owner"])
    await _seed_workspace_membership(owner_a, org_a, ws_a, ["workspace_admin"])
    await _perform_login(client, idp)
    csrf_a = await _csrf_token(client)

    group = await _create_group(client, csrf_a, org_a, ws_a, "victim-group")
    group_id = group["id"]

    # org_b 管理员（不同 subject）
    owner_b = await _seed_principal("bob-oidc")
    org_b = await _seed_org()
    ws_b = await _seed_workspace(org_b, name="b")
    await _seed_membership(owner_b, org_b, ["org_owner"])
    await _seed_workspace_membership(owner_b, org_b, ws_b, ["workspace_admin"])
    async for client_b in _client(app):
        await _perform_login(client_b, idp, subject="bob-oidc")
        csrf_b = await _csrf_token(client_b)
        _assert_scim_error(
            await client_b.get(
                f"/scim/v2/Groups/{group_id}",
                headers=_scim_headers(csrf_b, organization_id=org_b, workspace_id=ws_b),
            ),
            404,
        )
        _assert_scim_error(
            await client_b.put(
                f"/scim/v2/Groups/{group_id}",
                headers=_scim_headers(csrf_b, organization_id=org_b, workspace_id=ws_b),
                json={
                    "schemas": [GROUP_SCHEMA],
                    "externalId": "x",
                    "displayName": "x",
                    "members": [],
                },
            ),
            404,
        )


# --------------------------------------------------------------------------- 授权与 fail closed


@pytest.mark.asyncio
async def test_scim_reads_and_mutations_are_policy_gated(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, opa = app_and_client
    member_id = await _seed_principal("member-oidc")
    org = await _seed_org()
    await _seed_membership(member_id, org, ["member"])
    await _perform_login(client, idp, subject="member-oidc")
    csrf = await _csrf_token(client)

    opa.deny = True
    _assert_scim_error(
        await client.post(
            "/scim/v2/Users",
            headers=_scim_headers(csrf, organization_id=org),
            json=_user_body("nope"),
        ),
        403,
    )
    _assert_scim_error(
        await client.get(
            f"/scim/v2/Users/{uuid4()}",
            headers=_scim_headers(csrf, organization_id=org),
        ),
        403,
    )
    assert await _count(
        "external_identities", where="issuer = $1 AND subject = $2", params=[ISSUER, "nope"]
    ) == 0

    denied = [row for row in await _read_audit_rows(org) if row["result"] == "denied"]
    assert len(denied) == 2
    for row in denied:
        assert row["decision_id"] == opa.DENY_DECISION_ID
        assert row["policy_revision"] == opa.DENY_REVISION
        assert row["decision_reason"] == opa.DENY_REASON
        assert row["resource_version"] == 0
        assert row["actor_ref"] == f"user:{member_id}"
        assert _SHA256_DIGEST_RE.fullmatch(row["payload_digest"])

    # 读也经 gate：policy input 形状断言（action=manage、resource=org）
    inputs = opa.inputs
    assert len(inputs) == 2
    for policy_input in inputs:
        assert policy_input["action"] == "manage"
        assert policy_input["resource"]["type"] == "org"
        assert policy_input["actor"]["kind"] == "user"
        assert any(
            binding["name"] == "member" and binding["scope"] == "org"
            for binding in policy_input["actor"]["roles"]
        )


@pytest.mark.asyncio
async def test_scim_opa_unavailable_fails_closed(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    _app, client, idp, opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    await _seed_membership(owner_id, org, ["org_owner"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    # httpx.ConnectError 是 httpx.HTTPError 子类，OPAClient._evaluate_remote 只
    # catch httpx.HTTPError → opa_unavailable；RuntimeError 不被捕获会落入
    # PolicyEnforcer 的 enforcement_internal_error（对照 T4 line 1277 同款）。
    opa.fail = httpx.ConnectError("opa sidecar down")
    _assert_scim_error(
        await client.post(
            "/scim/v2/Users",
            headers=_scim_headers(csrf, organization_id=org),
            json=_user_body("nope"),
        ),
        403,
    )
    assert await _count(
        "external_identities", where="issuer = $1 AND subject = $2", params=[ISSUER, "nope"]
    ) == 0
    denied = [row for row in await _read_audit_rows(org) if row["result"] == "denied"]
    assert len(denied) == 1
    assert denied[0]["decision_id"] is None
    assert denied[0]["policy_revision"] is None
    assert denied[0]["decision_reason"] == "opa_unavailable"


@pytest.mark.asyncio
async def test_scim_session_and_csrf_enforcement(
    migrated_database: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA],
) -> None:
    app, client, idp, _opa = app_and_client
    owner_id = await _reset_alice()
    org = await _seed_org()
    await _seed_membership(owner_id, org, ["org_owner"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    # 无 cookie → 401（SCIM 错误体）
    async for anon in _client(app):
        _assert_scim_error(
            await anon.post(
                "/scim/v2/Users",
                headers={"X-ZhiWei-Organization": str(org)},
                json=_user_body("anon"),
            ),
            401,
        )

    # CSRF 缺失/不匹配 → 403（SCIM 错误体）
    headers = _scim_headers(csrf, organization_id=org)
    headers.pop("X-CSRF-Token")
    _assert_scim_error(
        await client.post("/scim/v2/Users", headers=headers, json=_user_body("nocsrf")),
        403,
    )
    headers = _scim_headers("deadbeef" * 8, organization_id=org)
    _assert_scim_error(
        await client.post("/scim/v2/Users", headers=headers, json=_user_body("badsrf")),
        403,
    )
    assert await _count(
        "external_identities", where="issuer = $1 AND subject = $2", params=[ISSUER, "nocsrf"]
    ) == 0


# --------------------------------------------------------------------------- slow：真实 OPA 纵切


@pytest.mark.slow
@pytest.mark.asyncio
async def test_slow_scim_owner_create_with_real_opa(
    migrated_database: None,
    app_and_client_real_opa: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    _app, client, idp = app_and_client_real_opa
    owner_id = await _reset_alice()
    org = await _seed_org()
    await _seed_membership(owner_id, org, ["org_owner"])
    await _perform_login(client, idp)
    csrf = await _csrf_token(client)

    response = await client.post(
        "/scim/v2/Users",
        headers=_scim_headers(csrf, organization_id=org),
        json=_user_body("slow-owner-create"),
    )
    assert response.status_code == 201, response.text

    audit = await _read_audit_rows(org)
    allowed = [
        row
        for row in audit
        if row["action"] == "scim.user.create" and row["result"] == "allowed"
    ]
    assert len(allowed) == 1
    # 真实 OPA 决策 metadata：decision_id / policy_revision 必填且非空
    assert allowed[0]["decision_id"]
    assert allowed[0]["policy_revision"]
    assert allowed[0]["decision_reason"] == "allow:org_owner"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_slow_scim_member_denied_with_real_opa(
    migrated_database: None,
    app_and_client_real_opa: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    _app, client, idp = app_and_client_real_opa
    member_id = await _seed_principal("slow-member-oidc")
    org = await _seed_org()
    await _seed_membership(member_id, org, ["member"])
    await _perform_login(client, idp, subject="slow-member-oidc")
    csrf = await _csrf_token(client)

    response = await client.post(
        "/scim/v2/Users",
        headers=_scim_headers(csrf, organization_id=org),
        json=_user_body("slow-member-denied"),
    )
    _assert_scim_error(response, 403)
    assert await _count(
        "external_identities",
        where="issuer = $1 AND subject = $2",
        params=[ISSUER, "slow-member-denied"],
    ) == 0
    denied = [row for row in await _read_audit_rows(org) if row["result"] == "denied"]
    assert len(denied) == 1
    assert denied[0]["decision_id"]
    assert denied[0]["policy_revision"]
    assert "no_rule_matched" in denied[0]["decision_reason"]
