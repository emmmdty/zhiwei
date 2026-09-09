"""S1-T4 RED：连接池单连接复用与事务级 tenant GUC 生命周期。

设计/验收方冻结（A 档），行为当前已实现，本文件是 green-at-RED 回归护栏：
- engine 固定 pool_size=1 / max_overflow=0，全部 session 复用同一物理连接
  （pg_backend_pid 相等，证明 GUC 断言发生在同一条连接上）；
- set_tenant_context 以 set_config(..., true) 只作用于当前事务：commit 或 rollback 之后，
  同一物理连接上的新事务必须回到空 GUC（current_setting 返回 NULL/''，且不等于租户 ID），
  RLS 全拒（SELECT 0 行）——前一租户的上下文不得存活；
- 新事务设置 org2 上下文时只可见 org2 数据（org1 不可见）；
- org-only 上下文可见 org 级数据（organizations / memberships / workspaces），
  workspace 级表（groups / workspace_memberships / group_members）恒 0 行；
  org+workspace 上下文可见 workspace 级行；
- 未开启事务时调用 set_tenant_context 必须 RuntimeError（fail closed）。
RLS 目录与行为矩阵由 test_rls.py 覆盖。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from zhiwei.persistence.tenant import TenantContext, set_tenant_context

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
APP_SQLALCHEMY_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

# 可猜测的固定 UUID：与 test_rls.py 保持同一套 fixture 语义
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


@pytest.fixture(scope="function")
def app_sessions() -> Iterator[async_sessionmaker[AsyncSession]]:
    """pool_size=1 / max_overflow=0：所有 session 复用唯一物理连接，GUC 断言才有意义。"""
    engine = create_async_engine(APP_SQLALCHEMY_URL, pool_size=1, max_overflow=0)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(engine.dispose())


async def _seed_tenancy() -> None:
    """migrator 预置双组织 fixture（orgs / workspaces / memberships / workspace_memberships /
    groups / group_members）；幂等——先清掉本 fixture 的已知 ID 再插入。"""
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
            "INSERT INTO organizations (id, status, schema_version) "
            "VALUES ($1, 'active', 1), ($2, 'active', 1)",
            ORG_1,
            ORG_2,
        )
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


# --------------------------------------------------------------------------- 契约 2：单连接复用

@pytest.mark.asyncio
async def test_pool_reuses_single_physical_connection(
    migrated_database: None,
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with app_sessions() as first_session, first_session.begin():
        first_pid = await first_session.scalar(text("SELECT pg_backend_pid()"))
    async with app_sessions() as second_session, second_session.begin():
        second_pid = await second_session.scalar(text("SELECT pg_backend_pid()"))
        assert second_pid == first_pid


# --------------------------------------------------------------------------- 契约 3：commit 不泄漏上下文

@pytest.mark.asyncio
async def test_tenant_guc_does_not_survive_commit(
    migrated_database: None,
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_tenancy()
    async with app_sessions() as session_a:
        await session_a.begin()
        await set_tenant_context(
            session_a, TenantContext(organization_id=ORG_1, workspace_id=WS_1)
        )
        committed_pid = await session_a.scalar(text("SELECT pg_backend_pid()"))
        assert await session_a.scalar(text("SELECT count(*) FROM organizations")) == 1
        assert await session_a.scalar(text("SELECT count(*) FROM workspaces")) == 1
        assert {
            row[0] for row in (await session_a.execute(text("SELECT id FROM workspaces"))).all()
        } == {WS_1}
        await session_a.commit()

    async with app_sessions() as session_b:
        await session_b.begin()
        assert await session_b.scalar(text("SELECT pg_backend_pid()")) == committed_pid
        organization_setting = await session_b.scalar(
            text("SELECT current_setting('zhiwei.organization_id', true)")
        )
        workspace_setting = await session_b.scalar(
            text("SELECT current_setting('zhiwei.workspace_id', true)")
        )
        assert organization_setting in (None, "")
        assert workspace_setting in (None, "")
        assert organization_setting != str(ORG_1)
        assert workspace_setting != str(WS_1)
        assert await session_b.scalar(text("SELECT count(*) FROM organizations")) == 0
        assert await session_b.scalar(text("SELECT count(*) FROM workspaces")) == 0
        await session_b.rollback()


# --------------------------------------------------------------------------- 契约 4：rollback 不泄漏上下文

@pytest.mark.asyncio
async def test_tenant_guc_does_not_survive_rollback(
    migrated_database: None,
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_tenancy()
    async with app_sessions() as session_a:
        await session_a.begin()
        await set_tenant_context(
            session_a, TenantContext(organization_id=ORG_1, workspace_id=WS_1)
        )
        rolled_back_pid = await session_a.scalar(text("SELECT pg_backend_pid()"))
        assert await session_a.scalar(text("SELECT count(*) FROM organizations")) == 1
        await session_a.rollback()

    async with app_sessions() as session_b:
        await session_b.begin()
        assert await session_b.scalar(text("SELECT pg_backend_pid()")) == rolled_back_pid
        organization_setting = await session_b.scalar(
            text("SELECT current_setting('zhiwei.organization_id', true)")
        )
        workspace_setting = await session_b.scalar(
            text("SELECT current_setting('zhiwei.workspace_id', true)")
        )
        assert organization_setting in (None, "")
        assert workspace_setting in (None, "")
        assert organization_setting != str(ORG_1)
        assert workspace_setting != str(WS_1)
        assert await session_b.scalar(text("SELECT count(*) FROM organizations")) == 0
        assert await session_b.scalar(text("SELECT count(*) FROM workspaces")) == 0
        await session_b.rollback()


# --------------------------------------------------------------------------- 契约 5：新事务只可见自己的租户

@pytest.mark.asyncio
async def test_new_transaction_sees_only_new_tenant(
    migrated_database: None,
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_tenancy()
    async with app_sessions() as session_a:
        await session_a.begin()
        await set_tenant_context(
            session_a, TenantContext(organization_id=ORG_2, workspace_id=WS_2)
        )
        assert await session_a.scalar(text("SELECT count(*) FROM organizations")) == 1
        assert (
            await session_a.scalar(
                text("SELECT count(*) FROM organizations WHERE id = :org_1"),
                {"org_1": ORG_1},
            )
            == 0
        )
        assert {
            row[0] for row in (await session_a.execute(text("SELECT id FROM workspaces"))).all()
        } == {WS_2}
        assert {
            row[0] for row in (await session_a.execute(text("SELECT id FROM groups"))).all()
        } == {GROUP_2}
        assert await session_a.scalar(text("SELECT count(*) FROM group_members")) == 1
        await session_a.commit()


# --------------------------------------------------------------------------- 契约 6：org-only 与 org+workspace 上下文

@pytest.mark.asyncio
async def test_org_only_context_hides_workspace_scoped_rows(
    migrated_database: None,
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_tenancy()
    async with app_sessions() as session_a:
        await session_a.begin()
        await set_tenant_context(session_a, TenantContext(organization_id=ORG_1))
        assert await session_a.scalar(text("SELECT count(*) FROM organizations")) == 1
        assert await session_a.scalar(text("SELECT count(*) FROM memberships")) == 1
        assert {
            row[0] for row in (await session_a.execute(text("SELECT id FROM workspaces"))).all()
        } == {WS_1}
        for table in ("groups", "workspace_memberships", "group_members"):
            assert await session_a.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        await session_a.commit()

    async with app_sessions() as session_b:
        await session_b.begin()
        await set_tenant_context(
            session_b, TenantContext(organization_id=ORG_1, workspace_id=WS_1)
        )
        assert await session_b.scalar(text("SELECT count(*) FROM groups")) == 1
        assert {
            row[0] for row in (await session_b.execute(text("SELECT id FROM groups"))).all()
        } == {GROUP_1}
        assert await session_b.scalar(text("SELECT count(*) FROM workspace_memberships")) == 1
        assert await session_b.scalar(text("SELECT count(*) FROM group_members")) == 1
        await session_b.commit()


# --------------------------------------------------------------------------- 契约 7：fail closed on misuse

@pytest.mark.asyncio
async def test_set_tenant_context_requires_active_transaction(
    migrated_database: None,
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with app_sessions() as session:
        with pytest.raises(RuntimeError, match="active transaction"):
            await set_tenant_context(
                session, TenantContext(organization_id=ORG_1, workspace_id=WS_1)
            )
