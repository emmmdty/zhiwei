"""S0-T3 RED：migration、role/RLS、tenant transaction 与幂等地基。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import (
    IdempotencyConflict,
    TenantRepository,
)
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
    tenant_session,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
ADMIN_SQLALCHEMY_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
APP_SQLALCHEMY_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

REQUIRED_TABLES = {
    "agent_definitions",
    "agent_versions",
    "artifact_manifests",
    "audit_events",
    "canonical_events",
    "canonical_projections",
    "dataset_versions",
    "eval_runs",
    "eval_samples",
    "eval_suite_versions",
    "auth_sessions",
    "external_identities",
    "group_members",
    "oidc_login_attempts",
    "secret_envelopes",
    "groups",
    "idempotency_records",
    "memberships",
    "organizations",
    "outbox",
    "principals",
    "runs",
    "workspace_memberships",
    "workspaces",
    "organization_bootstrap_claims",
    # S2-T7（0011）：审批旅程持久层（FORCE RLS、org+ws 作用域）
    "approval_requests",
    # S7（0012）：memory 生命周期持久层（FORCE RLS、org+ws 作用域）
    # 四轮 RED 机制修订登记：REQUIRED_TABLES 契约随新表扩展（0011 同款）。
    "memory_records",
    "memory_lifecycle_events",
    # S9（0013/0014）：eval campaign 计划层 + cost ledger 台账（FORCE RLS、org+ws 作用域）
    "eval_campaigns",
    "cost_reservations",
    "cost_reconciliations",
    # S9-T4（0015）：agent release 治理面 + claim registry（FORCE RLS、org+ws 作用域）
    # 四轮 RED 机制修订登记：REQUIRED_TABLES 契约随新表扩展（0011 同款）。
    "agent_releases",
    "claim_registry",
}
WORKSPACE_TABLES = {
    "agent_definitions",
    "agent_versions",
    "artifact_manifests",
    "canonical_events",
    "canonical_projections",
    "dataset_versions",
    "eval_runs",
    "eval_samples",
    "eval_suite_versions",
    "group_members",
    "groups",
    "runs",
    "workspace_memberships",
    # S2-T7（0011）：审批请求是 workspace 作用域租户数据（org+ws GUC RLS）
    "approval_requests",
    # S7（0012）：memory 记录与生命周期台账均为 workspace 作用域租户数据
    "memory_records",
    "memory_lifecycle_events",
    # S9（0013/0014）：campaign 与 cost ledger 行均为 workspace 作用域租户数据
    "eval_campaigns",
    "cost_reservations",
    "cost_reconciliations",
    # S9-T4（0015）：release 与 claim 行均为 workspace 作用域租户数据
    "agent_releases",
    "claim_registry",
}
OPTIONAL_WORKSPACE_TABLES = {"audit_events", "idempotency_records", "outbox"}
# 总设计 §3.1：Group 位于 Workspace 之下；只有 Membership 是 organization 级
ORG_SCOPED_TABLES = {"memberships"}
# Principal / ExternalIdentity 是跨 Organization 的 identity-global 记录（DATA_MODEL §2：
# 首次 callback 时用户可能尚未创建/加入组织，同一 Principal 也可属于多个组织），
# 不能挂 organization_id，也不能用 org GUC 做 RLS——它们不是租户表。
# organization_bootstrap_claims（0008，S1-T4 四轮）同属 identity-global：principal 级
# 唯一 claim，无 org/ws 租户作用域语义、无 RLS，不给 zhiwei_app 任何直接表权限（只走窄
# SECURITY DEFINER 函数）——四轮 RED 机制修订登记：REQUIRED_TABLES 契约随新表扩展。
IDENTITY_GLOBAL_TABLES = {
    "auth_sessions",
    "external_identities",
    "oidc_login_attempts",
    "organization_bootstrap_claims",
    "principals",
    "secret_envelopes",
}
# organization_bootstrap_claims（0008）的 organization_id 是 claim 的目标值（指向被
# bootstrap 的组织），不是租户作用域列：表无 RLS、无 GUC 语义、不给任何角色直接访问。
# 其余 identity-global 表不得出现任何租户列（S0 冻结契约保持原样）。
TENANT_COLUMN_FREE_TABLES = IDENTITY_GLOBAL_TABLES - {"organization_bootstrap_claims"}
# S1-T2：zhiwei_app 对这些表无任何直接 SELECT/INSERT（权限在 0003 撤销/从不授予）
APP_DENIED_TABLES = IDENTITY_GLOBAL_TABLES
TENANT_RLS_TABLES = REQUIRED_TABLES - IDENTITY_GLOBAL_TABLES
# zhiwei_app 的 DELETE 授权集合：memberships/workspace_memberships（T1 生命周期）+
# group_members（T5 SCIM reconciliation 的 remove 方向，0009 GRANT DELETE）
DELETE_GRANTED_TABLES = {"memberships", "workspace_memberships", "group_members"}
MUTABLE_COLUMNS = {
    "agent_definitions": {"lifecycle", "name"},
    # S2-T7（0011）：审批决策 CAS 只允许这四列（列级 UPDATE，无表级授权）
    "approval_requests": {"decided_at", "decided_by", "decision_reason", "status"},
    # S7（0012）：memory 生命周期转移列 + ADR-009 证据合并列（列级 UPDATE，
    # 无表级授权）；内容列不可变——状态机不原地覆盖
    "memory_records": {
        "approver_ref",
        "confidence",
        "observed_at",
        "revoked_reason",
        "source_refs",
        "status",
        "superseded_by",
        "tombstone",
        "updated_at",
    },
    "canonical_projections": {
        "head_event_digest",
        "sequence_no",
        "state",
        "updated_at",
    },
    "eval_runs": {"sealed_at", "status"},
    "eval_samples": {"result", "result_digest", "status"},
    # S9（0013）：campaign status 由「全部子运行 sealed」推导的完成转移 + updated_at；
    # 划分内容列（suite_id/version/unit_count）冻结不可变（列级 UPDATE，无表级授权）
    "eval_campaigns": {"status", "updated_at"},
    # S9-T4（0015）：release 只有生命周期转移列可变——manifest payload/digest 冻结
    # 不可变（无 UPDATE 路径），活跃 rollout 策略仅被 rollback 改 default pin
    # （cohort 不重写）；claim 只有状态机/证据/绑定值列可变，statement/scope 冻结
    "agent_releases": {"rollout_policy", "state", "updated_at"},
    "claim_registry": {"bound_value", "evidence", "status", "updated_at"},
    "organizations": {"policy_ref", "retention_policy", "status"},
    "outbox": {
        "attempts",
        "available_at",
        "claimed_at",
        "claimed_by",
        "claim_token",
        "dead_lettered_at",
        "last_error",
        "lease_expires_at",
        "status",
    },
    "principals": {"status"},
    "runs": {"status", "updated_at"},
    "workspaces": {"budget_policy", "classification_ceiling", "name"},
}


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_SQLALCHEMY_URL)
    config.attributes["database_url"] = ADMIN_SQLALCHEMY_URL
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


async def _public_tables() -> set[str]:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = 'public' AND tablename <> 'alembic_version'
            """
        )
        return {row["tablename"] for row in rows}
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[set[str]]:
    """Exercise fresh upgrade and downgrade/upgrade, then leave the database at head."""
    asyncio.run(_assert_safe_test_database(ADMIN_DSN))
    config = _alembic_config()
    command.downgrade(config, "base")
    assert asyncio.run(_public_tables()) == set()

    command.upgrade(config, "head")
    first_upgrade = asyncio.run(_public_tables())

    command.downgrade(config, "base")
    assert asyncio.run(_public_tables()) == set()
    command.upgrade(config, "head")
    second_upgrade = asyncio.run(_public_tables())

    yield first_upgrade & second_upgrade


def test_fresh_migration_and_downgrade_upgrade_smoke(migrated_database: set[str]) -> None:
    assert migrated_database == REQUIRED_TABLES


def test_orm_metadata_matches_migration(migrated_database: set[str]) -> None:
    assert migrated_database
    command.check(_alembic_config())


def test_destructive_migrations_reject_non_test_database_identity() -> None:
    with pytest.raises(RuntimeError, match="dedicated zhiwei_test"):
        asyncio.run(_assert_safe_test_database("postgresql://app@db.example/production"))


def test_test_migrations_ignore_ambient_runtime_database_url(
    migrated_database: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert migrated_database
    monkeypatch.setenv(
        "ZHIWEI_DATABASE_URL", "postgresql+asyncpg://invalid@127.0.0.1:1/not_a_test_database"
    )
    command.check(_alembic_config())


@pytest.mark.asyncio
async def test_application_role_is_unprivileged_and_owns_no_tenant_tables(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        role = await connection.fetchrow(
            """
            SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = 'zhiwei_app'
            """
        )
        assert role is not None
        assert dict(role) == {
            "rolsuper": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
            "rolinherit": False,
            "rolbypassrls": False,
        }

        rows = await connection.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   pg_get_userbyid(c.relowner) AS owner
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
            """,
            sorted(REQUIRED_TABLES),
        )
        assert {row["relname"] for row in rows} == REQUIRED_TABLES
        for row in rows:
            assert row["owner"] != "zhiwei_app"
            if row["relname"] in TENANT_RLS_TABLES:
                assert row["relrowsecurity"] is True
                assert row["relforcerowsecurity"] is True
            else:
                assert row["relname"] in IDENTITY_GLOBAL_TABLES
                assert row["relrowsecurity"] is False
                assert row["relforcerowsecurity"] is False

        for table in REQUIRED_TABLES:
            if table in APP_DENIED_TABLES:
                assert not await connection.fetchval(
                    "SELECT has_table_privilege('zhiwei_app', $1, 'SELECT')", table
                )
                assert not await connection.fetchval(
                    "SELECT has_table_privilege('zhiwei_app', $1, 'INSERT')", table
                )
                assert not await connection.fetchval(
                    "SELECT has_table_privilege('zhiwei_app', $1, 'UPDATE')", table
                )
                assert not await connection.fetchval(
                    "SELECT has_table_privilege('zhiwei_app', $1, 'DELETE')", table
                )
                continue
            assert await connection.fetchval(
                "SELECT has_table_privilege('zhiwei_app', $1, 'SELECT')", table
            )
            assert await connection.fetchval(
                "SELECT has_table_privilege('zhiwei_app', $1, 'INSERT')", table
            )
            assert not await connection.fetchval(
                "SELECT has_table_privilege('zhiwei_app', $1, 'UPDATE')", table
            )
            assert await connection.fetchval(
                "SELECT has_table_privilege('zhiwei_app', $1, 'DELETE')", table
            ) is (table in DELETE_GRANTED_TABLES)
            columns = await connection.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = $1
                """,
                table,
            )
            for column in {row["column_name"] for row in columns}:
                assert (
                    await connection.fetchval(
                        "SELECT has_column_privilege('zhiwei_app', $1, $2, 'UPDATE')",
                        table,
                        column,
                    )
                    is (column in MUTABLE_COLUMNS.get(table, set()))
                )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_every_tenant_table_has_expected_default_deny_policy(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            """
            SELECT c.relname AS table_name, p.polcmd, p.polpermissive,
                   pg_get_expr(p.polqual, p.polrelid) AS using_expression,
                   pg_get_expr(p.polwithcheck, p.polrelid) AS check_expression
            FROM pg_catalog.pg_policy AS p
            JOIN pg_catalog.pg_class AS c ON c.oid = p.polrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
            """,
            sorted(TENANT_RLS_TABLES),
        )
        assert {row["table_name"] for row in rows} == TENANT_RLS_TABLES
        assert len(rows) == len(TENANT_RLS_TABLES)
        for row in rows:
            table = row["table_name"]
            using_expression = row["using_expression"]
            assert row["polcmd"] == b"*"
            assert row["polpermissive"] is True
            assert row["check_expression"] == using_expression
            assert "zhiwei.organization_id" in using_expression
            if table == "organizations":
                assert "zhiwei.workspace_id" not in using_expression
            elif table == "workspaces":
                assert "zhiwei.workspace_id" in using_expression
                assert "organization_id" in using_expression
            elif table in WORKSPACE_TABLES:
                assert "zhiwei.workspace_id" in using_expression
                assert "workspace_id IS NULL" not in using_expression
            elif table in OPTIONAL_WORKSPACE_TABLES:
                assert "zhiwei.workspace_id" in using_expression
                assert "workspace_id IS NULL" in using_expression
            elif table in ORG_SCOPED_TABLES:
                assert "zhiwei.workspace_id" not in using_expression
            else:  # pragma: no cover - sets above must remain exhaustive
                raise AssertionError(f"unclassified RLS table: {table}")
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_missing_database_tenant_context_denies_every_table(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    organization_id, workspace_id, definition_id, idempotency_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(
            "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1)",
            organization_id,
        )
        await admin.execute(
            """
            INSERT INTO workspaces (id, organization_id, name, schema_version)
            VALUES ($1, $2, 'seeded', 1)
            """,
            workspace_id,
            organization_id,
        )
        await admin.execute(
            """
            INSERT INTO agent_definitions
                (id, organization_id, workspace_id, name, schema_version)
            VALUES ($1, $2, $3, 'seeded', 1)
            """,
            definition_id,
            organization_id,
            workspace_id,
        )
        await admin.execute(
            """
            INSERT INTO idempotency_records
                (id, organization_id, workspace_id, scope, idempotency_key,
                 request_digest, response, schema_version)
            VALUES ($1, $2, $3, 'seed', 'seed', $4, '{}'::jsonb, 1)
            """,
            idempotency_id,
            organization_id,
            workspace_id,
            "sha256:" + "0" * 64,
        )
    finally:
        await admin.close()

    connection = await asyncpg.connect(APP_DSN)
    try:
        for table in sorted(TENANT_RLS_TABLES):
            assert await connection.fetchval(f'SELECT count(*) FROM "{table}"') == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1)",
                uuid4(),
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_repository_requires_explicit_organization_context(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    engine = create_database_engine(APP_SQLALCHEMY_URL)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            repository = TenantRepository(session, context=None)
            with pytest.raises(TenantContextRequired):
                await repository.get_organization(uuid4())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tenant_context_is_transaction_local_and_does_not_leak(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    organization_id, workspace_id = uuid4(), uuid4()
    engine = create_database_engine(APP_SQLALCHEMY_URL)
    sessions = create_session_factory(engine)
    try:
        context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
        async with tenant_session(sessions, context) as session:
            repository = TenantRepository(session, context)
            created = await repository.create_organization(organization_id, status="active")
            assert created.id == organization_id
            await repository.create_workspace(workspace_id, name="leak-check")
            first_backend_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            setting = await session.scalar(
                text("SELECT current_setting('zhiwei.organization_id', true)")
            )
            assert setting == str(organization_id)
            workspace_setting = await session.scalar(
                text("SELECT current_setting('zhiwei.workspace_id', true)")
            )
            assert workspace_setting == str(workspace_id)

        async with sessions() as session, session.begin():
            second_backend_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert second_backend_pid == first_backend_pid
            organization_setting = await session.scalar(
                text("SELECT current_setting('zhiwei.organization_id', true)")
            )
            workspace_setting = await session.scalar(
                text("SELECT current_setting('zhiwei.workspace_id', true)")
            )
            assert organization_setting in (None, "")
            assert workspace_setting in (None, "")
            assert await session.scalar(text("SELECT count(*) FROM organizations")) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_rolls_back_all_tenant_writes(migrated_database: set[str]) -> None:
    assert migrated_database
    organization_id = uuid4()
    engine = create_database_engine(APP_SQLALCHEMY_URL)
    sessions = create_session_factory(engine)
    try:
        with pytest.raises(RuntimeError, match="force rollback"):
            async with tenant_session(
                sessions, TenantContext(organization_id=organization_id)
            ) as session:
                repository = TenantRepository(
                    session, TenantContext(organization_id=organization_id)
                )
                await repository.create_organization(organization_id, status="active")
                raise RuntimeError("force rollback")

        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            count = await connection.fetchval(
                "SELECT count(*) FROM organizations WHERE id = $1", organization_id
            )
            assert count == 0
        finally:
            await connection.close()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repository_and_rls_reject_cross_tenant_access(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    first_id, second_id = uuid4(), uuid4()
    engine = create_database_engine(APP_SQLALCHEMY_URL)
    sessions = create_session_factory(engine)
    try:
        for organization_id in (first_id, second_id):
            context = TenantContext(organization_id=organization_id)
            async with tenant_session(sessions, context) as session:
                await TenantRepository(session, context).create_organization(
                    organization_id, status="active"
                )

        first_context = TenantContext(organization_id=first_id)
        async with tenant_session(sessions, first_context) as session:
            repository = TenantRepository(session, first_context)
            assert await repository.get_organization(first_id) is not None
            with pytest.raises(TenantScopeError):
                await repository.get_organization(second_id)
            visible_ids = set((await session.execute(text("SELECT id FROM organizations"))).scalars())
            assert visible_ids == {first_id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workspace_and_optional_workspace_rls_scopes(migrated_database: set[str]) -> None:
    assert migrated_database
    organization_id, first_workspace_id, second_workspace_id = uuid4(), uuid4(), uuid4()
    definition_id = uuid4()
    engine = create_database_engine(APP_SQLALCHEMY_URL)
    sessions = create_session_factory(engine)
    try:
        organization_context = TenantContext(organization_id=organization_id)
        async with tenant_session(sessions, organization_context) as session:
            repository = TenantRepository(session, organization_context)
            await repository.create_organization(organization_id, status="active")
            await repository.create_workspace(first_workspace_id, name="first")
            await repository.create_workspace(second_workspace_id, name="second")
            await repository.claim_idempotency(
                scope="org-scope",
                key="shared",
                request_digest="sha256:" + "3" * 64,
                response={"scope": "organization"},
            )

        first_context = TenantContext(
            organization_id=organization_id, workspace_id=first_workspace_id
        )
        async with tenant_session(sessions, first_context) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO agent_definitions
                        (id, organization_id, workspace_id, name, schema_version)
                    VALUES (:id, :organization_id, :workspace_id, 'visible', 1)
                    """
                ),
                {
                    "id": definition_id,
                    "organization_id": organization_id,
                    "workspace_id": first_workspace_id,
                },
            )
            await TenantRepository(session, first_context).claim_idempotency(
                scope="workspace-scope",
                key="private",
                request_digest="sha256:" + "4" * 64,
                response={"scope": "workspace"},
            )

        second_context = TenantContext(
            organization_id=organization_id, workspace_id=second_workspace_id
        )
        async with tenant_session(sessions, second_context) as session:
            visible_workspace_ids = set(
                (await session.execute(text("SELECT id FROM workspaces"))).scalars()
            )
            assert visible_workspace_ids == {second_workspace_id}
            assert await session.scalar(
                text("SELECT count(*) FROM agent_definitions WHERE id = :id"),
                {"id": definition_id},
            ) == 0
            visible_idempotency_scopes = set(
                (
                    await session.execute(
                        text(
                            "SELECT scope FROM idempotency_records "
                            "WHERE scope IN ('org-scope', 'workspace-scope')"
                        )
                    )
                ).scalars()
            )
            assert visible_idempotency_scopes == {"org-scope"}
            updated = await session.execute(
                text("UPDATE workspaces SET name = 'blocked' WHERE id = :id RETURNING id"),
                {"id": first_workspace_id},
            )
            assert updated.scalar_one_or_none() is None

        with pytest.raises(DBAPIError):
            async with tenant_session(sessions, second_context) as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO agent_definitions
                            (id, organization_id, workspace_id, name, schema_version)
                        VALUES (:id, :organization_id, :workspace_id, 'blocked', 1)
                        """
                    ),
                    {
                        "id": uuid4(),
                        "organization_id": organization_id,
                        "workspace_id": first_workspace_id,
                    },
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_optional_workspace_rows_keep_organization_integrity(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    first_organization_id, second_organization_id, second_workspace_id = (
        uuid4(),
        uuid4(),
        uuid4(),
    )
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.executemany(
            "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1)",
            [(first_organization_id,), (second_organization_id,)],
        )
        await connection.execute(
            """
            INSERT INTO workspaces (id, organization_id, name, schema_version)
            VALUES ($1, $2, 'second', 1)
            """,
            second_workspace_id,
            second_organization_id,
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await connection.execute(
                """
                INSERT INTO idempotency_records
                    (id, organization_id, workspace_id, scope, idempotency_key,
                     request_digest, response, schema_version)
                VALUES ($1, $2, $3, 'fk', 'mismatch', $4, '{}'::jsonb, 1)
                """,
                uuid4(),
                first_organization_id,
                second_workspace_id,
                "sha256:" + "5" * 64,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await connection.execute(
                """
                INSERT INTO idempotency_records
                    (id, organization_id, workspace_id, scope, idempotency_key,
                     request_digest, response, schema_version)
                VALUES ($1, $2, NULL, 'fk', 'missing-org', $3, '{}'::jsonb, 1)
                """,
                uuid4(),
                uuid4(),
                "sha256:" + "6" * 64,
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_idempotency_reuses_same_request_and_rejects_conflict(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    engine = create_database_engine(APP_SQLALCHEMY_URL)
    sessions = create_session_factory(engine)

    async def claim(request_digest: str) -> tuple[bool, dict[str, Any]]:
        async with tenant_session(sessions, context) as session:
            result = await TenantRepository(session, context).claim_idempotency(
                scope="workspace.create",
                key="stable-request-key",
                request_digest=request_digest,
                response={"workspace_id": str(workspace_id)},
            )
            return result.created, result.response

    try:
        async with tenant_session(sessions, context) as session:
            repository = TenantRepository(session, context)
            await repository.create_organization(organization_id, status="active")
            await repository.create_workspace(workspace_id, name="Foundation")

        first = await claim("sha256:" + "1" * 64)
        repeated = await claim("sha256:" + "1" * 64)
        assert first == (True, {"workspace_id": str(workspace_id)})
        assert repeated == (False, {"workspace_id": str(workspace_id)})

        with pytest.raises(IdempotencyConflict):
            await claim("sha256:" + "2" * 64)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idempotency_scope_includes_workspace_and_handles_concurrency(
    migrated_database: set[str],
) -> None:
    assert migrated_database
    organization_id, first_workspace_id, second_workspace_id = uuid4(), uuid4(), uuid4()
    engine = create_database_engine(APP_SQLALCHEMY_URL)
    sessions = create_session_factory(engine)
    try:
        organization_context = TenantContext(organization_id=organization_id)
        async with tenant_session(sessions, organization_context) as session:
            repository = TenantRepository(session, organization_context)
            await repository.create_organization(organization_id, status="active")
            await repository.create_workspace(first_workspace_id, name="first")
            await repository.create_workspace(second_workspace_id, name="second")

        async def claim(workspace_id: Any, key: str, digest_digit: str) -> bool:
            context = TenantContext(
                organization_id=organization_id, workspace_id=workspace_id
            )
            async with tenant_session(sessions, context) as session:
                result = await TenantRepository(session, context).claim_idempotency(
                    scope="concurrent",
                    key=key,
                    request_digest="sha256:" + digest_digit * 64,
                    response={"workspace_id": str(workspace_id)},
                )
                return result.created

        assert await claim(first_workspace_id, "same-key-different-workspace", "7")
        assert await claim(second_workspace_id, "same-key-different-workspace", "7")

        repeated = await asyncio.gather(
            claim(first_workspace_id, "same-request", "8"),
            claim(first_workspace_id, "same-request", "8"),
        )
        assert sorted(repeated) == [False, True]

        conflicting = await asyncio.gather(
            claim(second_workspace_id, "conflicting-request", "9"),
            claim(second_workspace_id, "conflicting-request", "a"),
            return_exceptions=True,
        )
        assert sum(result is True for result in conflicting) == 1
        assert sum(isinstance(result, IdempotencyConflict) for result in conflicting) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_identity_global_tables_have_no_tenant_scope(
    migrated_database: set[str],
) -> None:
    """Principal / ExternalIdentity 是跨 Organization 的身份记录。

    DATA_MODEL §2：首次 OIDC callback 时用户可能尚未创建/加入组织，同一 Principal 也可属于
    多个组织——它们不能挂 organization_id，也不能用 org GUC 做 RLS（否则首登即被 FORCE RLS
    拦死）。租户边界由 Membership 与 repository/API 层负责，不在这里伪造租户列。
    """
    assert migrated_database
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        for table in sorted(TENANT_COLUMN_FREE_TABLES):
            assert await connection.fetchval(
                "SELECT relrowsecurity FROM pg_catalog.pg_class "
                "WHERE relname = $1 AND relnamespace = 'public'::regnamespace",
                table,
            ) is False
            assert await connection.fetchval(
                "SELECT relforcerowsecurity FROM pg_catalog.pg_class "
                "WHERE relname = $1 AND relnamespace = 'public'::regnamespace",
                table,
            ) is False
            assert await connection.fetchval(
                "SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class "
                "WHERE relname = $1 AND relnamespace = 'public'::regnamespace",
                table,
            ) != "zhiwei_app"
            tenant_columns = {
                row["column_name"]
                for row in await connection.fetch(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = $1
                      AND column_name IN ('organization_id', 'workspace_id')
                    """,
                    table,
                )
            }
            assert tenant_columns == set(), f"{table} 不得含租户列: {tenant_columns}"
        # organization_bootstrap_claims：claim 目标列豁免租户列断言（见常量注释），
        # 但必须无 RLS、owner 不得是 zhiwei_app（与其余 identity-global 表一致）。
        for table in IDENTITY_GLOBAL_TABLES - TENANT_COLUMN_FREE_TABLES:
            assert await connection.fetchval(
                "SELECT relrowsecurity FROM pg_catalog.pg_class "
                "WHERE relname = $1 AND relnamespace = 'public'::regnamespace",
                table,
            ) is False
            assert await connection.fetchval(
                "SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class "
                "WHERE relname = $1 AND relnamespace = 'public'::regnamespace",
                table,
            ) != "zhiwei_app"
    finally:
        await connection.close()


def test_test_compose_does_not_expose_or_start_a_model_service() -> None:
    compose = (REPO_ROOT / "deploy" / "compose" / "compose.test.yaml").read_text("utf-8")
    assert "OPENAI_API_KEY" not in compose
    assert "OPENAI_BASE_URL" not in compose
    assert "model" not in compose.lower()
