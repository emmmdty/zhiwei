"""S1-T2 RED：identity-global 独立数据库角色与 SECURITY DEFINER 窄接口。

设计/验收方冻结（A 档）：
- zhiwei_app 对 principals / external_identities / auth_sessions / oidc_login_attempts /
  secret_envelopes 的直接访问一律 insufficient privilege；
- zhiwei_identity 能完成 OIDC/session/secret/principal 操作，但直接访问 tenant 表失败；
- zhiwei_app 只能调用 principal snapshot 窄函数（kind/status 等最小字段）；
- zhiwei_identity 只能通过 membership resolver 获得指定 principal 的组织摘要；
- 两个函数必须 SECURITY DEFINER、owner=zhiwei_migrator、固定 search_path、REVOKE EXECUTE
  FROM PUBLIC、分别授予明确角色；无任意 SQL / 全表导出 / token 读取接口；
- 本文件只做角色与函数结构断言；T1 API 在撤销权限后继续通过由 test_memberships.py 覆盖。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

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

IDENTITY_GLOBAL_TABLES = (
    "principals",
    "external_identities",
    "auth_sessions",
    "oidc_login_attempts",
    "secret_envelopes",
)
TENANT_TABLES = (
    "organizations",
    "workspaces",
    "memberships",
    "workspace_memberships",
    "groups",
    "group_members",
)

SNAPSHOT_FUNCTION = "zhiwei_principal_snapshot"
MEMBERSHIP_FUNCTION = "zhiwei_principal_memberships"


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
    """从 base 重建到 head（GREEN 后含 0003）；供本文件所有用例使用。"""
    asyncio.run(_assert_safe_test_database(ADMIN_DSN))
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


async def _seed_identity_rows(principal_id: object) -> tuple[UUID, UUID, UUID]:
    """用 admin 连接预置一个 user principal 及其在两个组织中的 membership。"""
    org_a, org_b, workspace_a = uuid4(), uuid4(), uuid4()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1), ($2, 'active', 1)",
            org_a,
            org_b,
        )
        await connection.execute(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) VALUES ($1, $2, 'sales', 1)",
            workspace_a,
            org_a,
        )
        await connection.execute(
            "INSERT INTO principals (id, kind, status, schema_version) VALUES ($1, 'user', 'active', 1)",
            principal_id,
        )
        await connection.execute(
            "INSERT INTO external_identities (issuer, subject, principal_id) "
            "VALUES ('https://idp.example.com', 'alice', $1)",
            principal_id,
        )
        await connection.execute(
            "INSERT INTO memberships (principal_id, organization_id, role_bindings) "
            "VALUES ($1, $2, '[\"member\"]'::jsonb), ($1, $3, '[\"member\"]'::jsonb)",
            principal_id,
            org_a,
            org_b,
        )
        await connection.execute(
            "INSERT INTO workspace_memberships (principal_id, organization_id, workspace_id, role_bindings) "
            "VALUES ($1, $2, $3, '[\"builder\"]'::jsonb)",
            principal_id,
            org_a,
            workspace_a,
        )
        return org_a, org_b, workspace_a
    finally:
        await connection.close()


# --------------------------------------------------------------------------- zhiwei_app 撤销


@pytest.mark.asyncio
async def test_zhiwei_app_has_no_direct_access_to_identity_global_tables(
    migrated_database: None,
) -> None:
    connection = await asyncpg.connect(APP_DSN)
    try:
        for table in IDENTITY_GLOBAL_TABLES:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetchval(f'SELECT count(*) FROM "{table}"')
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    f'INSERT INTO "{table}" (id) VALUES ($1)',
                    uuid4(),
                )
    finally:
        await connection.close()


# --------------------------------------------------------------------------- zhiwei_identity 最小权限


@pytest.mark.asyncio
async def test_zhiwei_identity_can_do_identity_global_operations(
    migrated_database: None,
) -> None:
    connection = await asyncpg.connect(IDENTITY_DSN)
    principal_id = uuid4()
    try:
        await connection.execute(
            "INSERT INTO principals (id, kind, status, schema_version) VALUES ($1, 'user', 'active', 1)",
            principal_id,
        )
        assert await connection.fetchval("SELECT status FROM principals WHERE id = $1", principal_id) == "active"
        await connection.execute(
            "INSERT INTO external_identities (issuer, subject, principal_id) "
            "VALUES ('https://idp.example.com', 'alice', $1)",
            principal_id,
        )
        assert await connection.fetchval(
            "SELECT principal_id FROM external_identities "
            "WHERE issuer = 'https://idp.example.com' AND subject = 'alice'"
        ) == principal_id
        session_id = uuid4()
        await connection.execute(
            "INSERT INTO auth_sessions "
            "(id, cookie_token_hash, principal_id, issuer, subject, encrypted_token_ref, "
            " csrf_hash, expires_at, idle_expires_at, version, refresh_state, schema_version) "
            "VALUES ($1, 'a' || repeat('0', 63), $2, 'https://idp.example.com', 'alice', "
            "        'session:ref', 'b' || repeat('0', 63), "
            "        now() + interval '1 hour', now() + interval '30 minutes', 1, 'idle', 1)",
            session_id,
            principal_id,
        )
        assert await connection.fetchval(
            "SELECT cookie_token_hash FROM auth_sessions WHERE id = $1", session_id
        ) == "a" + "0" * 63
        await connection.execute(
            "INSERT INTO secret_envelopes "
            "(ref, purpose, version, envelope_version, key_id, key_version, "
            " data_nonce, wrapped_dek, wrap_nonce, ciphertext, schema_version) "
            "VALUES ('session:ref', 'oidc_session', 1, 1, 'k1', 1, "
            "        '\\x01'::bytea, '\\x02'::bytea, '\\x03'::bytea, '\\x04'::bytea, 1)"
        )
        assert await connection.fetchval(
            "SELECT purpose FROM secret_envelopes WHERE ref = 'session:ref'"
        ) == "oidc_session"
        await connection.execute(
            "INSERT INTO oidc_login_attempts "
            "(id, state_hash, nonce_hash, code_verifier, issuer, redirect_uri, "
            " expires_at, schema_version) "
            "VALUES ($1, 'c' || repeat('0', 63), 'd' || repeat('0', 63), 'verifier', "
            "        'https://idp.example.com', 'https://app.example.com/auth/callback', "
            "        now() + interval '10 minutes', 1)",
            uuid4(),
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_zhiwei_identity_has_no_direct_access_to_tenant_tables(
    migrated_database: None,
) -> None:
    connection = await asyncpg.connect(IDENTITY_DSN)
    try:
        for table in TENANT_TABLES:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetchval(f'SELECT count(*) FROM "{table}"')
    finally:
        await connection.close()


# --------------------------------------------------------------------------- SECURITY DEFINER 窄函数


async def _fetch_function_metadata(function_name: str) -> dict:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await connection.fetchrow(
            """
            SELECT p.proname, p.prosecdef, p.proowner::regrole::text AS owner,
                   p.proconfig, p.proacl, n.nspname
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = $1
            """,
            function_name,
        )
        return dict(row) if row is not None else {}
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_principal_snapshot_function_structure(migrated_database: None) -> None:
    meta = await _fetch_function_metadata(SNAPSHOT_FUNCTION)
    assert meta, f"{SNAPSHOT_FUNCTION} 不存在"
    assert meta["owner"] == "zhiwei_migrator"
    assert meta["prosecdef"] is True
    config = meta["proconfig"]
    assert config is not None, "必须固定 search_path"
    assert any("search_path" in entry for entry in config)
    assert meta["proacl"] is not None, "必须 REVOKE EXECUTE FROM PUBLIC（proacl 非空）"
    acl = " ".join(meta["proacl"])
    assert "zhiwei_app" in acl
    assert "zhiwei_identity" not in acl


@pytest.mark.asyncio
async def test_membership_resolver_function_structure(migrated_database: None) -> None:
    meta = await _fetch_function_metadata(MEMBERSHIP_FUNCTION)
    assert meta, f"{MEMBERSHIP_FUNCTION} 不存在"
    assert meta["owner"] == "zhiwei_migrator"
    assert meta["prosecdef"] is True
    config = meta["proconfig"]
    assert config is not None, "必须固定 search_path"
    assert any("search_path" in entry for entry in config)
    assert meta["proacl"] is not None, "必须 REVOKE EXECUTE FROM PUBLIC（proacl 非空）"
    acl = " ".join(meta["proacl"])
    assert "zhiwei_identity" in acl
    assert "zhiwei_app" not in acl


@pytest.mark.asyncio
async def test_execute_grants_are_per_role(migrated_database: None) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT has_function_privilege('zhiwei_app', 'public.zhiwei_principal_snapshot(uuid)', 'EXECUTE')"
        ) is True
        assert await connection.fetchval(
            "SELECT has_function_privilege('zhiwei_identity', 'public.zhiwei_principal_snapshot(uuid)', 'EXECUTE')"
        ) is False
        assert await connection.fetchval(
            "SELECT has_function_privilege('zhiwei_identity', 'public.zhiwei_principal_memberships(uuid)', 'EXECUTE')"
        ) is True
        assert await connection.fetchval(
            "SELECT has_function_privilege('zhiwei_app', 'public.zhiwei_principal_memberships(uuid)', 'EXECUTE')"
        ) is False
        assert await connection.fetchval(
            "SELECT has_function_privilege('PUBLIC', 'public.zhiwei_principal_snapshot(uuid)', 'EXECUTE')"
        ) is False
        assert await connection.fetchval(
            "SELECT has_function_privilege('PUBLIC', 'public.zhiwei_principal_memberships(uuid)', 'EXECUTE')"
        ) is False
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_principal_snapshot_returns_minimal_fields_to_app_role(
    migrated_database: None,
) -> None:
    principal_id = uuid4()
    await _seed_identity_rows(principal_id)
    connection = await asyncpg.connect(APP_DSN)
    try:
        row = await connection.fetchrow(
            f"SELECT * FROM public.{SNAPSHOT_FUNCTION}($1)", principal_id
        )
        assert row is not None
        assert set(row.keys()) == {"id", "kind", "status", "schema_version", "created_at"}
        assert row["kind"] == "user"
        assert row["status"] == "active"
        assert row["schema_version"] == 1
        missing = await connection.fetchrow(
            f"SELECT * FROM public.{SNAPSHOT_FUNCTION}($1)", uuid4()
        )
        assert missing is None
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_identity_role_cannot_call_snapshot_function(migrated_database: None) -> None:
    principal_id = uuid4()
    await _seed_identity_rows(principal_id)
    connection = await asyncpg.connect(IDENTITY_DSN)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchrow(
                f"SELECT * FROM public.{SNAPSHOT_FUNCTION}($1)", principal_id
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_membership_resolver_returns_only_that_principal_orgs(
    migrated_database: None,
) -> None:
    principal_id = uuid4()
    org_a, org_b, workspace_a = await _seed_identity_rows(principal_id)
    connection = await asyncpg.connect(IDENTITY_DSN)
    try:
        rows = await connection.fetch(
            f"SELECT scope, organization_id, workspace_id, role_bindings, organization_status "
            f"FROM public.{MEMBERSHIP_FUNCTION}($1) ORDER BY scope, organization_id",
            principal_id,
        )
        org_rows = {(row["scope"], row["organization_id"], row["workspace_id"]) for row in rows}
        assert (org_a, None) in {(r[1], r[2]) for r in org_rows}
        assert (org_b, None) in {(r[1], r[2]) for r in org_rows}
        assert (workspace_a,) in {(r[2],) for r in org_rows}
        for row in rows:
            assert row["organization_status"] == "active"
            assert row["role_bindings"] is not None
        other_principal = uuid4()
        empty = await connection.fetch(
            f"SELECT * FROM public.{MEMBERSHIP_FUNCTION}($1)", other_principal
        )
        assert empty == []
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_app_role_cannot_call_membership_resolver(migrated_database: None) -> None:
    principal_id = uuid4()
    await _seed_identity_rows(principal_id)
    connection = await asyncpg.connect(APP_DSN)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(
                f"SELECT * FROM public.{MEMBERSHIP_FUNCTION}($1)", principal_id
            )
    finally:
        await connection.close()
