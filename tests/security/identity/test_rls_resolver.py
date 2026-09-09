"""membership/TTL resolver 的 RLS 窄旁路契约（A 档，产品化窗口 2026-09-08 冻结）。

缺陷背景（见 docs/handoffs/s11-followup-github-and-live-evidence.md 批次 C）：
`zhiwei_principal_memberships` / `zhiwei_ttl_sweep_targets` 是 SECURITY DEFINER
（owner=zhiwei_migrator），但 19 张租户表 FORCE RLS 对非超级用户 definer 同样生效
（RLS 豁免只看 current_user 的 BYPASSRLS/超级用户属性，SECURITY DEFINER 不改变
这一点）。CI 测试栈的 zhiwei_migrator 是引导超级用户，故既有套件不可见该缺陷；
产品姿态（init-local-product.sh 的普通 migrator 角色）下两个函数恒返回空——
登录后组织列表为空、TTL sweep 永不清理。

修复契约（dispatcher 窄 BYPASSRLS 先例的函数级收窄版）：
- 专用角色 zhiwei_rls_resolver：NOLOGIN（不可建立会话）+ BYPASSRLS（跨租户
  resolver 的本质要求）；
- 两个函数 OWNER 转移到该角色（SECURITY DEFINER 以 owner 执行，旁路只随函数体
  生效；PG 禁止在 SECURITY DEFINER 函数内 SET role，所有权转移是唯一机制；
  owner 仍为非超级用户，表所有权不变）；
- resolver 的表权限 = 恰好函数体读取的 4 张表的 SELECT（memberships /
  workspace_memberships / organizations / memory_records），无其余表、无写权限；
- EXECUTE 授权面不变：memberships→zhiwei_identity、ttl→zhiwei_app、
  PUBLIC 一律 REVOKE；
- 行为面：以真实 invoker 角色调用，返回该 principal 的行（membership）与过期行
  （ttl）；resolver 不拥有任何表（保持 catalog 纪律）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
IDENTITY_DSN = os.environ.get(
    "ZHIWEI_TEST_IDENTITY_DSN", "postgresql://zhiwei_identity@127.0.0.1:55432/zhiwei_test"
)

RESOLVER_ROLE = "zhiwei_rls_resolver"
MEMBERSHIP_FUNCTION = "zhiwei_principal_memberships"
TTL_FUNCTION = "zhiwei_ttl_sweep_targets"
RESOLVER_TABLES = {
    "memberships": "SELECT",
    "workspace_memberships": "SELECT",
    "organizations": "SELECT",
    "memory_records": "SELECT",
}


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
            raise RuntimeError("connected database identity is not the dedicated migration test target")
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[None]:
    """从 base 重建到 head；本文件的函数/角色断言都作用在该专用库上。"""
    asyncio.run(_assert_safe_test_database(ADMIN_DSN))
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


async def _fetch_function_meta(function: str, argtype: str) -> dict:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await connection.fetchrow(
            "SELECT p.proname::text AS proname, p.prosecdef, p.proowner::regrole::text AS owner, "
            "p.proconfig, p.proacl "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.oid = $1::regprocedure",
            f"public.{function}({argtype})",
        )
        assert row is not None, f"{function}({argtype}) 不存在"
        return dict(row)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_resolver_functions_owned_by_bypass_role(migrated_database: None) -> None:
    """两个函数必须 owner=zhiwei_rls_resolver（NOLOGIN BYPASSRLS）：SECURITY DEFINER
    以 owner 执行，旁路只随该窄角色生效——PG 禁止在 SECURITY DEFINER 函数内
    SET role，所有权转移是唯一能表达「函数期旁路」的机制。owner=非超级用户，
    表所有权不变；其余函数（如 zhiwei_principal_snapshot）不受影响。"""
    for function, argtype in (
        (MEMBERSHIP_FUNCTION, "uuid"),
        (TTL_FUNCTION, "timestamptz"),
    ):
        meta = await _fetch_function_meta(function, argtype)
        assert meta["prosecdef"] is True, f"{function}: 必须保持 SECURITY DEFINER"
        assert meta["owner"] == RESOLVER_ROLE, (
            f"{function}: owner 必须是 {RESOLVER_ROLE}，实际 {meta['owner']!r}"
        )
        config = meta["proconfig"] or []
        assert any("search_path" in entry for entry in config), f"{function}: search_path 漂移"


@pytest.mark.asyncio
async def test_resolver_role_is_nologin_bypassrls(migrated_database: None) -> None:
    """resolver 角色：NOLOGIN + BYPASSRLS + 非超级用户（窄旁路，dispatcher 先例）。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        role = await connection.fetchrow(
            "SELECT rolcanlogin, rolbypassrls, rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = $1",
            RESOLVER_ROLE,
        )
        assert role is not None, f"角色 {RESOLVER_ROLE} 未供给（init 脚本必须创建）"
        assert role["rolcanlogin"] is False, "resolver 不得建立会话"
        assert role["rolbypassrls"] is True, "resolver 必须带 BYPASSRLS（跨租户 resolver 本质要求）"
        assert role["rolsuper"] is False and role["rolcreatedb"] is False and role["rolcreaterole"] is False
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_resolver_table_grants_are_exactly_function_reads(migrated_database: None) -> None:
    """resolver 的表权限 = 恰好 4 张函数体读取表的 SELECT；无写权限、无其余表。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = $1",
            RESOLVER_ROLE,
        )
        grants = {(r["table_name"], r["privilege_type"]) for r in rows}
        assert grants == set(RESOLVER_TABLES.items()), f"resolver 表权限漂移: {sorted(grants)}"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_execute_grant_surface_unchanged(migrated_database: None) -> None:
    """EXECUTE 授权面：memberships→{zhiwei_identity, zhiwei_app}（单库 CI 姿态
    由 zhiwei_identity 调用；分库姿态下 membership 数据在业务库，0027 起由
    zhiwei_app 经业务引擎调用）、ttl→zhiwei_app、PUBLIC 一律 REVOKE。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            "SELECT p.proname::text AS proname, r.rolname AS grantee FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN LATERAL aclexplode(p.proacl) g ON true "
            "JOIN pg_roles r ON r.oid = g.grantee "
            "WHERE n.nspname = 'public' AND p.proname IN ($1::name, $2::name)",
            MEMBERSHIP_FUNCTION,
            TTL_FUNCTION,
        )
        surface = {(r["proname"], r["grantee"]) for r in rows if r["grantee"] != "-"}
        assert ("zhiwei_principal_memberships", "zhiwei_identity") in surface
        assert ("zhiwei_principal_memberships", "zhiwei_app") in surface
        assert ("zhiwei_ttl_sweep_targets", "zhiwei_app") in surface
        assert not any(g == "PUBLIC" for _, g in surface), f"PUBLIC 不得有 EXECUTE: {sorted(surface)}"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_membership_resolver_returns_rows_as_identity_invoker(
    migrated_database: None,
) -> None:
    """行为面：以真实 invoker（zhiwei_identity）调用，返回该 principal 的组织/工作区
    摘要——产品姿态（非超级用户 definer + FORCE RLS）下的缺陷在 CI 超级用户库上
    不可复现，本断言是 GREEN 后的回归护栏。"""
    principal_id = uuid4()
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
    finally:
        await connection.close()
    identity = await asyncpg.connect(IDENTITY_DSN)
    try:
        rows = await identity.fetch("SELECT * FROM zhiwei_principal_memberships($1)", principal_id)
        scopes = {(r["scope"], r["organization_id"]) for r in rows}
        assert ("organization", org_a) in scopes and ("organization", org_b) in scopes, (
            f"membership resolver 必须返回两个组织摘要: {scopes}"
        )
        assert ("workspace", org_a) in scopes
        other = await identity.fetch("SELECT * FROM zhiwei_principal_memberships($1)", uuid4())
        assert other == [], "无绑定 principal 不得返回任何行"
    finally:
        await identity.close()
    # TTL resolver 的行为面不在此重复：同一旁路机制（同一角色、同一所有权转移），
    # 由 tests/integration/memory/test_ttl_sweep.py 的仓储层种子用例覆盖（memory_records
    # 有 20+ 必填列与 guard 校验，SQL 直插无法构造合法行）。
