"""S1-T4-repair 三轮 RED：真实 OPA bootstrap 纵切（slow，docker + postgres）。

Mock IdP + create_app + 真实 OPA（compose opa 服务，profile identity）+ 真实
PostgreSQL。三轮修复目标：bootstrap 成功后的精确重放必须 200，而不是被二轮
「已有 active org → deny」规则误杀；Rego 判定输入从单值 active_organization_id
改为集合 active_organization_ids（PEP 从全部权威 memberships 解析、去重后稳定
排序）。本文件冻结 org/create 的完整重放契约：

- 无 active org 的 USER 创建首个组织 → 201，同一事务落 org + owner membership +
  allowed 审计（真实 decision_id/revision/reason）+ audit.decision outbox；
- 精确重放（同 key 同 body）→ 200，响应与首次逐字节一致，零新增 audit/outbox/
  membership/idempotency 行；OPA 仍参与每次判定（停掉边车后重放必须 fail
  closed 403，证明不存在 pre-policy 响应缓存旁路）；
- 同 key 不同 digest → 409 idempotency_conflict（failed 审计、decision 元数据全
  NULL、resource_version=0），业务零写入；
- 非创建者成员重放创建者精确请求 → 403 "organization already exists"（幂等 scope
  绑定 owner，不产生新 owner membership、无租户接管）；
- 多 org 成员重放自建目标 org → 200，判定不依赖任何「第一个 org」的查询/排序
  （目标 UUID 排在前面的反序情形也在其中）；
- 目标 org 不在 active 集合 → 403 "policy denied"、业务零写入、零审计
  （pre-tenant 冻结例外：目标 org 不存在，无合法审计 FK scope）；
- OPA 边车不可达 → 同样 403 "policy denied"、零写入、零审计（fail closed）；
- authz_test.rego 交叉校验套件在真实 OPA 上全绿（opa test，bundle 由 entrypoint
  从仓库 policies 构建）。

与 tests/integration/policy/test_opa_sidecar_slow.py 同款纪律：只在 finally 里
还原 opa 服务，不动 postgres；无 docker 时跳过并给出明确理由（slow 显式运行，
不算断言放宽）。

RED 状态（三轮）：sidecar 上仍是二轮 Rego——org/create 要求
active_organization_id == null，主体一旦存在任一 active org 即 deny：
- 精确重放 → 403 "policy denied" 而非 200；
- 同 key 异 digest → 403 而非 409（策略层就挡住了，到不了幂等冲突判定）；
- 非创建者重放 → 403 "policy denied" 而非 "organization already exists"；
- 多 org 重放 → 403 而非 200。
这四项失败就是本轮要修复的重放回归；每项断言里都钉死业务零新增行，防止
GREEN 用「缓存/旁路」或「新写行」假过。
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
from zhiwei.persistence.events import AuditEventData, audit_data_from_row, verify_audit_chain
from zhiwei.persistence.models import AuditEvent

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

COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.test.yaml"
OPA_BASE_URL = "http://127.0.0.1:8181"
COMPOSE_CMD = ["docker", "compose", "-f", str(COMPOSE_FILE), "--profile", "identity"]

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
    """从 base 重建到 head；供本文件全部用例使用。"""
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
        # 真实 OPA 边车（127.0.0.1:8181，compose opa 服务）；policy_http_client
        # 不注入 → OPAClient 自带 httpx.AsyncClient 直连真实 OPA
        "ZHIWEI_OPA_BASE_URL": OPA_BASE_URL,
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
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        yield app, client, idp
    await app.state.dispose_engines()


# --------------------------------------------------------------------------- docker helpers


def _wait_healthy(deadline: float = 120.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        try:
            resp = httpx.get(f"{OPA_BASE_URL}/health?bundles", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("opa 服务未在期限内通过 /health?bundles")


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*COMPOSE_CMD, *args], capture_output=True, text=True, timeout=300, env=env or os.environ
    )


@pytest.fixture()
def opa_service() -> Iterator[None]:
    """确保 opa 容器以当前仓库 policies 重建并就绪（bundle 在容器启动时构建）。

    RED 修订登记（机制缺陷）：仅 `up -d --wait` 会复用既有容器——若容器在策略
    变更前启动，bundle 是旧策略，测试将测错 bundle。force-recreate 保证本文件
    判定的就是当前 authz.rego；测试结束后还原为 compose 定义状态（与
    test_opa_sidecar_slow.py 同款纪律）。
    """
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


async def _reset_alice() -> UUID:
    """清空 alice-oidc 的既有 memberships，返回 principal id（每用例自建精确集合）。"""
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


async def _perform_login_as(client: httpx.AsyncClient, idp: FakeIdP, subject: str) -> str:
    """同 _perform_login，但 id_token 的 sub 覆盖为指定外部身份（bob 等非默认主体）。"""
    login = await client.get("/auth/login")
    assert login.status_code == 302
    idp.record_authorization(login.headers["location"])
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    code = idp.issue_code(state, id_token_overrides={"sub": subject})
    callback = await client.get(f"/auth/callback?code={code}&state={state}")
    assert callback.status_code == 302
    cookie = client.cookies.get(COOKIE)
    assert cookie, "callback 必须签发 session cookie"
    return cookie


async def _csrf_token(client: httpx.AsyncClient) -> str:
    me = await client.get("/api/v1/me")
    assert me.status_code == 200
    return me.json()["csrf_token"]


async def _bootstrap_post(
    client: httpx.AsyncClient, csrf_token: str, org_id: UUID, idempotency_key: str
) -> httpx.Response:
    return await client.post(
        "/api/v1/organizations",
        json={"organization_id": str(org_id)},
        headers={
            "X-CSRF-Token": csrf_token,
            "Origin": "https://test",
            "Idempotency-Key": idempotency_key,
        },
    )


def _event_data(row: dict) -> AuditEventData:
    return audit_data_from_row(AuditEvent(**row))


# --------------------------------------------------------------------------- slow tests


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_opa_bootstrap_201_same_transaction_four_tables(
    migrated_database: None,
    opa_service: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    """真实 OPA 纵切：Mock IdP + create_app + 实际 OPA + PostgreSQL。

    POST /api/v1/organizations 返回 201；同一事务产生 owner membership、allowed
    审计（真实 OPA decision_id/revision/reason）、audit.decision outbox。
    """
    _, client, idp = app_and_client
    principal = await _reset_alice()
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    org_id = uuid4()

    response = await _bootstrap_post(client, csrf_token, org_id, "slow-bootstrap-201")
    assert response.status_code == 201
    assert response.json() == {"id": str(org_id), "status": "active"}

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_id)
            == 1
        )
        member_rows = await connection.fetch(
            "SELECT principal_id, role_bindings FROM memberships WHERE organization_id = $1",
            org_id,
        )
        assert len(member_rows) == 1
        assert member_rows[0]["principal_id"] == principal
        assert json.loads(member_rows[0]["role_bindings"]) == ["owner"]
        audit_rows = await connection.fetch(
            "SELECT * FROM audit_events WHERE organization_id = $1 ORDER BY id", org_id
        )
        assert len(audit_rows) == 1
        row = audit_rows[0]
        outbox = await connection.fetch(
            "SELECT * FROM outbox WHERE topic = 'audit.decision' AND payload->>'resource_id' = $1",
            str(org_id),
        )
        assert len(outbox) == 1, "bootstrap 必须同事务写 audit.decision outbox"
        assert outbox[0]["organization_id"] == org_id
        assert outbox[0]["workspace_id"] is None
        assert json.loads(outbox[0]["payload"])["action"] == "organization.create"
    finally:
        await connection.close()

    # allowed 审计携带真实 OPA 决策 provenance：decision_id/revision 由 OPA 生成
    # （非 mock 常量），reason 来自真实 Rego 的 allow 规则（不伪造 metadata）
    assert row["audit_schema_version"] == 2
    assert row["action"] == "organization.create"
    assert row["result"] == "allowed"
    assert row["decision_id"], "真实 OPA 必须返回非空 decision_id"
    assert row["policy_revision"], "真实 OPA bundle 必须携带非空 revision"
    assert row["decision_reason"].startswith("allow:"), row["decision_reason"]
    assert _SHA256_DIGEST_RE.fullmatch(row["payload_digest"])
    verify_audit_chain(_event_data(r) for r in audit_rows)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_opa_denies_bootstrap_for_existing_active_org_member(
    migrated_database: None,
    opa_service: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    """已有 active org 的 USER 不得 bootstrap：403 policy denied、业务零写入、零审计。"""
    _, client, idp = app_and_client
    principal = await _reset_alice()
    org_x = await _seed_org()
    await _seed_membership(principal, org_x, ["owner"])
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    org_y = uuid4()

    response = await _bootstrap_post(client, csrf_token, org_y, "slow-bootstrap-deny-1")
    assert response.status_code == 403
    assert response.json()["detail"] == "policy denied"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_y)
            == 0
        ), "已有 active org 的主体 bootstrap 必须业务零写入"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM memberships WHERE organization_id = $1", org_y
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM audit_events WHERE organization_id = $1", org_y
            )
            == 0
        ), "bootstrap 被拒审计例外：本请求不允许任何审计行"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM outbox WHERE organization_id = $1", org_y
            )
            == 0
        )
    finally:
        await connection.close()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_opa_unavailable_blocks_bootstrap_zero_writes(
    migrated_database: None,
    opa_service: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    """OPA sidecar 停止：bootstrap 403 policy denied、业务零写入、零审计。"""
    _, client, idp = app_and_client
    await _reset_alice()
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    org_id = uuid4()

    stopped = _run("stop", "opa")
    assert stopped.returncode == 0, stopped.stderr
    try:
        response = await _bootstrap_post(client, csrf_token, org_id, "slow-bootstrap-unavail")
        assert response.status_code == 403
        assert response.json()["detail"] == "policy denied"
    finally:
        _run("start", "opa")
        _wait_healthy()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_id)
            == 0
        ), "OPA 不可达 bootstrap 必须业务零写入"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM memberships WHERE organization_id = $1", org_id
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM audit_events WHERE organization_id = $1", org_id
            )
            == 0
        ), "bootstrap 被拒审计例外：本请求不允许任何审计行"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM outbox WHERE organization_id = $1", org_id
            )
            == 0
        )
    finally:
        await connection.close()


@pytest.mark.slow
def test_rego_suite_passes_against_real_opa() -> None:
    """authz_test.rego 交叉校验套件在真实 OPA 上必须全绿（opa test /policies/zhiwei -v）。"""
    if shutil.which("docker") is None:
        pytest.skip("环境守卫：无 docker 无法执行真实 OPA 纵切（slow 显式运行）")
    up = _run("up", "-d", "--wait", "opa")
    assert up.returncode == 0, f"opa 启动失败:\n{up.stdout}\n{up.stderr}"
    _wait_healthy()
    suite = subprocess.run(
        [*COMPOSE_CMD, "exec", "-T", "opa", "opa", "test", "/policies/zhiwei", "-v"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert suite.returncode == 0, f"opa test 必须全绿:\n{suite.stdout}\n{suite.stderr}"
    assert "PASS: " in suite.stdout, "opa test 汇总行必须出现 PASS: N/M"


# --------------------------------------------------------------------------- 三轮 RED：重放契约


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_opa_bootstrap_exact_replay_returns_200_without_new_writes(
    migrated_database: None,
    opa_service: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    """三轮修复：bootstrap 成功后的精确重放必须 200 且零新增行，OPA 仍参与判定。

    首建 201 后，alice 已是 org 的 owner member——二轮 Rego 因此对重放 deny
    （403 policy denied），这正是被修复的回归。重放 200 必须来自 idempotency
    精确重放（同 scope/key/digest），不得新写 audit/outbox/membership/claim，
    也不得绕过策略层（停边车后重放必须 fail closed 403，证明无 pre-policy
    响应缓存）。
    """
    _, client, idp = app_and_client
    principal = await _reset_alice()
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    org_id = uuid4()
    key = "slow-bootstrap-replay-exact"

    first = await _bootstrap_post(client, csrf_token, org_id, key)
    assert first.status_code == 201
    assert first.json() == {"id": str(org_id), "status": "active"}

    replayed = await _bootstrap_post(client, csrf_token, org_id, key)
    assert replayed.status_code == 200
    assert replayed.json() == first.json()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_id)
            == 1
        )
        member_rows = await connection.fetch(
            "SELECT principal_id, role_bindings FROM memberships WHERE organization_id = $1",
            org_id,
        )
        assert len(member_rows) == 1
        assert member_rows[0]["principal_id"] == principal
        assert json.loads(member_rows[0]["role_bindings"]) == ["owner"]
        audit_rows = await connection.fetch(
            "SELECT result, action FROM audit_events WHERE organization_id = $1", org_id
        )
        assert len(audit_rows) == 1
        assert audit_rows[0]["result"] == "allowed"
        assert audit_rows[0]["action"] == "organization.create"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM outbox WHERE topic = 'audit.decision' "
                "AND payload->>'resource_id' = $1",
                str(org_id),
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM idempotency_records WHERE organization_id = $1 "
                "AND idempotency_key = $2",
                org_id,
                key,
            )
            == 1
        )
    finally:
        await connection.close()

    # OPA 参与性证明：每次判定都真实求值边车（PolicyInput 的 trace_id 每请求
    # 不同 → input digest 不同 → 有界决策缓存必然 miss）。停掉 OPA 后重放若
    # 返回 200 就是 pre-policy 响应缓存旁路；必须 fail closed 403。
    stopped = _run("stop", "opa")
    assert stopped.returncode == 0, stopped.stderr
    try:
        denied = await _bootstrap_post(client, csrf_token, org_id, key)
        assert denied.status_code == 403
        assert denied.json()["detail"] == "policy denied"
    finally:
        _run("start", "opa")
        _wait_healthy()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_id)
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM audit_events WHERE organization_id = $1", org_id
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM idempotency_records WHERE organization_id = $1 "
                "AND idempotency_key = $2",
                org_id,
                key,
            )
            == 1
        )
    finally:
        await connection.close()

    again = await _bootstrap_post(client, csrf_token, org_id, key)
    assert again.status_code == 200
    assert again.json() == first.json()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_opa_bootstrap_same_key_different_digest_conflict_409(
    migrated_database: None,
    opa_service: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    """同 key 不同 body：409 idempotency_conflict + failed 审计，业务零写入。

    预置一条 request_digest 与服务端对真实请求算出的 digest 必然不同的 claim
    （sha256:00…0）。policy 放行（目标 org 在 active 集合）后必须由幂等冲突
    判定拒绝；二轮 Rego 在策略层就 deny（403 policy denied），到不了该判定。
    """
    _, client, idp = app_and_client
    principal = await _seed_principal("alice-oidc")
    org_a = await _seed_org()
    await _seed_membership(principal, org_a, ["owner"])
    conflict_key = "slow-bootstrap-conflict"
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO idempotency_records "
            "(id, organization_id, workspace_id, scope, idempotency_key, request_digest, "
            "response, status, schema_version, created_at) "
            "VALUES ($1, $2, NULL, $3, $4, $5, $6::jsonb, 'completed', 1, now())",
            uuid4(),
            org_a,
            f"organization.create:{principal}",
            conflict_key,
            f"sha256:{'0' * 64}",
            json.dumps({"id": str(org_a), "status": "active"}),
        )
    finally:
        await connection.close()

    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    response = await _bootstrap_post(client, csrf_token, org_a, conflict_key)
    assert response.status_code == 409
    assert response.json()["detail"] == "idempotency key was already used for another request"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_a)
            == 1
        )
        member_rows = await connection.fetch(
            "SELECT principal_id, role_bindings FROM memberships WHERE organization_id = $1",
            org_a,
        )
        assert len(member_rows) == 1
        assert member_rows[0]["principal_id"] == principal
        assert json.loads(member_rows[0]["role_bindings"]) == ["owner"]
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM idempotency_records WHERE organization_id = $1 "
                "AND idempotency_key = $2",
                org_a,
                conflict_key,
            )
            == 1
        ), "冲突请求不得新增幂等记录"
        audit_rows = await connection.fetch(
            "SELECT * FROM audit_events WHERE organization_id = $1 ORDER BY id", org_a
        )
        assert len(audit_rows) == 1
        assert audit_rows[0]["result"] == "failed"
        assert audit_rows[0]["decision_reason"] == "idempotency_conflict"
        assert audit_rows[0]["decision_id"] is None
        assert audit_rows[0]["resource_version"] == 0
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM outbox WHERE topic = 'audit.decision' "
                "AND payload->>'resource_id' = $1",
                str(org_a),
            )
            == 1
        )
    finally:
        await connection.close()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_opa_bootstrap_non_creator_replay_denied_no_takeover(
    migrated_database: None,
    opa_service: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    """非创建者成员重放创建者精确请求：403 organization already exists，无接管。

    bob 与 alice 同属 org_a 但非创建者：policy 放行（目标在 active 集合）后，
    幂等 scope 绑定 owner（organization.create:<creator pid>），bob 的 scope 查
    不到 alice 的 claim → OrganizationExistsError。不得给 bob 写 owner
    membership，不得新增 claim。
    """
    _, client, idp = app_and_client
    alice_pid = await _reset_alice()
    await _perform_login(client, idp)
    csrf_alice = await _csrf_token(client)
    org_a = uuid4()
    key = "slow-bootstrap-takeover"
    first = await _bootstrap_post(client, csrf_alice, org_a, key)
    assert first.status_code == 201

    bob_pid = await _seed_principal("bob-oidc")
    await _seed_membership(bob_pid, org_a, ["member"])
    await _perform_login_as(client, idp, "bob-oidc")
    csrf_bob = await _csrf_token(client)

    attempt = await _bootstrap_post(client, csrf_bob, org_a, key)
    assert attempt.status_code == 403
    assert attempt.json()["detail"] == "organization already exists"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_a)
            == 1
        )
        member_rows = await connection.fetch(
            "SELECT principal_id, role_bindings FROM memberships WHERE organization_id = $1 "
            "ORDER BY principal_id",
            org_a,
        )
        assert len(member_rows) == 2, "重放不得新增 membership（alice owner + bob member）"
        owners = [r for r in member_rows if json.loads(r["role_bindings"]) == ["owner"]]
        assert len(owners) == 1, "owner 必须仍只有 alice 一条，bob 不得接管"
        assert owners[0]["principal_id"] == alice_pid
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM idempotency_records WHERE organization_id = $1 "
                "AND idempotency_key = $2",
                org_a,
                key,
            )
            == 1
        ), "只允许 alice 创建时的原始 claim，bob 重放不得新增"
    finally:
        await connection.close()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_opa_bootstrap_multi_org_replay_is_deterministic(
    migrated_database: None,
    opa_service: None,
    app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP],
) -> None:
    """多 org 成员重放自建目标 org 必须 200，判定不依赖 org 顺序。

    alice 自建 org_a、bob 自建 org_b（UUID 排在 org_a 之前——「查询返回的第一个
    org」式实现会选到 org_b 而误拒 org_a 重放）；alice 再加入 org_b 成为普通成员
    后，重放 org_a 的创建请求必须仍 200。集合判定（org_a ∈ active set）与顺序
    无关；二轮单值 active_organization_id 对该重放 deny（403 policy denied）。
    """
    _, client, idp = app_and_client
    alice_pid = await _reset_alice()
    await _perform_login(client, idp)
    csrf_alice = await _csrf_token(client)
    org_a = uuid4()
    key_a = "slow-bootstrap-multi-a"
    first_a = await _bootstrap_post(client, csrf_alice, org_a, key_a)
    assert first_a.status_code == 201

    org_b = uuid4()
    while str(org_b) >= str(org_a):
        org_b = uuid4()

    bob_pid = await _seed_principal("bob-oidc")
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        # 本文件用例共享同一数据库（migrated_database 为 session 级）：先前用例
        # 可能给 bob-oidc 留过 memberships，必须清空才能把 bob 当成「首登无 org」
        # 的 bootstrap 主体（与 _reset_alice 同款机制）。
        await connection.execute("DELETE FROM memberships WHERE principal_id = $1", bob_pid)
        await connection.execute(
            "DELETE FROM workspace_memberships WHERE principal_id = $1", bob_pid
        )
    finally:
        await connection.close()
    await _perform_login_as(client, idp, "bob-oidc")
    csrf_bob = await _csrf_token(client)
    created_b = await _bootstrap_post(client, csrf_bob, org_b, "slow-bootstrap-multi-b")
    assert created_b.status_code == 201

    await _seed_membership(alice_pid, org_b, ["member"])
    await _perform_login(client, idp)
    csrf_alice2 = await _csrf_token(client)

    replayed = await _bootstrap_post(client, csrf_alice2, org_a, key_a)
    assert replayed.status_code == 200
    assert replayed.json() == first_a.json()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_a)
            == 1
        )
        member_rows = await connection.fetch(
            "SELECT principal_id, role_bindings FROM memberships WHERE organization_id = $1",
            org_a,
        )
        assert len(member_rows) == 1
        assert member_rows[0]["principal_id"] == alice_pid
        assert json.loads(member_rows[0]["role_bindings"]) == ["owner"]
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM audit_events WHERE organization_id = $1", org_a
            )
            == 1
        ), "重放不得追加 allowed 审计"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM idempotency_records WHERE organization_id = $1 "
                "AND idempotency_key = $2",
                org_a,
                key_a,
            )
            == 1
        )
    finally:
        await connection.close()
