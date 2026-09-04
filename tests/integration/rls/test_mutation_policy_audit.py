"""S1-T4-repair RED：生产 PEP/audit 纵切的 API 级集成契约（tests/integration/rls/）。

设计/验收方冻结（docs/handoffs/s1-t4-repair-design.md §3.1/§5，本文件按冻结契约实现，
不以今日代码为准）：
- 组合根是唯一强制点：create_app 组合 OPAClient/PolicyEnforcer，policy_http_client 是
  唯一外部 binding 替换点（MockTransport 只出现在测试）；ZHIWEI_OPA_BASE_URL 组合期必需；
- policy 先于业务事务求值：denied → append_fail_closed_audit 独立审计事务 + 业务零写入；
  allowed → 同一租户事务内 audit + outbox（同提交/同回滚，审计写失败 → 整体回滚）；
- 审计冻结值：actor_ref / effective_identity_ref = "user:<principal_id>"；action 串
  organization.create / organization.workspace.create / workspace.group.create /
  organization.member.add / organization.member.remove；audit_schema_version=2；
  payload_digest 匹配 ^sha256:[0-9a-f]{64}$；request/trace_id = uuid4().hex（32 位
  小写 hex，两次请求互异、非 body/header 来源；请求模型 extra="forbid" 拒绝注入）；
- metadata 三类规则：allowed 与 OPA-denied 行携带真实 decision_id / policy_revision /
  decision_reason（逐字保留）；本地拒绝（opa_unavailable / tenant_scope_mismatch）
  两列全 NULL + 固定 reason 码；幂等冲突 result=failed + NULL + "idempotency_conflict"；
- resource_version：allowed=1、denied/failed=0（unknown 不伪装成 1）；
- 跨租户猜 ID：gate 不构造 PolicyInput、不发 OPA 请求（mock 计数 0），审计写 actor
  scope（organization_id=actor org、workspace_id NULL），API 403 "outside tenant scope"；
- bootstrap（§3.1.9）：首登 principal 无组织绑定，policy input 的 actor.roles 为空
  （不得伪造 org_owner 绑定——G3 禁止第二套事实源）；policy input 的 org = 新建 org；
  **bootstrap 被拒审计例外（独立审查 7.1 裁决）**：audit_events.organization_id NOT NULL
  且 FK → organizations.id、append_fail_closed_audit 要求非空 tenant context——被 OPA 拒绝
  的 bootstrap 目标 org 不存在，任何 scope 都无合法审计落点，故 403 "policy denied"、
  业务零写入、**不写 denied 审计**（schema 边界约束的冻结例外，不得静默扩大）；
- API detail：OPA deny / opa_unavailable → 403 "policy denied"。

RED 状态：今日 create_app 只接受 oidc_http_client——传入 policy_http_client 在 fixture
组合期抛 TypeError（G1 组合契约缺失），本文件全部用例以该错误在 setup 期失败。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
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
OPA_BASE_URL = "http://opa.test:8181"

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")


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


class FakeOPA:
    """本地假 OPA：响应严格符合 T3 client 校验（decision_id/reason/唯一 bundle revision）。

    客户端对缺 decision_id / provenance / bundles / revision 的响应一律判畸形并本地
    拒绝（tests/unit/policy/test_enforcement.py ok_response 的形状），因此本 handler
    必须逐字给出该形状。calls 记录每次收到的规范化 input（body 为 {"input": ...}）。
    """

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
    """从 base 重建到 head（GREEN 后含 0006）；供本目录所有用例使用。"""
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
        # §3.2：组合期必需输入（GREEN 加入 _REQUIRED）；RED 阶段 load_settings
        # 尚不识别该键，传入即被忽略，create_app 在 policy_http_client 处失败。
        "ZHIWEI_OPA_BASE_URL": OPA_BASE_URL,
    }


@pytest_asyncio.fixture(loop_scope="function")
async def app_and_client(
    keyring_path: Path,
    idp: FakeIdP,
    opa: FakeOPA,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]]:
    app = create_app(
        load_settings(_settings(keyring_path)),
        oidc_http_client=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler), timeout=5.0),
        # RED：今日 create_app 只接受 oidc_http_client，这里必然 TypeError——
        # 组合契约缺失（G1）是本目录全部用例的预期失败点。
        policy_http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(opa.handler), timeout=5.0
        ),
    )
    async with httpx.AsyncClient(
        # raise_app_exceptions=False：500 场景（审计写失败等）要观察响应本身——
        # Starlette ServerErrorMiddleware 发送 500 后总是再抛原始异常，默认行为会把
        # 预期内的 500 变成 client.post 抛错（RED 修订登记：repair addendum §3.3）。
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        yield app, client, idp, opa
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
            "VALUES ($1, $2, $3::jsonb)",
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
            "VALUES ($1, $2, $3, $4::jsonb)",
            principal_id,
            organization_id,
            workspace_id,
            json.dumps(roles),
        )
    finally:
        await connection.close()


async def _delete_bootstrap_claims_if_exists(principal_id: UUID) -> None:
    """fixture 清理专用窄 helper：claim 表不存在则跳过，存在才 DELETE。

    四轮 RED 机制修订登记：RED 数据库停在 0007，claim 表（0008）尚不存在——无条件
    DELETE 会在 fixture 阶段抛 UndefinedTableError，把用例截断在 setup 期。先经
    to_regclass('public.organization_bootstrap_claims') 判断表是否存在：存在才清理，
    不存在直接继续。这是 RED fixture 兼容，不是生产旁路。
    """
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        exists = await connection.fetchval(
            "SELECT to_regclass('public.organization_bootstrap_claims') IS NOT NULL"
        )
        if exists:
            await connection.execute(
                "DELETE FROM organization_bootstrap_claims WHERE principal_id = $1",
                principal_id,
            )
    finally:
        await connection.close()


async def _reset_alice() -> UUID:
    """清空 alice-oidc 的既有 memberships 与 bootstrap claim，返回 principal id。

    四轮 RED 机制修订登记：bootstrap claim 是 identity-global 持久状态（0008），
    本文件共享同一数据库与同一 principal——用例必须连同 claim 一起重置，否则先前
    用例的 claim 会让后续用例的 bootstrap 得到 403。claim 表无直接表权限，
    测试经 migrator（superuser）清理。
    """
    principal = await _seed_principal("alice-oidc")
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute("DELETE FROM memberships WHERE principal_id = $1", principal)
        await connection.execute(
            "DELETE FROM workspace_memberships WHERE principal_id = $1", principal
        )
    finally:
        await connection.close()
    await _delete_bootstrap_claims_if_exists(principal)
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


async def _csrf_token(client: httpx.AsyncClient) -> str:
    me = await client.get("/api/v1/me")
    assert me.status_code == 200
    return me.json()["csrf_token"]


def _mutation_headers(
    csrf_token: str,
    idempotency_key: str,
    *,
    organization_id: UUID,
    workspace_id: UUID | None = None,
) -> dict[str, str]:
    headers = {
        "X-CSRF-Token": csrf_token,
        "Origin": "https://test",
        "Idempotency-Key": idempotency_key,
        "X-ZhiWei-Organization": str(organization_id),
    }
    if workspace_id is not None:
        headers["X-ZhiWei-Workspace"] = str(workspace_id)
    return headers


async def _read_audit_rows(organization_id: UUID, workspace_id: UUID | None) -> list[dict]:
    """以 migrator 读取单 scope 链的全部 audit 行（owner 不受 RLS 限制）。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        if workspace_id is None:
            rows = await connection.fetch(
                "SELECT * FROM audit_events "
                "WHERE organization_id = $1 AND workspace_id IS NULL ORDER BY id",
                organization_id,
            )
        else:
            rows = await connection.fetch(
                "SELECT * FROM audit_events "
                "WHERE organization_id = $1 AND workspace_id = $2 ORDER BY id",
                organization_id,
                workspace_id,
            )
        return [dict(row) for row in rows]
    finally:
        await connection.close()


def _event_data(row: dict) -> AuditEventData:
    return audit_data_from_row(AuditEvent(**row))


def _assert_frozen_allowed_fields(
    row: dict,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID,
    principal_id: UUID,
    resource_version: int = 1,
) -> None:
    """allowed 审计行的冻结字段全集（§3.1.6/§3.1.11 三类 metadata 规则之一）。"""
    assert row["audit_schema_version"] == 2
    assert row["organization_id"] == organization_id
    assert row["workspace_id"] == workspace_id
    assert row["action"] == action
    assert row["resource_type"] == resource_type
    assert row["resource_id"] == resource_id
    assert row["resource_version"] == resource_version
    assert row["actor_ref"] == f"user:{principal_id}"
    assert row["effective_identity_ref"] == f"user:{principal_id}"
    assert row["result"] == "allowed"
    assert row["decision_id"] == FakeOPA.ALLOW_DECISION_ID
    assert row["policy_revision"] == FakeOPA.ALLOW_REVISION
    assert row["decision_reason"] == FakeOPA.ALLOW_REASON
    assert _SHA256_DIGEST_RE.fullmatch(row["payload_digest"])
    assert _REQUEST_ID_RE.fullmatch(row["request_id"])
    assert _REQUEST_ID_RE.fullmatch(row["trace_id"])


def _assert_frozen_denied_fields(
    row: dict,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID,
    principal_id: UUID,
    reason: str,
    result: str = "denied",
    resource_version: int = 0,
) -> None:
    """denied/failed 审计行的冻结字段全集：NULL metadata + 固定 reason 码。"""
    assert row["audit_schema_version"] == 2
    assert row["organization_id"] == organization_id
    assert row["workspace_id"] == workspace_id
    assert row["action"] == action
    assert row["resource_type"] == resource_type
    assert row["resource_id"] == resource_id
    assert row["resource_version"] == resource_version
    assert row["actor_ref"] == f"user:{principal_id}"
    assert row["effective_identity_ref"] == f"user:{principal_id}"
    assert row["result"] == result
    assert row["decision_id"] is None
    assert row["policy_revision"] is None
    assert row["decision_reason"] == reason
    assert _SHA256_DIGEST_RE.fullmatch(row["payload_digest"])
    assert _REQUEST_ID_RE.fullmatch(row["request_id"])
    assert _REQUEST_ID_RE.fullmatch(row["trace_id"])


async def _install_audit_fail_trigger() -> None:
    """audit_events BEFORE INSERT 触发器：模拟审计写失败（用后必须删除）。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "CREATE OR REPLACE FUNCTION zhiwei_test_audit_fail() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'intentional audit write failure (test trigger)'; END $$ "
            "LANGUAGE plpgsql"
        )
        await connection.execute(
            "CREATE TRIGGER zhiwei_test_audit_fail_trg BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION zhiwei_test_audit_fail()"
        )
    finally:
        await connection.close()


async def _drop_audit_fail_trigger() -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "DROP TRIGGER IF EXISTS zhiwei_test_audit_fail_trg ON audit_events"
        )
        await connection.execute("DROP FUNCTION IF EXISTS zhiwei_test_audit_fail()")
    finally:
        await connection.close()


# --------------------------------------------------------------------------- 1-3. bootstrap


@pytest.mark.asyncio
async def test_bootstrap_creates_org_owner_audit_outbox_and_policy_input(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """bootstrap 成功：org + owner membership + v2 audit + outbox 同事务；OPA 输入冻结。"""
    _, client, idp, opa = app_and_client
    principal = await _reset_alice()
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    org_id = uuid4()

    response = await client.post(
        "/api/v1/organizations",
        json={"organization_id": str(org_id)},
        headers={
            "X-CSRF-Token": csrf_token,
            "Origin": "https://test",
            "Idempotency-Key": "bootstrap-audit-1",
        },
    )
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

    rows = await _read_audit_rows(org_id, None)
    assert len(rows) == 1
    _assert_frozen_allowed_fields(
        rows[0],
        organization_id=org_id,
        workspace_id=None,
        action="organization.create",
        resource_type="organization",
        resource_id=org_id,
        principal_id=principal,
        resource_version=1,
    )
    verify_audit_chain(_event_data(row) for row in rows)

    # OPA 输入冻结（§3.1.9/§3.1.10/§3.1.13）：policy input 的 org=新建 org；
    # actor.roles 为空（首登无绑定，不伪造 org_owner）；trace 与本请求审计共用。
    assert len(opa.inputs) == 1
    policy_input = opa.inputs[0]
    assert policy_input["organization_id"] == str(org_id)
    assert policy_input["workspace_id"] is None
    assert policy_input["action"] == "create"
    assert policy_input["purpose"] == "general"
    assert policy_input["resource"] == {"type": "org", "id": str(org_id), "version": "1"}
    assert policy_input["classification"] is None
    assert policy_input["risk"] is None
    assert policy_input["delegation"] == []
    # SoD/own 证据全空（S1 mutation 无此类动作）；序列化形状含全部默认字段
    # （T3 PolicyInput model_dump 契约，不省略 None/空值）
    assert policy_input["resource_context"] == {
        "owner_principal_id": None,
        "last_content_author_principal_id": None,
        "requester_principal_id": None,
        "modifier_principal_ids": [],
        "agent_identity_principal_id": None,
        "publisher_principal_id": None,
        "publisher_roles": [],
    }
    assert policy_input["actor"] == {
        "principal_id": str(principal),
        "kind": "user",
        "roles": [],
        "active_organization_ids": [],
    }
    assert policy_input["context"]["trace_id"] == rows[0]["trace_id"]


@pytest.mark.asyncio
async def test_bootstrap_audit_write_failure_rolls_back_everything(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """bootstrap 审计写失败：业务/审计/outbox 全部回滚，API 500（§3.1.7 allowed 同事务）。"""
    _, client, idp, opa = app_and_client
    await _reset_alice()
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    org_id = uuid4()

    await _install_audit_fail_trigger()
    try:
        response = await client.post(
            "/api/v1/organizations",
            json={"organization_id": str(org_id)},
            headers={
                "X-CSRF-Token": csrf_token,
                "Origin": "https://test",
                "Idempotency-Key": "bootstrap-audit-rollback",
            },
        )
        assert response.status_code == 500
    finally:
        await _drop_audit_fail_trigger()

    # policy 先于事务求值（已发生一次 allow 决策）；业务事务内审计写失败 → 整体回滚
    assert len(opa.inputs) == 1
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_id)
            == 0
        )
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
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM outbox WHERE organization_id = $1", org_id
            )
            == 0
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_bootstrap_opa_deny_blocks_without_audit_row(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """OPA 拒绝的 bootstrap：403 + 零写入 + 不写 denied 审计（§3.1.9 schema 边界例外）。

    audit_events.organization_id NOT NULL 且 FK → organizations.id、append_fail_closed_audit
    要求非空 tenant context——被拒 bootstrap 的目标 org 不存在，任何 scope 都无合法审计
    落点（identity-global 审计链不存在，另建属越界）。该例外由本测试固定，不得静默扩大；
    organization_exists 等 org 已存在场景的 failed 审计仍写在该 org scope（FK 满足）。
    """
    _, client, idp, opa = app_and_client
    await _reset_alice()
    opa.deny = True
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    org_id = uuid4()

    response = await client.post(
        "/api/v1/organizations",
        json={"organization_id": str(org_id)},
        headers={
            "X-CSRF-Token": csrf_token,
            "Origin": "https://test",
            "Idempotency-Key": "bootstrap-deny-1",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "policy denied"

    # gate 对 bootstrap 正常求值：OPA 恰被调用一次，input 的 org=新建 org、
    # actor.roles 为空（首登无绑定，§3.1.9）
    assert len(opa.inputs) == 1
    policy_input = opa.inputs[0]
    assert policy_input["organization_id"] == str(org_id)
    assert policy_input["actor"]["kind"] == "user"
    assert policy_input["actor"]["roles"] == []
    assert policy_input["action"] == "create"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_id)
            == 0
        )
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
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM idempotency_records WHERE organization_id = $1", org_id
            )
            == 0
        ), "被拒 bootstrap 不得 claim 幂等记录"
    finally:
        await connection.close()


# --------------------------------------------------------------------------- 3-5. workspace/group/member 审计与幂等


@pytest.mark.asyncio
async def test_workspace_group_member_mutations_auto_write_audit(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """每次真实 mutation 自动写 allowed 审计；成员删除后 membership 行消失。"""
    _, client, idp, opa = app_and_client
    principal = await _reset_alice()
    org_a = await _seed_org()
    ws_a = await _seed_workspace(org_a)
    bob = await _seed_principal("bob-audit")
    await _seed_membership(principal, org_a, ["owner"])
    await _seed_workspace_membership(principal, org_a, ws_a, ["builder"])
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)

    workspace_id = uuid4()
    response = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces",
        json={"workspace_id": str(workspace_id), "name": "eng"},
        headers=_mutation_headers(csrf_token, "mutation-ws-1", organization_id=org_a),
    )
    assert response.status_code == 201

    group_id = uuid4()
    response = await client.post(
        f"/api/v1/workspaces/{ws_a}/groups",
        json={"group_id": str(group_id), "name": "Finance"},
        headers=_mutation_headers(
            csrf_token, "mutation-group-1", organization_id=org_a, workspace_id=ws_a
        ),
    )
    assert response.status_code == 201

    response = await client.post(
        f"/api/v1/organizations/{org_a}/members",
        json={"principal_id": str(bob), "role_bindings": ["member"]},
        headers=_mutation_headers(csrf_token, "mutation-add-1", organization_id=org_a),
    )
    assert response.status_code == 201

    response = await client.delete(
        f"/api/v1/organizations/{org_a}/members/{bob}",
        headers=_mutation_headers(csrf_token, "mutation-remove-1", organization_id=org_a),
    )
    assert response.status_code == 204

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM memberships WHERE principal_id = $1 AND organization_id = $2",
                bob,
                org_a,
            )
            == 0
        ), "remove 后 membership 必须消失"
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", workspace_id)
            == 1
        )
        assert await connection.fetchval("SELECT count(*) FROM groups WHERE id = $1", group_id) == 1
    finally:
        await connection.close()

    # 组织级 scope 链：workspace.create + member.add + member.remove（按 action 定位）
    org_rows = {row["action"]: row for row in await _read_audit_rows(org_a, None)}
    assert set(org_rows) == {
        "organization.workspace.create",
        "organization.member.add",
        "organization.member.remove",
    }
    _assert_frozen_allowed_fields(
        org_rows["organization.workspace.create"],
        organization_id=org_a,
        workspace_id=None,
        action="organization.workspace.create",
        resource_type="workspace",
        resource_id=workspace_id,
        principal_id=principal,
        resource_version=1,
    )
    _assert_frozen_allowed_fields(
        org_rows["organization.member.add"],
        organization_id=org_a,
        workspace_id=None,
        action="organization.member.add",
        resource_type="membership",
        resource_id=bob,
        principal_id=principal,
        resource_version=1,
    )
    _assert_frozen_allowed_fields(
        org_rows["organization.member.remove"],
        organization_id=org_a,
        workspace_id=None,
        action="organization.member.remove",
        resource_type="membership",
        resource_id=bob,
        principal_id=principal,
        resource_version=1,
    )
    verify_audit_chain(_event_data(row) for row in await _read_audit_rows(org_a, None))

    # workspace 级 scope 链：group.create
    ws_rows = [row for row in await _read_audit_rows(org_a, ws_a) if row["resource_id"] == group_id]
    assert len(ws_rows) == 1
    _assert_frozen_allowed_fields(
        ws_rows[0],
        organization_id=org_a,
        workspace_id=ws_a,
        action="workspace.group.create",
        resource_type="group",
        resource_id=group_id,
        principal_id=principal,
        resource_version=1,
    )
    verify_audit_chain(_event_data(row) for row in await _read_audit_rows(org_a, ws_a))

    # 角色绑定流入 PolicyInput（roles-flow 证明）：org_owner 绑定逐字出现在 workspace
    # create 的 OPA 输入中（§3.2 resolve_context 填充 role_bindings → build_policy_input）
    assert len(opa.inputs) == 4
    first_input = opa.inputs[0]
    assert first_input["organization_id"] == str(org_a)
    assert first_input["resource"]["type"] == "workspace_policy"
    assert first_input["resource"]["version"] == "1"
    # 2026-09-03 修订（ADR-012 反例 4）：动作从 configure_workspace 改为 configure——
    # 原值是矩阵死锁的固化（唯一允许角色 workspace_admin 是 workspace 作用域，
    # 创建时无 workspace 上下文 → 真实 OPA 恒 deny）；specs/s1 §3 增补裁定为
    # org 作用域 configure（org_owner）。
    assert first_input["action"] == "configure"
    assert first_input["purpose"] == "general"
    assert {
        "name": "org_owner",
        "scope": "org",
        "organization_id": str(org_a),
        "workspace_id": None,
    } in first_input["actor"]["roles"]
    assert (
        first_input["context"]["trace_id"] == org_rows["organization.workspace.create"]["trace_id"]
    )


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_append_audit_or_outbox(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """精确重放（同 key + 同 body）：200 返回原响应，不追加 audit/outbox（§3.1.8）。"""
    _, client, idp, _ = app_and_client
    principal = await _reset_alice()
    org_a = await _seed_org()
    await _seed_membership(principal, org_a, ["owner"])
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)

    workspace_id = uuid4()
    body = {"workspace_id": str(workspace_id), "name": "sales"}
    headers = _mutation_headers(csrf_token, "replay-ws-1", organization_id=org_a)

    first = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces", json=body, headers=headers
    )
    assert first.status_code == 201

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        audit_before = await connection.fetchval(
            "SELECT count(*) FROM audit_events WHERE resource_id = $1", workspace_id
        )
        outbox_before = await connection.fetchval(
            "SELECT count(*) FROM outbox WHERE topic = 'audit.decision' "
            "AND payload->>'resource_id' = $1",
            str(workspace_id),
        )
    finally:
        await connection.close()
    assert audit_before == 1
    assert outbox_before == 1

    replayed = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces", json=body, headers=headers
    )
    assert replayed.status_code == 200
    assert replayed.json() == first.json()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", workspace_id)
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM audit_events WHERE resource_id = $1", workspace_id
            )
            == audit_before
        ), "精确重放不得追加审计行"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM outbox WHERE topic = 'audit.decision' "
                "AND payload->>'resource_id' = $1",
                str(workspace_id),
            )
            == outbox_before
        ), "精确重放不得追加 outbox 行"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_idempotency_conflict_writes_failed_audit_and_no_business_write(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """同 key 不同 body：409 + failed 审计（NULL metadata、idempotency_conflict）+ 零写入。"""
    _, client, idp, _ = app_and_client
    principal = await _reset_alice()
    org_a = await _seed_org()
    await _seed_membership(principal, org_a, ["owner"])
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)

    first_id = uuid4()
    body = {"workspace_id": str(first_id), "name": "sales"}
    headers = _mutation_headers(csrf_token, "conflict-ws-1", organization_id=org_a)
    first = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces", json=body, headers=headers
    )
    assert first.status_code == 201

    second_id = uuid4()
    response = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces",
        json={"workspace_id": str(second_id), "name": "Conflicting"},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "idempotency key was already used for another request"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", first_id)
            == 1
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", second_id)
            == 0
        ), "冲突请求不得落库"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM idempotency_records WHERE organization_id = $1 "
                "AND idempotency_key = 'conflict-ws-1'",
                org_a,
            )
            == 1
        ), "幂等记录只允许首次请求的一条"
    finally:
        await connection.close()

    rows = [row for row in await _read_audit_rows(org_a, None) if row["resource_id"] == second_id]
    assert len(rows) == 1
    _assert_frozen_denied_fields(
        rows[0],
        organization_id=org_a,
        workspace_id=None,
        action="organization.workspace.create",
        resource_type="workspace",
        resource_id=second_id,
        principal_id=principal,
        reason="idempotency_conflict",
        result="failed",
        resource_version=0,
    )


# --------------------------------------------------------------------------- 6-8. deny 路径


@pytest.mark.asyncio
async def test_opa_deny_blocks_mutation_and_writes_denied_audit(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """OPA deny：403 "policy denied"、业务零写入、denied 审计携带真实决策 metadata。"""
    _, client, idp, opa = app_and_client
    principal = await _reset_alice()
    org_a = await _seed_org()
    await _seed_membership(principal, org_a, ["owner"])
    opa.deny = True
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)

    workspace_id = uuid4()
    response = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces",
        json={"workspace_id": str(workspace_id), "name": "sales"},
        headers=_mutation_headers(csrf_token, "deny-ws-1", organization_id=org_a),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "policy denied"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", workspace_id)
            == 0
        ), "deny 路径业务零写入"
    finally:
        await connection.close()

    rows = [
        row for row in await _read_audit_rows(org_a, None) if row["resource_id"] == workspace_id
    ]
    assert len(rows) == 1
    row = rows[0]
    # OPA deny 行携带真实 decision_id/revision/reason（逐字，不得改写）
    assert row["result"] == "denied"
    assert row["decision_id"] == FakeOPA.DENY_DECISION_ID
    assert row["policy_revision"] == FakeOPA.DENY_REVISION
    assert row["decision_reason"] == FakeOPA.DENY_REASON
    assert row["resource_version"] == 0
    assert row["actor_ref"] == f"user:{principal}"
    assert row["effective_identity_ref"] == f"user:{principal}"
    assert row["audit_schema_version"] == 2
    assert _SHA256_DIGEST_RE.fullmatch(row["payload_digest"])
    assert _REQUEST_ID_RE.fullmatch(row["request_id"])
    assert _REQUEST_ID_RE.fullmatch(row["trace_id"])


@pytest.mark.asyncio
async def test_opa_unavailable_fails_closed_with_denied_audit(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """OPA 不可达：403 "policy denied"、零写入、denied 审计 NULL metadata + opa_unavailable。"""
    _, client, idp, opa = app_and_client
    principal = await _reset_alice()
    org_a = await _seed_org()
    await _seed_membership(principal, org_a, ["owner"])
    opa.fail = httpx.ConnectError("opa sidecar is down")
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)

    workspace_id = uuid4()
    response = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces",
        json={"workspace_id": str(workspace_id), "name": "sales"},
        headers=_mutation_headers(csrf_token, "unavailable-ws-1", organization_id=org_a),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "policy denied"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", workspace_id)
            == 0
        )
    finally:
        await connection.close()

    rows = [
        row for row in await _read_audit_rows(org_a, None) if row["resource_id"] == workspace_id
    ]
    assert len(rows) == 1
    _assert_frozen_denied_fields(
        rows[0],
        organization_id=org_a,
        workspace_id=None,
        action="organization.workspace.create",
        resource_type="workspace",
        resource_id=workspace_id,
        principal_id=principal,
        reason="opa_unavailable",
        result="denied",
        resource_version=0,
    )


@pytest.mark.asyncio
async def test_denied_audit_write_failure_still_means_zero_mutation(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """deny 路径审计写失败：异常上抛 500，mutation 绝不执行（§3.1.7 审计失败不吞）。"""
    _, client, idp, opa = app_and_client
    principal = await _reset_alice()
    org_a = await _seed_org()
    await _seed_membership(principal, org_a, ["owner"])
    opa.deny = True
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    workspace_id = uuid4()

    await _install_audit_fail_trigger()
    try:
        response = await client.post(
            f"/api/v1/organizations/{org_a}/workspaces",
            json={"workspace_id": str(workspace_id), "name": "sales"},
            headers=_mutation_headers(csrf_token, "deny-audit-fail", organization_id=org_a),
        )
        assert response.status_code == 500
    finally:
        await _drop_audit_fail_trigger()

    assert len(opa.inputs) == 1, "policy 必须已求值（deny 决策先于审计写失败）"
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", workspace_id)
            == 0
        ), "审计写失败不得留下任何业务写入"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM audit_events WHERE resource_id = $1", workspace_id
            )
            == 0
        ), "失败的审计事务必须整体回滚"
    finally:
        await connection.close()


# --------------------------------------------------------------------------- 9-10. 跨租户与请求标识


@pytest.mark.asyncio
async def test_cross_tenant_guessed_id_denies_without_opa_call(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """跨租户猜 ID：不发 OPA 请求（计数 0），audit 写 actor scope，version=0，403。"""
    _, client, idp, opa = app_and_client
    principal = await _reset_alice()
    org_a = await _seed_org()
    org_b = await _seed_org()
    ws_b = await _seed_workspace(org_b)
    await _seed_membership(principal, org_a, ["owner"])
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)

    guessed_id = uuid4()
    response = await client.post(
        f"/api/v1/organizations/{org_b}/workspaces",
        json={"workspace_id": str(guessed_id), "name": "intruder"},
        headers=_mutation_headers(csrf_token, "cross-tenant-1", organization_id=org_a),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "outside tenant scope"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM workspaces WHERE organization_id = $1", org_b
            )
            == 1
        ), "org_b 的 workspace 集合必须原样"
        assert await connection.fetchval("SELECT id FROM workspaces WHERE id = $1", ws_b) == ws_b
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", guessed_id)
            == 0
        )
    finally:
        await connection.close()

    # gate 结构 scope 检查在构造 PolicyInput 之前：OPA 一次都不允许被调用
    assert len(opa.inputs) == 0, "跨租户拒绝不得构造 PolicyInput / 不得发 OPA 请求"
    rows = [row for row in await _read_audit_rows(org_a, None) if row["resource_id"] == guessed_id]
    assert len(rows) == 1
    _assert_frozen_denied_fields(
        rows[0],
        organization_id=org_a,
        workspace_id=None,
        action="organization.workspace.create",
        resource_type="workspace",
        resource_id=guessed_id,
        principal_id=principal,
        reason="tenant_scope_mismatch",
        result="denied",
        resource_version=0,
    )


@pytest.mark.asyncio
async def test_request_and_trace_ids_are_distinct_server_generated(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """request/trace id：两次请求互异、32 位小写 hex、非 body 来源（模型 extra=forbid）。"""
    _, client, idp, _ = app_and_client
    principal = await _reset_alice()
    org_a = await _seed_org()
    await _seed_membership(principal, org_a, ["owner"])
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)

    first_id, second_id = uuid4(), uuid4()
    first = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces",
        json={"workspace_id": str(first_id), "name": "alpha"},
        headers=_mutation_headers(csrf_token, "trace-key-1", organization_id=org_a),
    )
    assert first.status_code == 201
    second = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces",
        json={"workspace_id": str(second_id), "name": "beta"},
        headers=_mutation_headers(csrf_token, "trace-key-2", organization_id=org_a),
    )
    assert second.status_code == 201

    # 客户端注入尝试：请求模型 extra="forbid"，request_id/trace_id 一律 422
    response = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces",
        json={
            "workspace_id": str(uuid4()),
            "name": "injected",
            "request_id": "a" * 32,
            "trace_id": "b" * 32,
        },
        headers=_mutation_headers(csrf_token, "trace-key-3", organization_id=org_a),
    )
    assert response.status_code == 422

    rows = await _read_audit_rows(org_a, None)
    by_resource = {row["resource_id"]: row for row in rows}
    assert set(by_resource) == {first_id, second_id}
    first_row, second_row = by_resource[first_id], by_resource[second_id]
    for row in (first_row, second_row):
        assert _REQUEST_ID_RE.fullmatch(row["request_id"])
        assert _REQUEST_ID_RE.fullmatch(row["trace_id"])
    assert first_row["request_id"] != second_row["request_id"], "两次请求 request_id 必须互异"
    assert first_row["trace_id"] != second_row["trace_id"], "两次请求 trace_id 必须互异"
    # 生成值不得来自请求 body/header（body 值 + 幂等键都不是 32 位 hex 来源）
    client_values = {str(first_id), str(second_id), "alpha", "beta", "trace-key-1", "trace-key-2"}
    for row in (first_row, second_row):
        assert row["request_id"] not in client_values
        assert row["trace_id"] not in client_values


@pytest.mark.asyncio
async def test_no_mutation_success_with_zero_audit_and_outbox(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """生产 API 反例冻结：任何 mutation 成功（201/204）都不得与「audit/outbox 双零」同现。

    每个 mutation 端点独立 scope；成功后该 scope 的 audit_events 与 outbox 必须各 ≥ 1。
    """
    _, client, idp, _ = app_and_client
    await _reset_alice()
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)

    org_id = uuid4()
    # bootstrap 是 identity-global 请求：不携带 X-ZhiWei-Organization（actor 尚无 org）
    response = await client.post(
        "/api/v1/organizations",
        json={"organization_id": str(org_id)},
        headers={
            "X-CSRF-Token": csrf_token,
            "Origin": "https://test",
            "Idempotency-Key": "counter-bootstrap-1",
        },
    )
    assert response.status_code == 201
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM audit_events WHERE organization_id = $1 AND workspace_id IS NULL",
            org_id,
        ) >= 1, "bootstrap 201 不得与 audit 双零同现"
        assert await connection.fetchval(
            "SELECT count(*) FROM outbox WHERE organization_id = $1 AND workspace_id IS NULL",
            org_id,
        ) >= 1, "bootstrap 201 不得与 outbox 双零同现"
    finally:
        await connection.close()

    workspace_id = uuid4()
    response = await client.post(
        f"/api/v1/organizations/{org_id}/workspaces",
        json={"workspace_id": str(workspace_id), "name": "counter-ws"},
        headers=_mutation_headers(csrf_token, "counter-ws-1", organization_id=org_id),
    )
    assert response.status_code == 201
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM audit_events WHERE organization_id = $1 AND workspace_id IS NULL",
            org_id,
        ) >= 2
        assert await connection.fetchval(
            "SELECT count(*) FROM outbox WHERE organization_id = $1 AND workspace_id IS NULL",
            org_id,
        ) >= 2
    finally:
        await connection.close()

    group_id = uuid4()
    # group 端点要求 workspace 级 actor 上下文：创建 workspace 时已同事务授予
    # 创建者 workspace_admin（2026-09-03 增补的 bootstrap 路径），无需再补绑定
    # ——重复 seed 会撞 pk_workspace_memberships。
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/groups",
        json={"group_id": str(group_id), "name": "CounterGroup"},
        headers=_mutation_headers(
            csrf_token, "counter-group-1", organization_id=org_id, workspace_id=workspace_id
        ),
    )
    assert response.status_code == 201
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM audit_events WHERE organization_id = $1 AND workspace_id = $2",
            org_id, workspace_id,
        ) >= 1
        assert await connection.fetchval(
            "SELECT count(*) FROM outbox WHERE organization_id = $1 AND workspace_id = $2",
            org_id, workspace_id,
        ) >= 1
    finally:
        await connection.close()

    member_id = await _seed_principal("counter-member-" + uuid4().hex[:8])
    response = await client.post(
        f"/api/v1/organizations/{org_id}/members",
        json={"principal_id": str(member_id), "role_bindings": ["member"]},
        headers=_mutation_headers(csrf_token, "counter-member-1", organization_id=org_id),
    )
    assert response.status_code == 201
    response = await client.delete(
        f"/api/v1/organizations/{org_id}/members/{member_id}",
        headers=_mutation_headers(csrf_token, "counter-member-remove-1", organization_id=org_id),
    )
    assert response.status_code == 204
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM audit_events WHERE organization_id = $1 AND workspace_id IS NULL",
            org_id,
        ) >= 4
        assert await connection.fetchval(
            "SELECT count(*) FROM outbox WHERE organization_id = $1 AND workspace_id IS NULL",
            org_id,
        ) >= 4
    finally:
        await connection.close()


# --------------------------------------------------------------------------- 二轮修复：pre-tenant 免审计边界冻结


@pytest.mark.asyncio
async def test_pre_tenant_no_org_mutation_403_without_audit(
    migrated_database: None, app_and_client: tuple[FastAPI, httpx.AsyncClient, FakeIdP, FakeOPA]
) -> None:
    """预租户边界冻结：actor 无 org context 的目标 mutation → 403、gate 不求值、零审计。

    与 bootstrap-OPA-deny 例外同源（§3.1.9：目标租户不存在时任何 scope 都无合法
    FK audit 落点）：本路径在 gate 求值前被 router 拒绝（organization context
    required），OPA 不得被调用、不得写任何审计/outbox 行。该「免审计」边界只允许
    存在于 pre-tenant 路径；tenant deny（actor 有 org）必须写审计
    （test_opa_deny_blocks_mutation_and_writes_denied_audit），禁止静默扩大。
    """
    _, client, idp, opa = app_and_client
    await _reset_alice()
    await _perform_login(client, idp)
    csrf_token = await _csrf_token(client)
    org_a = await _seed_org()
    guessed = uuid4()

    response = await client.post(
        f"/api/v1/organizations/{org_a}/workspaces",
        json={"workspace_id": str(guessed), "name": "pre-tenant"},
        headers={
            "X-CSRF-Token": csrf_token,
            "Origin": "https://test",
            "Idempotency-Key": "pre-tenant-1",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "organization context required"
    assert len(opa.inputs) == 0, "gate 未求值：OPA 不得被调用"

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert (
            await connection.fetchval("SELECT count(*) FROM workspaces WHERE id = $1", guessed)
            == 0
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM audit_events WHERE resource_id = $1", guessed)
            == 0
        ), "pre-tenant 403 不得写审计行"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM outbox WHERE payload->>'resource_id' = $1", str(guessed)
            )
            == 0
        ), "pre-tenant 403 不得写 outbox 行"
    finally:
        await connection.close()
