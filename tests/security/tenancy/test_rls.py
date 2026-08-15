"""S1-T4 RED：FORCE RLS 目录、角色属性与租户隔离行为矩阵。

设计/验收方冻结（A 档），行为当前已实现，本文件是 green-at-RED 回归护栏：
- 19 张 tenant 表全部 relrowsecurity + relforcerowsecurity=true、owner=zhiwei_migrator，
  owner 不得是 zhiwei_app；5 张 identity-global 表无 RLS（无 org GUC 语义）；
- zhiwei_app：rolsuper=false / rolbypassrls=false / rolcreaterole=false / rolinherit=false，
  不是 zhiwei_migrator / zhiwei_identity 的成员，不能 SET ROLE 二者，也不能
  SET SESSION AUTHORIZATION（均 'permission denied'）；
- 无 GUC：SELECT 恒 0 行；INSERT 报 row-level security 错误；UPDATE 0 行；
  有 DELETE 授权的表 DELETE 0 行，无 DELETE 授权的表 insufficient privilege；
- 仅 org GUC：只可见该 org 的 organizations / workspaces / memberships；
  workspaces 在 org-only 上下文必须列出该 org 全部 workspace（org 级 list_workspaces 依赖）；
- org+workspace GUC：groups / workspace_memberships / group_members 需要匹配的 org+ws 对，
  错配对（org1+ws2）与仅 ws GUC 一律 0 行，错配对 INSERT 被 RLS 拒绝；
- RLS 对 JOIN / 子查询 / 聚合 / ORDER BY LIMIT OFFSET / UNION ALL 一视同仁；
- identity-global 表（principals / external_identities）由 zhiwei_identity 无 GUC 可读。
pool 复用与事务 GUC 生命周期由 test_pool.py 覆盖。
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

# 冻结契约：19 张 tenant 表 + 5 张 identity-global 表（与 migrations/versions/0001~0003 一致）
TENANT_TABLES = (
    "organizations",
    "workspaces",
    "agent_definitions",
    "agent_versions",
    "runs",
    "canonical_events",
    "canonical_projections",
    "artifact_manifests",
    "dataset_versions",
    "eval_suite_versions",
    "eval_runs",
    "eval_samples",
    "idempotency_records",
    "audit_events",
    "outbox",
    "memberships",
    "workspace_memberships",
    "groups",
    "group_members",
)
IDENTITY_GLOBAL_TABLES = (
    "principals",
    "external_identities",
    "auth_sessions",
    "oidc_login_attempts",
    "secret_envelopes",
)

# 可猜测的固定 UUID：契约要求 ID 可预测——被跨租户引用时必须查无此号
ORG_1 = UUID("11111111-1111-1111-1111-111111111111")
ORG_2 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
WS_1 = UUID("11111111-1111-1111-1111-222222222222")
WS_2 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-bbbbbbbbbbbb")
PRINCIPAL_1 = UUID("11111111-1111-1111-1111-333333333333")
PRINCIPAL_2 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-cccccccccccc")
GROUP_1 = UUID("11111111-1111-1111-1111-444444444444")
GROUP_2 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-dddddddddddd")


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
    """从 base 重建到 head；仅供本文件使用，不依赖任何测试顺序。"""
    asyncio.run(_assert_safe_test_database(ADMIN_DSN))
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


async def _seed_tenancy() -> None:
    """migrator 预置双组织 fixture：org1/org2、同名 'sales' workspace、memberships、
    workspace_memberships、groups 与 group_members；幂等——先清掉本 fixture 的已知 ID 再插入。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "DELETE FROM group_members WHERE group_id = ANY($1::uuid[])", [GROUP_1, GROUP_2]
        )
        await connection.execute(
            "DELETE FROM groups WHERE id = ANY($1::uuid[])", [GROUP_1, GROUP_2]
        )
        await connection.execute(
            "DELETE FROM workspace_memberships "
            "WHERE workspace_id = ANY($1::uuid[]) OR principal_id = ANY($2::uuid[])",
            [WS_1, WS_2],
            [PRINCIPAL_1, PRINCIPAL_2],
        )
        await connection.execute(
            "DELETE FROM memberships "
            "WHERE organization_id = ANY($1::uuid[]) OR principal_id = ANY($2::uuid[])",
            [ORG_1, ORG_2],
            [PRINCIPAL_1, PRINCIPAL_2],
        )
        await connection.execute(
            "DELETE FROM workspaces WHERE id = ANY($1::uuid[])", [WS_1, WS_2]
        )
        await connection.execute(
            "DELETE FROM external_identities WHERE principal_id = ANY($1::uuid[])",
            [PRINCIPAL_1, PRINCIPAL_2],
        )
        await connection.execute(
            "DELETE FROM organizations WHERE id = ANY($1::uuid[])", [ORG_1, ORG_2]
        )
        await connection.execute(
            "DELETE FROM principals WHERE id = ANY($1::uuid[])", [PRINCIPAL_1, PRINCIPAL_2]
        )

        await connection.execute(
            "INSERT INTO principals (id, kind, status, schema_version) "
            "VALUES ($1, 'user', 'active', 1), ($2, 'user', 'active', 1)",
            PRINCIPAL_1,
            PRINCIPAL_2,
        )
        await connection.execute(
            "INSERT INTO external_identities (issuer, subject, principal_id) "
            "VALUES ('https://idp.example.com', 'alice-guessed', $1), "
            "       ('https://idp.example.com', 'bob-guessed', $2)",
            PRINCIPAL_1,
            PRINCIPAL_2,
        )
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) "
            "VALUES ($1, 'active', 1), ($2, 'active', 1)",
            ORG_1,
            ORG_2,
        )
        # 两个 org 用同名 workspace（'sales'）——契约要求重叠名字必须不泄露
        await connection.execute(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) "
            "VALUES ($1, $2, 'sales', 1), ($3, $4, 'sales', 1)",
            WS_1,
            ORG_1,
            WS_2,
            ORG_2,
        )
        await connection.execute(
            "INSERT INTO memberships (principal_id, organization_id, role_bindings) "
            "VALUES ($1, $2, '[\"member\"]'::jsonb), ($3, $4, '[\"member\"]'::jsonb)",
            PRINCIPAL_1,
            ORG_1,
            PRINCIPAL_2,
            ORG_2,
        )
        await connection.execute(
            "INSERT INTO workspace_memberships "
            "(principal_id, organization_id, workspace_id, role_bindings) "
            "VALUES ($1, $2, $3, '[\"builder\"]'::jsonb), "
            "       ($4, $5, $6, '[\"builder\"]'::jsonb)",
            PRINCIPAL_1,
            ORG_1,
            WS_1,
            PRINCIPAL_2,
            ORG_2,
            WS_2,
        )
        await connection.execute(
            "INSERT INTO groups (id, organization_id, workspace_id, name, schema_version) "
            "VALUES ($1, $2, $3, 'sales-team', 1), ($4, $5, $6, 'sales-team', 1)",
            GROUP_1,
            ORG_1,
            WS_1,
            GROUP_2,
            ORG_2,
            WS_2,
        )
        await connection.execute(
            "INSERT INTO group_members (group_id, organization_id, workspace_id, principal_id) "
            "VALUES ($1, $2, $3, $4), ($5, $6, $7, $8)",
            GROUP_1,
            ORG_1,
            WS_1,
            PRINCIPAL_1,
            GROUP_2,
            ORG_2,
            WS_2,
            PRINCIPAL_2,
        )
    finally:
        await connection.close()


# --------------------------------------------------------------------------- 目录与角色

# 契约 1：tenant 表 FORCE RLS + owner=zhiwei_migrator
@pytest.mark.asyncio
async def test_every_tenant_table_is_force_rls_and_migrator_owned(
    migrated_database: None,
) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   pg_get_userbyid(c.relowner) AS owner
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
            """,
            list(TENANT_TABLES),
        )
        by_name = {row["relname"]: row for row in rows}
        assert set(by_name) == set(TENANT_TABLES)
        for table in TENANT_TABLES:
            row = by_name[table]
            assert row["relrowsecurity"] is True, f"{table} 未启用 RLS"
            assert row["relforcerowsecurity"] is True, f"{table} 未 FORCE RLS"
            assert row["owner"] == "zhiwei_migrator", f"{table} owner 不是 zhiwei_migrator"
            assert row["owner"] != "zhiwei_app", f"{table} owner 不得是 zhiwei_app"
    finally:
        await connection.close()


# 契约 2：identity-global 表无 RLS（不套用 org GUC）
@pytest.mark.asyncio
async def test_identity_global_tables_have_no_row_level_security(
    migrated_database: None,
) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
            """,
            list(IDENTITY_GLOBAL_TABLES),
        )
        by_name = {row["relname"]: row for row in rows}
        assert set(by_name) == set(IDENTITY_GLOBAL_TABLES)
        for table in IDENTITY_GLOBAL_TABLES:
            assert by_name[table]["relrowsecurity"] is False, f"{table} 不得启用 RLS"
            assert by_name[table]["relforcerowsecurity"] is False, f"{table} 不得 FORCE RLS"
    finally:
        await connection.close()


# 契约 3 + 4 的目录部分：角色属性与成员关系
@pytest.mark.asyncio
async def test_app_and_identity_roles_are_unprivileged(
    migrated_database: None,
) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        for role_name in ("zhiwei_app", "zhiwei_identity"):
            role = await connection.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = $1",
                role_name,
            )
            assert role is not None, f"角色 {role_name} 不存在"
            assert role["rolsuper"] is False
            assert role["rolbypassrls"] is False

        app_role = await connection.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolinherit "
            "FROM pg_catalog.pg_roles WHERE rolname = 'zhiwei_app'"
        )
        assert app_role is not None
        assert app_role["rolsuper"] is False
        assert app_role["rolbypassrls"] is False
        assert app_role["rolcreaterole"] is False
        assert app_role["rolinherit"] is False

        # zhiwei_app 不是任何角色的成员（SET ROLE 的前提是成员关系）
        assert await connection.fetchval(
            "SELECT count(*) FROM pg_catalog.pg_auth_members "
            "WHERE member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = 'zhiwei_app')"
        ) == 0
    finally:
        await connection.close()


# 契约 4：zhiwei_app 不能提升自身（SET ROLE / SET SESSION AUTHORIZATION）
@pytest.mark.asyncio
async def test_app_role_cannot_set_role_or_session_authorization(
    migrated_database: None,
) -> None:
    connection = await asyncpg.connect(APP_DSN)
    try:
        for target in ("zhiwei_migrator", "zhiwei_identity"):
            with pytest.raises(asyncpg.PostgresError, match="permission denied"):
                await connection.execute(f"SET ROLE {target}")
        with pytest.raises(asyncpg.PostgresError, match="permission denied"):
            await connection.execute("SET SESSION AUTHORIZATION zhiwei_migrator")
    finally:
        await connection.close()


# --------------------------------------------------------------------------- 行为矩阵

# 契约 5：无 GUC —— 数据存在但全拒
@pytest.mark.asyncio
async def test_no_tenant_context_denies_every_access_shape(
    migrated_database: None,
) -> None:
    await _seed_tenancy()
    connection = await asyncpg.connect(APP_DSN)
    try:
        for table in ("organizations", "workspaces", "memberships"):
            assert await connection.fetchval(f'SELECT count(*) FROM "{table}"') == 0

        with pytest.raises(asyncpg.PostgresError, match="row-level security"):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO organizations (id, status, schema_version) "
                    "VALUES ($1, 'active', 1) RETURNING id",
                    uuid4(),
                )
        with pytest.raises(asyncpg.PostgresError, match="row-level security"):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO workspaces (id, organization_id, name, schema_version) "
                    "VALUES ($1, $2, 'blocked', 1) RETURNING id",
                    uuid4(),
                    ORG_1,
                )
        with pytest.raises(asyncpg.PostgresError, match="row-level security"):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO memberships (principal_id, organization_id, role_bindings) "
                    "VALUES ($1, $2, '[\"member\"]'::jsonb) RETURNING principal_id",
                    PRINCIPAL_1,
                    ORG_1,
                )

        # UPDATE 授权存在（列级），RLS 过滤后 0 行
        assert (
            await connection.fetchval(
                "UPDATE organizations SET status = 'active' WHERE id = $1 RETURNING id", ORG_1
            )
            is None
        )
        assert (
            await connection.fetchval(
                "UPDATE workspaces SET name = 'blocked' WHERE id = $1 RETURNING id", WS_1
            )
            is None
        )
        # memberships 对 zhiwei_app 无 UPDATE 授权：fail closed 为 insufficient privilege
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "UPDATE memberships SET role_bindings = '[]'::jsonb "
                "WHERE principal_id = $1",
                PRINCIPAL_1,
            )

        # DELETE：有授权的表 0 行（RLS），无授权的表 insufficient privilege
        assert (
            await connection.fetchval(
                "DELETE FROM memberships WHERE principal_id = $1 RETURNING principal_id",
                PRINCIPAL_1,
            )
            is None
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "DELETE FROM organizations WHERE id = $1", ORG_1
            )
    finally:
        await connection.close()


# 契约 6：仅 org GUC —— 只可见该 org；workspaces 列表必须返回该 org 全部 workspace
@pytest.mark.asyncio
async def test_org_only_context_reveals_only_that_org(
    migrated_database: None,
) -> None:
    await _seed_tenancy()
    connection = await asyncpg.connect(APP_DSN)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('zhiwei.organization_id', $1, true)", str(ORG_1)
            )
            assert await connection.fetchval("SELECT count(*) FROM organizations") == 1
            assert await connection.fetchval("SELECT id FROM organizations WHERE id = $1", ORG_2) is None
            assert await connection.fetchval("SELECT count(*) FROM memberships") == 1
            workspace_ids = {
                row["id"] for row in await connection.fetch("SELECT id FROM workspaces")
            }
            assert workspace_ids == {WS_1}
    finally:
        await connection.close()


# 契约 7：workspace 级表需要匹配的 org+ws 对
@pytest.mark.asyncio
async def test_workspace_context_requires_matching_org_workspace_pair(
    migrated_database: None,
) -> None:
    await _seed_tenancy()
    connection = await asyncpg.connect(APP_DSN)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('zhiwei.organization_id', $1, true)", str(ORG_1)
            )
            await connection.execute(
                "SELECT set_config('zhiwei.workspace_id', $1, true)", str(WS_1)
            )
            assert {
                row["id"] for row in await connection.fetch("SELECT id FROM groups")
            } == {GROUP_1}
            assert await connection.fetchval("SELECT count(*) FROM workspace_memberships") == 1
            assert await connection.fetchval("SELECT count(*) FROM group_members") == 1
            assert {
                row["id"] for row in await connection.fetch("SELECT id FROM workspaces")
            } == {WS_1}

        # 错配对 org1+ws2：四个表全部 0 行（workspaces 的 policy 也要求 org 匹配）
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('zhiwei.organization_id', $1, true)", str(ORG_1)
            )
            await connection.execute(
                "SELECT set_config('zhiwei.workspace_id', $1, true)", str(WS_2)
            )
            for table in ("groups", "workspace_memberships", "group_members", "workspaces"):
                assert await connection.fetchval(f'SELECT count(*) FROM "{table}"') == 0

        # 仅 ws2 GUC（无 org）：全部 0 行——workspace 级 policy 必须 org+ws 齐备
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('zhiwei.workspace_id', $1, true)", str(WS_2)
            )
            for table in ("groups", "workspace_memberships", "group_members", "workspaces"):
                assert await connection.fetchval(f'SELECT count(*) FROM "{table}"') == 0

        # 错配对 INSERT：org1+ws1 上下文里写 org2/ws2 的 group 行 → RLS 拒绝
        # （org2/ws2 是合法外键组合，唯一失败点就是 WITH CHECK 的 org 不匹配）
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('zhiwei.organization_id', $1, true)", str(ORG_1)
            )
            await connection.execute(
                "SELECT set_config('zhiwei.workspace_id', $1, true)", str(WS_1)
            )
            with pytest.raises(asyncpg.PostgresError, match="row-level security"):
                await connection.execute(
                    "INSERT INTO groups (id, organization_id, workspace_id, name, schema_version) "
                    "VALUES ($1, $2, $3, 'intruder', 1)",
                    uuid4(),
                    ORG_2,
                    WS_2,
                )
    finally:
        await connection.close()


# 契约 8：RLS 对全部 SQL 形态一视同仁
@pytest.mark.asyncio
async def test_rls_holds_for_join_subquery_aggregate_pagination_union(
    migrated_database: None,
) -> None:
    await _seed_tenancy()
    connection = await asyncpg.connect(APP_DSN)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('zhiwei.organization_id', $1, true)", str(ORG_1)
            )
            # 直接 SELECT
            assert await connection.fetchval(
                "SELECT count(*) FROM workspaces WHERE id = $1", WS_2
            ) == 0
            assert await connection.fetchval(
                "SELECT id FROM organizations WHERE id = $1", ORG_2
            ) is None
            # JOIN
            assert await connection.fetchval(
                "SELECT count(*) FROM workspaces AS w "
                "JOIN organizations AS o ON o.id = w.organization_id "
                "WHERE w.id = $1",
                WS_2,
            ) == 0
            # 子查询
            assert await connection.fetchval(
                "SELECT count(*) FROM workspaces WHERE organization_id IN "
                "(SELECT id FROM organizations WHERE id = $1)",
                ORG_2,
            ) == 0
            # 聚合
            aggregate = await connection.fetchrow(
                "SELECT count(*) AS total, count(DISTINCT id) AS distinct_ids FROM workspaces"
            )
            assert dict(aggregate) == {"total": 1, "distinct_ids": 1}
            # cursor 风格分页：任何一页都不得出现 org2 的 workspace
            first_page = await connection.fetch(
                "SELECT id FROM workspaces ORDER BY id LIMIT 1 OFFSET 0"
            )
            second_page = await connection.fetch(
                "SELECT id FROM workspaces ORDER BY id LIMIT 1 OFFSET 1"
            )
            assert [row["id"] for row in first_page] == [WS_1]
            assert second_page == []
            assert WS_2 not in {row["id"] for row in first_page}
            # UNION ALL
            union_rows = await connection.fetch(
                "SELECT id FROM workspaces WHERE id = $1 "
                "UNION ALL SELECT id FROM workspaces WHERE id = $2",
                WS_1,
                WS_2,
            )
            assert [row["id"] for row in union_rows] == [WS_1]
    finally:
        await connection.close()


# 契约 9：identity-global 表不是租户表——zhiwei_identity 无 GUC 可读
@pytest.mark.asyncio
async def test_identity_global_tables_are_readable_without_tenant_guc(
    migrated_database: None,
) -> None:
    await _seed_tenancy()
    connection = await asyncpg.connect(IDENTITY_DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM principals") == 2
        assert await connection.fetchval(
            "SELECT status FROM principals WHERE id = $1", PRINCIPAL_1
        ) == "active"
        assert await connection.fetchval("SELECT count(*) FROM external_identities") == 2
        assert await connection.fetchval(
            "SELECT principal_id FROM external_identities "
            "WHERE issuer = 'https://idp.example.com' AND subject = 'alice-guessed'"
        ) == PRINCIPAL_1
    finally:
        await connection.close()
