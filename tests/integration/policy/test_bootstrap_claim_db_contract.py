"""S1-T4 四轮 RED：bootstrap claim 的数据库层契约（确定性并发 + 权限 + 迁移 backfill）。

设计/验收方冻结（A 档，四轮任务书 阶段二 B/E + 阶段三 migration 契约）：
- 一个 principal 最多只能 claim 一个由自己 bootstrap 创建的 Organization；membership
  被删除不能重置该资格（TOCTOU/生命周期绕过修复的持久围栏）；
- 新增 identity-global 表 organization_bootstrap_claims（不属于 tenant 数据面：无 RLS、
  无 org/ws 租户作用域语义，不给 app/identity 直接表权限）与窄函数
  zhiwei_claim_organization_bootstrap(principal_id, organization_id) -> boolean：
  * 函数使用 transaction-level advisory lock 按 principal 序列化；UNIQUE 约束是第二层防线；
  * claim 已存在且 target 相同 → true；不同 → false；禁止更新/迁移既有 claim；
  * SECURITY DEFINER（owner=zhiwei_migrator）、search_path=pg_catalog,public、
    无动态 SQL、对象全限定；REVOKE PUBLIC，只 GRANT EXECUTE 给 zhiwei_app；
  * zhiwei_app / zhiwei_identity / PUBLIC 均不能直接 SELECT/INSERT/UPDATE/DELETE claim 表，
    也不能执行该函数（除 zhiwei_app 外）；
- 迁移对历史 `organization.create:<principal>` idempotency 记录做可验证 backfill；
  同一 principal 已有多个不同 bootstrap targets → migration fail closed 并给出明确错误，
  禁止静默选择「第一个」；downgrade/upgrade 可逆；
- 数据库 claim/CAS 只允许一个并发提交：两个独立 transaction 在 OPA 已允许的前提下同时为
  同一 principal 创建不同 target，断言只允许一个提交（不依赖 sleep 或测试速度制造竞态）。

RED 状态（四轮）：organization_bootstrap_claims 表与 zhiwei_claim_organization_bootstrap
函数尚不存在——本文件全部用例在目录查询 / 函数调用点失败，失败原因正是「缺少持久
claim/原子围栏」；migration backfill 实验在 upgrade head 时因 0008 缺失而失败。
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

CLAIM_TABLE = "organization_bootstrap_claims"
CLAIM_FUNCTION = "zhiwei_claim_organization_bootstrap"
CLAIM_FUNCTION_SIGNATURE = "zhiwei_claim_organization_bootstrap(uuid, uuid)"
BOOTSTRAP_SCOPE_PREFIX = "organization.create:"

# 四轮 RED 冻结的 403 detail（router 映射 BootstrapClaimConflict）
CLAIM_CONFLICT_DETAIL = "bootstrap claim already used for another organization"


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


# --------------------------------------------------------------------------- seed / helpers


async def _seed_org(organization_id: UUID) -> None:
    """migrator 预置 organization（superuser 不受 RLS 约束，无需 GUC）。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1)",
            organization_id,
        )
    finally:
        await connection.close()


async def _seed_principal(principal_id: UUID) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO principals (id, kind, status, schema_version) VALUES ($1, 'user', 'active', 1)",
            principal_id,
        )
    finally:
        await connection.close()


async def _claim_via_app(conn: asyncpg.Connection, principal_id: UUID, org_id: UUID) -> bool:
    """以 zhiwei_app 调用窄函数（唯一合法执行者）。"""
    return bool(
        await conn.fetchval(
            f"SELECT public.{CLAIM_FUNCTION}($1, $2)", principal_id, org_id
        )
    )


# --------------------------------------------------------------------------- B. 确定性数据库并发契约


@pytest.mark.asyncio
async def test_db_claim_cas_allows_exactly_one_commit_for_concurrent_different_targets(
    migrated_database: None,
) -> None:
    """两个独立 transaction 同时为同一 principal 创建不同 target：claim/CAS 只允许一个提交。

    不依赖 sleep/测试速度制造竞态：txn1 先持有 principal 级 transaction-level advisory
    lock 并写入 claim（true）；txn2 的并发调用被同一锁串行化——无论其调用落在 txn1 提交
    之前（阻塞）还是之后（读到已提交 claim），都必须返回 false。org_b 随 txn2 回滚，
    终态只有 org_a 一条 claim/一个 committed org。
    """
    principal_id = uuid4()
    org_a = uuid4()
    org_b = uuid4()
    await _seed_principal(principal_id)
    conn1 = await asyncpg.connect(APP_DSN)
    conn2 = await asyncpg.connect(APP_DSN)
    try:
        t1 = conn1.transaction()
        await t1.start()
        t2 = conn2.transaction()
        await t2.start()
        for conn, org_id in ((conn1, org_a), (conn2, org_b)):
            await conn.execute(
                "SELECT set_config('zhiwei.organization_id', $1, true)", str(org_id)
            )
            await conn.execute("SELECT set_config('zhiwei.workspace_id', '', true)")
            await conn.execute(
                "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1)",
                org_id,
            )
        first = await _claim_via_app(conn1, principal_id, org_a)
        assert first is True, "首个 claim 必须成功"

        second_task = asyncio.create_task(_claim_via_app(conn2, principal_id, org_b))
        # 让并发调用有机会进入阻塞态；无论它是否已经启动，结果都确定（见 docstring）。
        await asyncio.sleep(0)
        await t1.commit()
        second = await second_task
        assert second is False, "同一 principal 的第二个不同 target 必须被 claim/CAS 拒绝"
        await t2.rollback()
    finally:
        await conn1.close()
        await conn2.close()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            f"SELECT principal_id, organization_id, schema_version FROM {CLAIM_TABLE}"
        )
        assert len(rows) == 1, "并发创建必须只留下一条 claim"
        assert rows[0]["principal_id"] == principal_id
        assert rows[0]["organization_id"] == org_a
        assert rows[0]["schema_version"] == 1
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_a)
            == 1
        ), "winner target 必须已提交"
        assert (
            await connection.fetchval("SELECT count(*) FROM organizations WHERE id = $1", org_b)
            == 0
        ), "loser target 必须整体回滚，零残留"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_db_claim_same_target_replay_true_and_row_never_updated(
    migrated_database: None,
) -> None:
    """claim 已存在且 target 相同 → true；既有 claim 永不更新/迁移到其他 target。"""
    principal_id = uuid4()
    org_a = uuid4()
    org_b = uuid4()
    await _seed_principal(principal_id)
    await _seed_org(org_a)
    await _seed_org(org_b)

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            f"INSERT INTO {CLAIM_TABLE} "
            "(principal_id, organization_id, schema_version) VALUES ($1, $2, 1)",
            principal_id,
            org_a,
        )
    finally:
        await connection.close()

    conn = await asyncpg.connect(APP_DSN)
    try:
        assert await _claim_via_app(conn, principal_id, org_a) is True
        assert await _claim_via_app(conn, principal_id, org_b) is False
    finally:
        await conn.close()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await connection.fetchrow(
            f"SELECT organization_id, schema_version, created_at FROM {CLAIM_TABLE} "
            "WHERE principal_id = $1",
            principal_id,
        )
        assert row["organization_id"] == org_a, "既有 claim 不得被迁移到其他 target"
        assert row["schema_version"] == 1
    finally:
        await connection.close()


# --------------------------------------------------------------------------- E. 权限：表 ACL / 函数 ACL / catalog 契约


@pytest.mark.asyncio
async def test_claim_table_direct_access_denied_for_all_roles(
    migrated_database: None,
) -> None:
    """zhiwei_app / zhiwei_identity / PUBLIC 均不能直接 SELECT/INSERT/UPDATE/DELETE claim 表。

    表不属于 tenant 数据面（无 RLS、owner=zhiwei_migrator），只允许经 SECURITY DEFINER
    窄函数访问；不给任何角色直接表权限。
    """
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        for role in ("zhiwei_app", "zhiwei_identity", "public"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                granted = await connection.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)", role, CLAIM_TABLE, privilege
                )
                assert granted is False, (
                    f"{role} 不得直接 {privilege} {CLAIM_TABLE}"
                )
        row = await connection.fetchrow(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner) AS owner "
            "FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = $1",
            CLAIM_TABLE,
        )
        assert row is not None, "claim 表必须存在"
        assert row["owner"] == "zhiwei_migrator", "claim 表 owner 必须是 zhiwei_migrator"
        assert row["relrowsecurity"] is False, "claim 表不启用 RLS（不属于 tenant 数据面）"
        assert row["relforcerowsecurity"] is False
    finally:
        await connection.close()

    for dsn in (APP_DSN, IDENTITY_DSN):
        conn = await asyncpg.connect(dsn)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval(f"SELECT count(*) FROM {CLAIM_TABLE}")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    f"INSERT INTO {CLAIM_TABLE} "
                    "(principal_id, organization_id, schema_version) VALUES ($1, $2, 1)",
                    uuid4(),
                    uuid4(),
                )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    f"UPDATE {CLAIM_TABLE} SET schema_version = 2 WHERE principal_id = $1",
                    uuid4(),
                )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    f"DELETE FROM {CLAIM_TABLE} WHERE principal_id = $1", uuid4()
                )
        finally:
            await conn.close()


@pytest.mark.asyncio
async def test_claim_function_execution_restricted_to_zhiwei_app(
    migrated_database: None,
) -> None:
    """只有 zhiwei_app 可执行窄函数；PUBLIC / zhiwei_identity 一律拒绝执行。"""
    principal_id = uuid4()
    org_id = uuid4()
    await _seed_principal(principal_id)
    await _seed_org(org_id)

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        for role, expected in (
            ("zhiwei_app", True),
            ("zhiwei_identity", False),
            ("public", False),
        ):
            granted = await connection.fetchval(
                "SELECT has_function_privilege($1, $2, 'EXECUTE')",
                role,
                CLAIM_FUNCTION_SIGNATURE,
            )
            assert granted is expected, f"{role} 对窄函数的 EXECUTE 必须是 {expected}"
    finally:
        await connection.close()

    conn = await asyncpg.connect(APP_DSN)
    try:
        claimed = await _claim_via_app(conn, principal_id, org_id)
        assert claimed is True, "zhiwei_app 经窄函数 claim 必须成功"
    finally:
        await conn.close()
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await admin.fetchrow(
            f"SELECT organization_id FROM {CLAIM_TABLE} WHERE principal_id = $1",
            principal_id,
        )
        assert row is not None
        assert row["organization_id"] == org_id
    finally:
        await admin.close()

    conn = await asyncpg.connect(IDENTITY_DSN)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await _claim_via_app(conn, principal_id, org_id)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_claim_function_catalog_contract(
    migrated_database: None,
) -> None:
    """窄函数 catalog 契约：owner=migrator、SECURITY DEFINER、search_path=pg_catalog,public、
    无动态 SQL（prosrc 不含 EXECUTE）、返回 boolean、两个 uuid 参数。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await connection.fetchrow(
            "SELECT p.prosecdef, p.prokind, p.proconfig, p.prosrc, p.pronargs, "
            "pg_get_userbyid(p.proowner) AS owner, pg_get_function_result(p.oid) AS result_type "
            "FROM pg_catalog.pg_proc AS p "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = $1",
            CLAIM_FUNCTION,
        )
        assert row is not None, "窄函数必须存在"
        assert row["prosecdef"] is True, "函数必须是 SECURITY DEFINER"
        assert row["prokind"] == b"f", "prokind 是 PostgreSQL 内部 char 类型（asyncpg 以 bytes 返回）"
        assert row["pronargs"] == 2
        assert row["result_type"] == "boolean"
        assert row["owner"] == "zhiwei_migrator", "函数 owner 必须是 zhiwei_migrator"
        assert (row["proconfig"] or []) == [
            "search_path=pg_catalog, public"
        ], "函数必须固定 search_path=pg_catalog,public"
        assert " EXECUTE " not in row["prosrc"], "函数禁止动态 SQL（EXECUTE '...'）"
    finally:
        await connection.close()


# --------------------------------------------------------------------------- 迁移：backfill 与可逆性


async def _seed_bootstrap_idempotency(
    principal_id: UUID, org_id: UUID, *, scope: str | None = None
) -> None:
    """migrator 预置一条历史 `organization.create:<principal>` idempotency 记录。

    scope 可覆盖为任意后缀：malformed 用例预置不可解析的
    `organization.create:<suffix>`（四轮 RED 机制修订新增）。
    """
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO idempotency_records "
            "(id, organization_id, workspace_id, scope, idempotency_key, request_digest, "
            "response, status, schema_version, created_at) "
            "VALUES ($1, $2, NULL, $3, $4, $5, $6::jsonb, 'completed', 1, now())",
            uuid4(),
            org_id,
            scope or f"{BOOTSTRAP_SCOPE_PREFIX}{principal_id}",
            f"historical-{uuid4().hex}",
            f"sha256:{'1' * 64}",
            '{"id": "org", "status": "active"}',
        )
    finally:
        await connection.close()


async def _cleanup_bootstrap_records(scope: str) -> None:
    """歧义/malformed 用例收尾：清除预置的幂等记录与对应 org，恢复到可继续 upgrade 的状态。

    失败的迁移在事务内整体回滚（transactional DDL），claim 表此时不存在——只清理
    幂等记录与 org，不触碰 claim 表。
    """
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute("DELETE FROM idempotency_records WHERE scope = $1", scope)
        await connection.execute(
            "DELETE FROM organizations WHERE id NOT IN (SELECT organization_id FROM "
            "idempotency_records UNION SELECT organization_id FROM memberships UNION "
            "SELECT organization_id FROM workspaces)"
        )
    finally:
        await connection.close()


def test_backfill_creates_claims_from_historical_idempotency_records(
    migrated_database: None,
) -> None:
    """0007 → head：历史 `organization.create:<principal>` 幂等记录 backfill 为 claim。"""
    config = _alembic_config()
    command.downgrade(config, "0007_audit_metadata_nonempty")
    principal_id = uuid4()
    org_id = uuid4()
    asyncio.run(_seed_principal(principal_id))
    asyncio.run(_seed_org(org_id))
    asyncio.run(_seed_bootstrap_idempotency(principal_id, org_id))
    command.upgrade(config, "head")

    asyncio.run(_verify_backfill(principal_id, org_id))


async def _verify_backfill(principal_id: UUID, org_id: UUID) -> None:
    """连接与断言处于同一 event loop：避免跨 loop 使用 asyncpg 连接。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await connection.fetchrow(
            f"SELECT organization_id, schema_version FROM {CLAIM_TABLE} WHERE principal_id = $1",
            principal_id,
        )
        assert row is not None, "历史 bootstrap 幂等记录必须 backfill 为 claim"
        assert row["organization_id"] == org_id
        assert row["schema_version"] == 1
        assert await connection.fetchval(
            f"SELECT count(*) FROM {CLAIM_TABLE} WHERE principal_id = $1", principal_id
        ) == 1
    finally:
        await connection.close()


def test_backfill_fails_closed_on_ambiguous_principal(
    migrated_database: None,
) -> None:
    """同一 principal 已有多个不同 bootstrap targets → migration fail closed，绝不静默选第一个。"""
    config = _alembic_config()
    command.downgrade(config, "0007_audit_metadata_nonempty")
    principal_id = uuid4()
    org_x = uuid4()
    org_y = uuid4()
    asyncio.run(_seed_principal(principal_id))
    asyncio.run(_seed_org(org_x))
    asyncio.run(_seed_org(org_y))
    asyncio.run(_seed_bootstrap_idempotency(principal_id, org_x))
    asyncio.run(_seed_bootstrap_idempotency(principal_id, org_y))

    with pytest.raises(RuntimeError, match="ambiguous bootstrap history"):
        command.upgrade(config, "head")

    # 迁移在事务内失败 → 表与函数随事务整体回滚（transactional DDL）：断言整个
    # 0008 无残留，而不是「表还在但无行」。
    asyncio.run(_assert_schema_missing(CLAIM_TABLE, CLAIM_FUNCTION))

    # 收尾：清除歧义资产后必须能干净地回到 head（不残留半迁移状态）
    asyncio.run(_cleanup_bootstrap_records(f"{BOOTSTRAP_SCOPE_PREFIX}{principal_id}"))
    command.upgrade(config, "head")


def test_backfill_fails_closed_on_malformed_scope(
    migrated_database: None,
) -> None:
    """0007 预置不可解析的 `organization.create:<suffix>` → upgrade 必须 fail closed。

    四轮 RED 机制修订新增：scope 后缀不是合法 UUID 的历史记录与「多 target 歧义」
    同属不可自动裁决的历史——migration 必须明确 RuntimeError（unparseable owner
    principal），禁止静默跳过或猜测。RED（无 0008）：upgrade head 是空操作不抛错，
    本用例以 DID NOT RAISE 失败；GREEN（0008）：backfill 检测到 malformed scope →
    RuntimeError，transactional DDL 整体回滚。artifact 只有在本用例实际运行后才能
    写「已实验验证」。
    """
    config = _alembic_config()
    command.downgrade(config, "0007_audit_metadata_nonempty")
    org_id = uuid4()
    asyncio.run(_seed_org(org_id))
    asyncio.run(
        _seed_bootstrap_idempotency(
            uuid4(), org_id, scope=f"{BOOTSTRAP_SCOPE_PREFIX}not-a-uuid"
        )
    )

    with pytest.raises(RuntimeError, match="unparseable owner principal"):
        command.upgrade(config, "head")

    # 迁移在事务内失败 → 表与函数随事务整体回滚（transactional DDL）：断言整个
    # 0008 无残留，而不是「表还在但无行」。
    asyncio.run(_assert_schema_missing(CLAIM_TABLE, CLAIM_FUNCTION))

    # 收尾：清除 malformed 记录后必须能干净地回到 head（不残留半迁移状态）
    asyncio.run(_cleanup_bootstrap_records(f"{BOOTSTRAP_SCOPE_PREFIX}not-a-uuid"))
    command.upgrade(config, "head")


def test_downgrade_head_to_0007_drops_claims_table_and_function_upgrade_restores(
    migrated_database: None,
) -> None:
    """head → 0007 → head 可逆：downgrade 全撤 claim 表与窄函数，upgrade 全部重建。"""
    config = _alembic_config()
    command.downgrade(config, "0007_audit_metadata_nonempty")

    asyncio.run(_assert_schema_missing(CLAIM_TABLE, CLAIM_FUNCTION))

    command.upgrade(config, "head")

    asyncio.run(_assert_schema_present(CLAIM_TABLE, CLAIM_FUNCTION))


async def _assert_schema_missing(table: str, function: str) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM pg_class WHERE relname = $1", table
        ) == 0, "downgrade 后 claim 表必须不存在"
        assert await connection.fetchval(
            "SELECT count(*) FROM pg_proc WHERE proname = $1", function
        ) == 0, "downgrade 后窄函数必须不存在"
    finally:
        await connection.close()


async def _assert_schema_present(table: str, function: str) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM pg_class WHERE relname = $1", table
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM pg_proc WHERE proname = $1", function
        ) == 1
    finally:
        await connection.close()