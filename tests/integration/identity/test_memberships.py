"""S1-T1 CONTRACT REPAIR RED：identity 迁移、RLS、tenant-scoped repository 与 /api/v1 契约。

上位契约（设计/验收方裁决）：
- 冻结总设计 §3.1 + docs/API.md §2：Group 是 Workspace scope（organization_id + workspace_id），
  名称唯一范围是 Workspace；跨 Workspace guessed group ID 返回 absent/拒绝；
- Group/GroupMember RLS 同时检查 zhiwei.organization_id 与 zhiwei.workspace_id；
- API route shape 全部采用 /api/v1；所有 mutation 要求非空 Idempotency-Key（重复 key +
  相同 payload 返回原结果，不同 payload 冲突，接入 S0 idempotency 基础）；
- 读跨租户或不存在资源统一 404；已知资源上的未授权 mutation 返回 403；不泄露存在性；
- bootstrap 命令原子创建 Organization + Owner Membership；首登 principal 可以没有 active org；
- OIDC 身份来源留给 T2，本文件用测试 stub actor。

API 层：routers 必须经显式 actor dependency 注入身份与 tenant context，没有默认 allow。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fixtures.policy_fake import FakePolicyEnforcer
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from zhiwei.api.memberships import create_memberships_router
from zhiwei.api.organizations import create_organizations_router
from zhiwei.api.workspaces import create_workspaces_router
from zhiwei.identity.commands import (
    ExternalIdentityConflictError,
    IdempotencyRequest,
    PrincipalDisabledError,
    add_group_member,
    add_org_membership,
    add_workspace_membership,
    create_organization,
    create_user,
    disable_principal,
    remove_org_membership,
)
from zhiwei.identity.domain import ActorContext, PrincipalKind, PrincipalStatus
from zhiwei.identity.repositories import IdentityRepository, IdentityStore
from zhiwei.persistence.repositories import IdempotencyConflict, TenantRepository
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
IDENTITY_DSN = os.environ.get(
    "ZHIWEI_TEST_IDENTITY_DSN", "postgresql://zhiwei_identity@127.0.0.1:55432/zhiwei_test"
)
IDENTITY_SQLALCHEMY_URL = IDENTITY_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

TENANT_TABLES = {"group_members", "groups", "memberships", "workspace_memberships"}
IDENTITY_GLOBAL_TABLES = {"external_identities", "principals"}


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


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[None]:
    """0002 identity migration：从 base 重建到 head 后保持，供本文件所有用例使用。"""
    asyncio.run(_assert_safe_test_database(ADMIN_DSN))
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


@pytest.fixture(scope="function")
def sessions() -> Iterator[async_sessionmaker[AsyncSession]]:
    """每个测试独立 engine；NullPool 让连接只活在测试自己的 event loop 里。"""
    engine = create_async_engine(APP_SQLALCHEMY_URL, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(engine.dispose())


@pytest.fixture(scope="function")
def identity_sessions() -> Iterator[async_sessionmaker[AsyncSession]]:
    """S1-T2：identity-global 数据（principals / external_identities / auth_sessions /
    secret_envelopes）走独立 zhiwei_identity 角色与独立 engine，不再经 zhiwei_app。"""
    engine = create_async_engine(IDENTITY_SQLALCHEMY_URL, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(engine.dispose())


async def _seed_organization_and_workspace(
    sessions: async_sessionmaker[AsyncSession],
    organization_id: UUID,
    workspace_id: UUID,
    *,
    workspace_name: str,
) -> None:
    context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name=workspace_name)


async def _seed_principal(
    identity_sessions: async_sessionmaker[AsyncSession], principal_id: UUID
) -> None:
    async with identity_sessions() as session, session.begin():
        repository = IdentityStore(session)
        await repository.create_principal(
            principal_id, kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
        )


def _org_actor(organization_id: UUID, *, workspace_id: UUID | None = None) -> ActorContext:
    return ActorContext(
        principal_id=uuid4(), organization_id=organization_id, workspace_id=workspace_id
    )


async def _seed_actor_principal(
    identity_sessions: async_sessionmaker[AsyncSession], principal_id: UUID
) -> None:
    """为合成 actor 落 principal 行。

    workspace 创建自 2026-09-03 增补起同事务授予创建者 workspace_admin
    （bootstrap），授予要求创建者 principal 真实存在（fail closed）——
    经端点创建 workspace 的合成 actor 必须先种子。
    """
    async with identity_sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, kind, status, schema_version)"
                " VALUES (:id, 'user', 'active', 1)"
            ),
            {"id": principal_id},
        )


def _no_org_actor(principal_id: UUID | None = None) -> ActorContext:
    return ActorContext(principal_id=principal_id or uuid4())


def _idempotency(key: str, digest_digit: str) -> IdempotencyRequest:
    return IdempotencyRequest(key=key, request_digest="sha256:" + digest_digit * 64)


# --------------------------------------------------------------------------- migration 与 RLS 结构


@pytest.mark.asyncio
async def test_identity_tables_exist_with_expected_rls_structure(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   pg_get_userbyid(c.relowner) AS owner
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY($1::text[])
            """,
            sorted(TENANT_TABLES | IDENTITY_GLOBAL_TABLES),
        )
        by_name = {row["relname"]: row for row in rows}
        assert set(by_name) == TENANT_TABLES | IDENTITY_GLOBAL_TABLES
        for table in TENANT_TABLES:
            assert by_name[table]["relrowsecurity"] is True
            assert by_name[table]["relforcerowsecurity"] is True
            assert by_name[table]["owner"] != "zhiwei_app"
        for table in IDENTITY_GLOBAL_TABLES:
            assert by_name[table]["relrowsecurity"] is False
            assert by_name[table]["relforcerowsecurity"] is False

        role = await connection.fetchrow(
            "SELECT rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = 'zhiwei_app'"
        )
        assert role is not None
        assert role["rolbypassrls"] is False
    finally:
        await connection.close()


# --------------------------------------------------------------------------- repository 与 RLS 行为


@pytest.mark.asyncio
async def test_principal_is_identity_global_but_tenant_tables_require_context(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    principal_id = uuid4()
    async with identity_sessions() as session:
        store = IdentityStore(session)
        principal = await store.create_principal(
            principal_id, kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
        )
        assert principal.id == principal_id
        assert (await store.get_principal(principal_id)) == principal
        identity = await store.bind_external_identity(
            issuer="https://idp.example.com", subject="alice", principal_id=principal_id
        )
        assert identity.stable_key == ("https://idp.example.com", "alice")
        assert (
            await store.get_external_identity(
                issuer="https://idp.example.com", subject="alice"
            )
        ) == identity
    async with sessions() as session:
        with pytest.raises(TenantContextRequired):
            await IdentityRepository(session, context=None).add_membership(
                principal_id=principal_id, organization_id=uuid4(), role_bindings=frozenset()
            )


@pytest.mark.asyncio
async def test_external_identity_issuer_subject_unique_at_database_level(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    first_id, second_id = uuid4(), uuid4()
    async with identity_sessions() as session:
        store = IdentityStore(session)
        await store.create_principal(
            first_id, kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
        )
        await store.create_principal(
            second_id, kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
        )
        await store.bind_external_identity(
            issuer="https://idp.example.com", subject="alice", principal_id=first_id
        )
        with pytest.raises(ExternalIdentityConflictError):
            await store.bind_external_identity(
                issuer="https://idp.example.com", subject="alice", principal_id=second_id
            )


@pytest.mark.asyncio
async def test_missing_tenant_context_denies_identity_tenant_tables(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, workspace_id, principal_id, group_id = uuid4(), uuid4(), uuid4(), uuid4()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1)",
            organization_id,
        )
        await connection.execute(
            """
            INSERT INTO workspaces (id, organization_id, name, schema_version)
            VALUES ($1, $2, 'seeded', 1)
            """,
            workspace_id,
            organization_id,
        )
        await connection.execute(
            "INSERT INTO principals (id, kind, status, schema_version) VALUES ($1, 'user', 'active', 1)",
            principal_id,
        )
        await connection.execute(
            """
            INSERT INTO memberships (principal_id, organization_id, role_bindings)
            VALUES ($1, $2, '["member"]'::jsonb)
            """,
            principal_id,
            organization_id,
        )
        await connection.execute(
            """
            INSERT INTO workspace_memberships (principal_id, organization_id, workspace_id, role_bindings)
            VALUES ($1, $2, $3, '["builder"]'::jsonb)
            """,
            principal_id,
            organization_id,
            workspace_id,
        )
        await connection.execute(
            """
            INSERT INTO groups (id, organization_id, workspace_id, name, schema_version)
            VALUES ($1, $2, $3, 'seeded', 1)
            """,
            group_id,
            organization_id,
            workspace_id,
        )
        await connection.execute(
            """
            INSERT INTO group_members (group_id, organization_id, workspace_id, principal_id)
            VALUES ($1, $2, $3, $4)
            """,
            group_id,
            organization_id,
            workspace_id,
            principal_id,
        )
    finally:
        await connection.close()

    connection = await asyncpg.connect(APP_DSN)
    try:
        for table in sorted(TENANT_TABLES):
            assert await connection.fetchval(f'SELECT count(*) FROM "{table}"') == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                """
                INSERT INTO memberships (principal_id, organization_id, role_bindings)
                VALUES ($1, $2, '["member"]'::jsonb)
                """,
                principal_id,
                organization_id,
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                """
                INSERT INTO groups (id, organization_id, workspace_id, name, schema_version)
                VALUES ($1, $2, $3, 'blocked', 1)
                """,
                uuid4(),
                organization_id,
                workspace_id,
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_same_name_workspace_and_group_do_not_leak_across_orgs(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    first_org, second_org = uuid4(), uuid4()
    first_workspace, second_workspace = uuid4(), uuid4()
    first_group, second_group = uuid4(), uuid4()
    first_member, second_member = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, first_org, first_workspace, workspace_name="Sales"
    )
    await _seed_organization_and_workspace(
        sessions, second_org, second_workspace, workspace_name="Sales"
    )
    await _seed_principal(identity_sessions, first_member)
    await _seed_principal(identity_sessions, second_member)

    async with tenant_session(
        sessions, TenantContext(organization_id=first_org, workspace_id=first_workspace)
    ) as session:
        repository = IdentityRepository(
            session, TenantContext(organization_id=first_org, workspace_id=first_workspace)
        )
        await repository.create_group(
            first_group, organization_id=first_org, workspace_id=first_workspace, name="Finance"
        )
        await repository.add_group_member(
            group_id=first_group,
            organization_id=first_org,
            workspace_id=first_workspace,
            principal_id=first_member,
        )
    async with tenant_session(
        sessions, TenantContext(organization_id=second_org, workspace_id=second_workspace)
    ) as session:
        repository = IdentityRepository(
            session, TenantContext(organization_id=second_org, workspace_id=second_workspace)
        )
        await repository.create_group(
            second_group,
            organization_id=second_org,
            workspace_id=second_workspace,
            name="Finance",
        )
        await repository.add_group_member(
            group_id=second_group,
            organization_id=second_org,
            workspace_id=second_workspace,
            principal_id=second_member,
        )

    async with tenant_session(
        sessions, TenantContext(organization_id=first_org, workspace_id=first_workspace)
    ) as session:
        assert set((await session.execute(text("SELECT id FROM workspaces"))).scalars()) == {
            first_workspace
        }
        assert set(
            (
                await session.execute(
                    text("SELECT id FROM groups WHERE name = 'Finance'")
                )
            ).scalars()
        ) == {first_group}
        assert set(
            (
                await session.execute(
                    text(
                        "SELECT principal_id FROM group_members WHERE group_id = :group_id"
                    ),
                    {"group_id": second_group},
                )
            ).scalars()
        ) == set()

    async with tenant_session(
        sessions, TenantContext(organization_id=second_org, workspace_id=second_workspace)
    ) as session:
        assert set((await session.execute(text("SELECT id FROM workspaces"))).scalars()) == {
            second_workspace
        }
        assert set(
            (
                await session.execute(
                    text("SELECT id FROM groups WHERE name = 'Finance'")
                )
            ).scalars()
        ) == {second_group}
        assert set(
            (
                await session.execute(
                    text(
                        "SELECT principal_id FROM group_members WHERE group_id = :group_id"
                    ),
                    {"group_id": first_group},
                )
            ).scalars()
        ) == set()


@pytest.mark.asyncio
async def test_cross_workspace_group_ids_return_absent_or_reject(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """同一 Organization 内，跨 Workspace 的 Group 不可见也不可写。"""
    organization_id, first_workspace, second_workspace = uuid4(), uuid4(), uuid4()
    group_id, principal_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, first_workspace, workspace_name="Sales"
    )
    second_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, second_context) as session:
        await TenantRepository(session, second_context).create_workspace(
            second_workspace, name="Eng"
        )
    await _seed_principal(identity_sessions, principal_id)

    async with tenant_session(
        sessions, TenantContext(organization_id=organization_id, workspace_id=first_workspace)
    ) as session:
        repository = IdentityRepository(
            session, TenantContext(organization_id=organization_id, workspace_id=first_workspace)
        )
        await repository.create_group(
            group_id, organization_id=organization_id, workspace_id=first_workspace, name="Finance"
        )

    wrong_workspace_context = TenantContext(
        organization_id=organization_id, workspace_id=second_workspace
    )
    async with tenant_session(sessions, wrong_workspace_context) as session:
        repository = IdentityRepository(session, wrong_workspace_context)
        with pytest.raises(TenantScopeError):
            await repository.get_group(
                group_id, organization_id=organization_id, workspace_id=first_workspace
            )
        assert (
            await repository.get_group(
                group_id, organization_id=organization_id, workspace_id=second_workspace
            )
        ) is None
        assert await session.scalar(
            text("SELECT count(*) FROM groups WHERE id = :group_id"),
            {"group_id": group_id},
        ) == 0
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    """
                    INSERT INTO group_members (group_id, organization_id, workspace_id, principal_id)
                    VALUES (:group_id, :organization_id, :workspace_id, :principal_id)
                    """
                ),
                {
                    "group_id": group_id,
                    "organization_id": organization_id,
                    "workspace_id": first_workspace,
                    "principal_id": principal_id,
                },
            )


@pytest.mark.asyncio
async def test_same_name_groups_in_different_workspaces_of_same_org_allowed(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, first_workspace, second_workspace = uuid4(), uuid4(), uuid4()
    first_group, second_group = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, first_workspace, workspace_name="Sales"
    )
    second_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, second_context) as session:
        await TenantRepository(session, second_context).create_workspace(
            second_workspace, name="Eng"
        )

    first_group_context = TenantContext(
        organization_id=organization_id, workspace_id=first_workspace
    )
    async with tenant_session(sessions, first_group_context) as session:
        repository = IdentityRepository(session, first_group_context)
        await repository.create_group(
            first_group,
            organization_id=organization_id,
            workspace_id=first_workspace,
            name="Finance",
        )
    second_group_context = TenantContext(
        organization_id=organization_id, workspace_id=second_workspace
    )
    async with tenant_session(sessions, second_group_context) as session:
        repository = IdentityRepository(session, second_group_context)
        await repository.create_group(
            second_group,
            organization_id=organization_id,
            workspace_id=second_workspace,
            name="Finance",
        )

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            "SELECT id, organization_id, workspace_id, name FROM groups "
            "WHERE name = 'Finance' AND organization_id = $1",
            organization_id,
        )
        assert {(row["id"], row["workspace_id"]) for row in rows} == {
            (first_group, first_workspace),
            (second_group, second_workspace),
        }
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_guessed_cross_org_ids_return_absent_or_reject(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    first_org, second_org = uuid4(), uuid4()
    first_workspace, second_workspace = uuid4(), uuid4()
    principal_id, group_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, first_org, first_workspace, workspace_name="Sales"
    )
    await _seed_organization_and_workspace(
        sessions, second_org, second_workspace, workspace_name="Eng"
    )
    await _seed_principal(identity_sessions, principal_id)

    async with tenant_session(sessions, TenantContext(organization_id=first_org)) as session:
        repository = IdentityRepository(session, TenantContext(organization_id=first_org))
        await repository.add_membership(
            principal_id=principal_id,
            organization_id=first_org,
            role_bindings=frozenset({"member"}),
        )
    async with tenant_session(
        sessions, TenantContext(organization_id=first_org, workspace_id=first_workspace)
    ) as session:
        repository = IdentityRepository(
            session, TenantContext(organization_id=first_org, workspace_id=first_workspace)
        )
        await repository.create_group(
            group_id,
            organization_id=first_org,
            workspace_id=first_workspace,
            name="Finance",
        )

    second_org_context = TenantContext(organization_id=second_org)
    async with tenant_session(sessions, second_org_context) as session:
        repository = IdentityRepository(session, second_org_context)
        with pytest.raises(TenantScopeError):
            await repository.get_membership(
                principal_id=principal_id, organization_id=first_org
            )
        assert (
            await repository.get_membership(
                principal_id=principal_id, organization_id=second_org
            )
        ) is None
        assert await session.scalar(
            text(
                "SELECT count(*) FROM memberships WHERE principal_id = :principal_id "
                "AND organization_id = :organization_id"
            ),
            {"principal_id": principal_id, "organization_id": first_org},
        ) == 0
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    """
                    INSERT INTO memberships (principal_id, organization_id, role_bindings)
                    VALUES (:principal_id, :organization_id, '["member"]'::jsonb)
                    """
                ),
                {"principal_id": principal_id, "organization_id": first_org},
            )

    second_workspace_context = TenantContext(
        organization_id=second_org, workspace_id=second_workspace
    )
    async with tenant_session(sessions, second_workspace_context) as session:
        repository = IdentityRepository(session, second_workspace_context)
        with pytest.raises(TenantScopeError):
            await repository.get_group(
                group_id, organization_id=first_org, workspace_id=first_workspace
            )


@pytest.mark.asyncio
async def test_membership_commands_and_scope_separation(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    async with identity_sessions() as session, session.begin():
        identity_repository = IdentityStore(session)
        principal = await create_user(
            identity_repository,  # type: ignore[arg-type]  # identity-global 子集
            issuer="https://idp.example.com",
            subject="alice-scopes",
        )

    org_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        outcome = await add_org_membership(
            repository,
            principal_id=principal.id,
            organization_id=organization_id,
            role_bindings=frozenset({"owner"}),
        )
        assert outcome.created is True
        assert outcome.response["principal_id"] == str(principal.id)

    workspace_context = TenantContext(
        organization_id=organization_id, workspace_id=workspace_id
    )
    async with tenant_session(sessions, workspace_context) as session:
        repository = IdentityRepository(session, workspace_context)
        workspace_membership = await add_workspace_membership(
            repository,
            principal_id=principal.id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            role_bindings=frozenset({"builder"}),
        )
        assert workspace_membership.role_bindings == frozenset({"builder"})
        assert workspace_membership.workspace_id == workspace_id

    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        assert [m.principal_id for m in await repository.list_memberships(
            organization_id=organization_id
        )] == [principal.id]
        with pytest.raises(TenantScopeError):
            await repository.list_workspace_memberships(workspace_id=workspace_id)

    async with tenant_session(sessions, workspace_context) as session:
        repository = IdentityRepository(session, workspace_context)
        assert [m.principal_id for m in await repository.list_workspace_memberships(
            workspace_id=workspace_id
        )] == [principal.id]
        with pytest.raises(TenantScopeError):
            await repository.list_memberships(organization_id=organization_id)

    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        removal = await remove_org_membership(
            repository, principal_id=principal.id, organization_id=organization_id
        )
        assert removal.created is True
        assert await repository.list_memberships(organization_id=organization_id) == []


@pytest.mark.asyncio
async def test_principal_can_belong_to_multiple_organizations(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    first_org, second_org = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, first_org, uuid4(), workspace_name="Sales"
    )
    await _seed_organization_and_workspace(
        sessions, second_org, uuid4(), workspace_name="Eng"
    )
    async with identity_sessions() as session, session.begin():
        identity_repository = IdentityStore(session)
        principal = await create_user(
            identity_repository,  # type: ignore[arg-type]  # identity-global 子集
            issuer="https://idp.example.com",
            subject="alice-multi-org",
        )

    for organization_id in (first_org, second_org):
        context = TenantContext(organization_id=organization_id)
        async with tenant_session(sessions, context) as session:
            repository = IdentityRepository(session, context)
            await add_org_membership(
                repository, principal_id=principal.id, organization_id=organization_id
            )

    for organization_id in (first_org, second_org):
        context = TenantContext(organization_id=organization_id)
        async with tenant_session(sessions, context) as session:
            repository = IdentityRepository(session, context)
            memberships = await repository.list_memberships(organization_id=organization_id)
            assert [m.principal_id for m in memberships] == [principal.id]


@pytest.mark.asyncio
async def test_workspace_membership_requires_matching_workspace_context(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, first_workspace, second_workspace = uuid4(), uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, first_workspace, workspace_name="Sales"
    )
    second_workspace_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, second_workspace_context) as session:
        await TenantRepository(session, second_workspace_context).create_workspace(
            second_workspace, name="Eng"
        )
    principal_id = uuid4()
    await _seed_principal(identity_sessions, principal_id)

    org_only_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, org_only_context) as session:
        repository = IdentityRepository(session, org_only_context)
        with pytest.raises(TenantScopeError):
            await repository.add_workspace_membership(
                principal_id=principal_id,
                organization_id=organization_id,
                workspace_id=first_workspace,
                role_bindings=frozenset(),
            )

    mismatched_context = TenantContext(
        organization_id=organization_id, workspace_id=second_workspace
    )
    async with tenant_session(sessions, mismatched_context) as session:
        repository = IdentityRepository(session, mismatched_context)
        with pytest.raises(TenantScopeError):
            await repository.add_workspace_membership(
                principal_id=principal_id,
                organization_id=organization_id,
                workspace_id=first_workspace,
                role_bindings=frozenset(),
            )

    matching_context = TenantContext(
        organization_id=organization_id, workspace_id=first_workspace
    )
    async with tenant_session(sessions, matching_context) as session:
        repository = IdentityRepository(session, matching_context)
        membership = await repository.add_workspace_membership(
            principal_id=principal_id,
            organization_id=organization_id,
            workspace_id=first_workspace,
            role_bindings=frozenset({"builder"}),
        )
        assert membership.workspace_id == first_workspace
        assert (
            await repository.get_workspace_membership(
                principal_id=principal_id, workspace_id=first_workspace
            )
        ) == membership


@pytest.mark.asyncio
async def test_group_operations_require_matching_workspace_context(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    group_id = uuid4()
    org_only_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, org_only_context) as session:
        repository = IdentityRepository(session, org_only_context)
        with pytest.raises(TenantScopeError):
            await repository.create_group(
                group_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                name="Finance",
            )

    workspace_context = TenantContext(
        organization_id=organization_id, workspace_id=workspace_id
    )
    async with tenant_session(sessions, workspace_context) as session:
        repository = IdentityRepository(session, workspace_context)
        created, group = await repository.create_group(
            group_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            name="Finance",
        )
        assert created is True
        assert group.workspace_id == workspace_id
        assert (
            await repository.get_group(
                group_id, organization_id=organization_id, workspace_id=workspace_id
            )
        ) == group
        assert [
            g.id for g in await repository.list_groups(
                organization_id=organization_id, workspace_id=workspace_id
            )
        ] == [group_id]


@pytest.mark.asyncio
async def test_disabled_principal_blocked_from_new_memberships(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    async with identity_sessions() as session, session.begin():
        identity_repository = IdentityStore(session)
        principal = await create_user(
            identity_repository,  # type: ignore[arg-type]  # identity-global 子集
            issuer="https://idp.example.com",
            subject="alice-disabled",
        )

    org_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        await add_org_membership(
            repository,
            principal_id=principal.id,
            organization_id=organization_id,
        )

    async with identity_sessions() as session, session.begin():
        await disable_principal(IdentityStore(session), principal.id)  # type: ignore[arg-type]

    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        with pytest.raises(PrincipalDisabledError):
            await add_org_membership(
                repository, principal_id=principal.id, organization_id=organization_id
            )
        with pytest.raises(PrincipalDisabledError):
            await repository.add_membership(
                principal_id=principal.id,
                organization_id=organization_id,
                role_bindings=frozenset({"owner"}),
            )
        removal = await remove_org_membership(
            repository, principal_id=principal.id, organization_id=organization_id
        )
        assert removal.created is True

    workspace_context = TenantContext(
        organization_id=organization_id, workspace_id=workspace_id
    )
    async with tenant_session(sessions, workspace_context) as session:
        repository = IdentityRepository(session, workspace_context)
        with pytest.raises(PrincipalDisabledError):
            await add_workspace_membership(
                repository,
                principal_id=principal.id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                role_bindings=frozenset(),
            )
        group_id = uuid4()
        await repository.create_group(
            group_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            name="Finance",
        )
        with pytest.raises(PrincipalDisabledError):
            await add_group_member(
                repository,
                group_id=group_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
            )


@pytest.mark.asyncio
async def test_group_member_add_is_idempotent_on_retry(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    principal_id = uuid4()
    await _seed_principal(identity_sessions, principal_id)
    context = TenantContext(
        organization_id=organization_id, workspace_id=workspace_id
    )
    group_id = uuid4()
    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        await repository.create_group(
            group_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            name="Finance",
        )

    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        first = await add_group_member(
            repository,
            group_id=group_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
        )
        second = await add_group_member(
            repository,
            group_id=group_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
        )
        assert first == second
        members = await repository.list_group_members(
            group_id=group_id, organization_id=organization_id, workspace_id=workspace_id
        )
        assert members == [first]


@pytest.mark.asyncio
async def test_organization_bootstrap_creates_org_and_owner_membership_atomically(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    principal_id = uuid4()
    await _seed_principal(identity_sessions, principal_id)
    organization_id = uuid4()
    context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        first = await create_organization(
            repository,
            organization_id=organization_id,
            owner_principal_id=principal_id,
            idempotency=_idempotency("bootstrap-key", "1"),
        )
        replayed = await create_organization(
            repository,
            organization_id=organization_id,
            owner_principal_id=principal_id,
            idempotency=_idempotency("bootstrap-key", "1"),
        )
        assert first.created is True
        assert replayed.created is False
        assert replayed.response == first.response

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM organizations WHERE id = $1", organization_id
        ) == 1
        rows = await connection.fetch(
            "SELECT principal_id, role_bindings FROM memberships WHERE organization_id = $1",
            organization_id,
        )
        assert len(rows) == 1
        assert rows[0]["principal_id"] == principal_id
        assert json.loads(rows[0]["role_bindings"]) == ["owner"]
    finally:
        await connection.close()


# --------------------------------------------------------------------------- API 基础


def test_api_composition_requires_explicit_actor_dependency() -> None:
    with pytest.raises(TypeError):
        create_organizations_router()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_workspaces_router()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_memberships_router()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_api_requires_idempotency_key_for_mutations(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    principal_id = uuid4()
    await _seed_principal(identity_sessions, principal_id)
    app = FastAPI()
    app.include_router(
        create_organizations_router(
            actor_dependency=lambda: _no_org_actor(principal_id), sessions=sessions, identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/organizations", json={"organization_id": str(uuid4())}
        )
        assert response.status_code == 422

        organization_id = uuid4()
        body = {"organization_id": str(organization_id)}
        first = await client.post(
            "/api/v1/organizations", json=body, headers={"Idempotency-Key": "create-org-1"}
        )
        assert first.status_code == 201
        assert first.json() == {"id": str(organization_id), "status": "active"}

        replayed = await client.post(
            "/api/v1/organizations", json=body, headers={"Idempotency-Key": "create-org-1"}
        )
        assert replayed.status_code == 200
        assert replayed.json() == first.json()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM organizations WHERE id = $1", organization_id
        ) == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_organization_bootstrap_and_list(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id = uuid4()
    principal_id = uuid4()
    await _seed_principal(identity_sessions, principal_id)
    actor = _no_org_actor(principal_id)
    app = FastAPI()
    app.include_router(
        create_organizations_router(actor_dependency=lambda: actor, sessions=sessions, identity_sessions=identity_sessions, policy_enforcer=FakePolicyEnforcer())
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/organizations")
        assert response.status_code == 200
        assert response.json() == []

        response = await client.post(
            "/api/v1/organizations",
            json={"organization_id": str(organization_id)},
            headers={"Idempotency-Key": "bootstrap"},
        )
        assert response.status_code == 201
        assert response.json() == {"id": str(organization_id), "status": "active"}

        # S1-T2 完成 T1 阻断语义：GET /api/v1/organizations 返回已认证 principal 的
        # 全部组织；bootstrap 已授予创建者 Owner membership，因此这里不再为空
        response = await client.get("/api/v1/organizations")
        assert response.status_code == 200
        assert response.json() == [
            {"id": str(organization_id), "status": "active"}
        ]

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        owner_rows = await connection.fetch(
            "SELECT role_bindings FROM memberships WHERE organization_id = $1",
            organization_id,
        )
        assert len(owner_rows) == 1
        assert json.loads(owner_rows[0]["role_bindings"]) == ["owner"]
    finally:
        await connection.close()

    with_org_app = FastAPI()
    with_org_app.include_router(
        create_organizations_router(
            actor_dependency=lambda: ActorContext(
                principal_id=principal_id, organization_id=organization_id
            ),
            sessions=sessions,
            identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=with_org_app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/organizations")
        assert response.status_code == 200
        assert response.json() == [{"id": str(organization_id), "status": "active"}]


@pytest.mark.asyncio
async def test_api_organization_read_is_scoped_to_actor_org(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    first_org, second_org = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, first_org, uuid4(), workspace_name="Sales"
    )
    await _seed_organization_and_workspace(
        sessions, second_org, uuid4(), workspace_name="Eng"
    )
    app = FastAPI()
    app.include_router(
        create_organizations_router(
            actor_dependency=lambda: _org_actor(first_org), sessions=sessions, identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/organizations/{first_org}")
        assert response.status_code == 200
        assert response.json()["id"] == str(first_org)
        # 读操作防枚举：跨租户 org 与不存在的 id 统一 404，不泄露组织是否存在
        response = await client.get(f"/api/v1/organizations/{second_org}")
        assert response.status_code == 404
        response = await client.get(f"/api/v1/organizations/{uuid4()}")
        assert response.status_code == 404

    no_org_app = FastAPI()
    no_org_app.include_router(
        create_organizations_router(actor_dependency=_no_org_actor, sessions=sessions, identity_sessions=identity_sessions, policy_enforcer=FakePolicyEnforcer())
    )
    async with AsyncClient(
        transport=ASGITransport(app=no_org_app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/organizations/{first_org}")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_workspaces_and_groups_endpoints_enforce_scope(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, first_workspace, second_workspace = uuid4(), uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, first_workspace, workspace_name="Sales"
    )
    second_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, second_context) as session:
        await TenantRepository(session, second_context).create_workspace(
            second_workspace, name="Eng"
        )
    other_org = uuid4()
    await _seed_organization_and_workspace(
        sessions, other_org, uuid4(), workspace_name="Ops"
    )

    org_app = FastAPI()
    org_actor = _org_actor(organization_id)
    await _seed_actor_principal(identity_sessions, org_actor.principal_id)
    org_app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: org_actor, sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=org_app), base_url="http://test") as client:
        # 组织级 actor 创建 workspace（org 上下文，workspace_id 为空）
        workspace_body = {
            "workspace_id": str(uuid4()),
            "name": "Sales-2",
        }
        response = await client.post(
            f"/api/v1/organizations/{organization_id}/workspaces",
            json=workspace_body,
            headers={"Idempotency-Key": "create-workspace"},
        )
        assert response.status_code == 201
        assert response.json()["organization_id"] == str(organization_id)
        assert response.json()["name"] == "Sales-2"
        replayed = await client.post(
            f"/api/v1/organizations/{organization_id}/workspaces",
            json=workspace_body,
            headers={"Idempotency-Key": "create-workspace"},
        )
        assert replayed.status_code == 200
        assert replayed.json() == response.json()

        response = await client.get(f"/api/v1/organizations/{organization_id}/workspaces")
        assert response.status_code == 200
        assert {workspace["id"] for workspace in response.json()} == {
            str(first_workspace),
            str(second_workspace),
            workspace_body["workspace_id"],
        }

        # 幂等冲突：同 key + 不同 payload（org 级 mutation 的键空间稳定）→ 409
        conflicting = await client.post(
            f"/api/v1/organizations/{organization_id}/workspaces",
            json={"workspace_id": str(uuid4()), "name": "Conflicting"},
            headers={"Idempotency-Key": "create-workspace"},
        )
        assert conflicting.status_code == 409

        # 跨租户：写 403，读 404
        response = await client.post(
            f"/api/v1/organizations/{other_org}/workspaces",
            json=workspace_body,
            headers={"Idempotency-Key": "cross-org-workspace"},
        )
        assert response.status_code == 403
        response = await client.get(f"/api/v1/organizations/{other_org}/workspaces")
        assert response.status_code == 404

    workspace_app = FastAPI()
    workspace_app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: _org_actor(
                organization_id, workspace_id=first_workspace
            ),
            sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=workspace_app), base_url="http://test"
    ) as client:
        # 组织级 mutation 要求组织级 actor
        response = await client.post(
            f"/api/v1/organizations/{organization_id}/workspaces",
            json={"workspace_id": str(uuid4()), "name": "Blocked"},
            headers={"Idempotency-Key": "ws-actor-workspace"},
        )
        assert response.status_code == 403

        # workspace actor 创建 Group；名称唯一范围是 Workspace
        first_group_id = uuid4()
        group_body = {"group_id": str(first_group_id), "name": "Finance"}
        response = await client.post(
            f"/api/v1/workspaces/{first_workspace}/groups",
            json=group_body,
            headers={"Idempotency-Key": "create-group-1"},
        )
        assert response.status_code == 201
        assert response.json() == {
            "id": str(first_group_id),
            "organization_id": str(organization_id),
            "workspace_id": str(first_workspace),
            "name": "Finance",
        }
        replayed = await client.post(
            f"/api/v1/workspaces/{first_workspace}/groups",
            json=group_body,
            headers={"Idempotency-Key": "create-group-1"},
        )
        assert replayed.status_code == 200
        assert replayed.json() == response.json()
        response = await client.get(f"/api/v1/workspaces/{first_workspace}/groups")
        assert response.status_code == 200
        assert [group["id"] for group in response.json()] == [str(first_group_id)]

        # 跨 workspace：读 404、写 403
        response = await client.post(
            f"/api/v1/workspaces/{second_workspace}/groups",
            json=group_body,
            headers={"Idempotency-Key": "cross-ws-group"},
        )
        assert response.status_code == 403
        response = await client.get(f"/api/v1/workspaces/{second_workspace}/groups")
        assert response.status_code == 404
        # 同一 Workspace 内重名 Group 冲突（名称唯一范围是 Workspace）
        response = await client.post(
            f"/api/v1/workspaces/{first_workspace}/groups",
            json={"group_id": str(uuid4()), "name": "Finance"},
            headers={"Idempotency-Key": "create-group-2"},
        )
        assert response.status_code == 409

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM groups WHERE organization_id = $1", organization_id
        ) == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_members_endpoints_enforce_scope(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id = uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, uuid4(), workspace_name="Sales"
    )
    active_id, disabled_id, missing_id = uuid4(), uuid4(), uuid4()
    await _seed_principal(identity_sessions, active_id)
    await _seed_principal(identity_sessions, disabled_id)
    async with identity_sessions() as session, session.begin():
        identity_repository = IdentityStore(session)
        await disable_principal(identity_repository, disabled_id)  # type: ignore[arg-type]

    app = FastAPI()
    app.include_router(
        create_memberships_router(
            actor_dependency=lambda: _org_actor(organization_id), sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        member_body = {"principal_id": str(active_id), "role_bindings": ["member"]}
        response = await client.post(
            f"/api/v1/organizations/{organization_id}/members",
            json=member_body,
            headers={"Idempotency-Key": "add-member-1"},
        )
        assert response.status_code == 201
        assert response.json()["principal_id"] == str(active_id)
        assert response.json()["role_bindings"] == ["member"]
        replayed = await client.post(
            f"/api/v1/organizations/{organization_id}/members",
            json=member_body,
            headers={"Idempotency-Key": "add-member-1"},
        )
        assert replayed.status_code == 200
        assert replayed.json() == response.json()

        response = await client.get(f"/api/v1/organizations/{organization_id}/members")
        assert response.status_code == 200
        assert [member["principal_id"] for member in response.json()] == [str(active_id)]

        response = await client.post(
            f"/api/v1/organizations/{organization_id}/members",
            json={"principal_id": str(disabled_id), "role_bindings": ["member"]},
            headers={"Idempotency-Key": "add-member-disabled"},
        )
        assert response.status_code == 409

        response = await client.post(
            f"/api/v1/organizations/{organization_id}/members",
            json={"principal_id": str(missing_id), "role_bindings": ["member"]},
            headers={"Idempotency-Key": "add-member-missing"},
        )
        assert response.status_code == 404

        response = await client.delete(
            f"/api/v1/organizations/{organization_id}/members/{active_id}",
            headers={"Idempotency-Key": "remove-member-1"},
        )
        assert response.status_code == 204
        replayed = await client.delete(
            f"/api/v1/organizations/{organization_id}/members/{active_id}",
            headers={"Idempotency-Key": "remove-member-1"},
        )
        assert replayed.status_code == 204
        response = await client.get(f"/api/v1/organizations/{organization_id}/members")
        assert response.json() == []

    no_org_app = FastAPI()
    no_org_app.include_router(
        create_memberships_router(actor_dependency=_no_org_actor, sessions=sessions, policy_enforcer=FakePolicyEnforcer())
    )
    async with AsyncClient(
        transport=ASGITransport(app=no_org_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/organizations/{organization_id}/members",
            json=member_body,
            headers={"Idempotency-Key": "no-org-add"},
        )
        assert response.status_code == 403
        response = await client.get(f"/api/v1/organizations/{organization_id}/members")
        assert response.status_code == 404


# --------------------------------------------------------------------------- P0 修复契约（bootstrap 接管 / 资源碰撞 / fail-closed 解析）


@pytest.mark.asyncio
async def test_api_bootstrap_takeover_rejected(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """attacker（无组织 context）猜测已存在 victim org UUID：403，零写入。"""
    victim_org = uuid4()
    await _seed_organization_and_workspace(
        sessions, victim_org, uuid4(), workspace_name="Victim"
    )
    attacker_id = uuid4()
    await _seed_principal(identity_sessions, attacker_id)
    app = FastAPI()
    app.include_router(
        create_organizations_router(
            actor_dependency=lambda: _no_org_actor(attacker_id), sessions=sessions, identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/organizations",
            json={"organization_id": str(victim_org)},
            headers={"Idempotency-Key": "takeover"},
        )
        assert response.status_code == 403

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM memberships WHERE organization_id = $1", victim_org
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM idempotency_records WHERE organization_id = $1", victim_org
        ) == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_bootstrap_replay_confirms_owner(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """创建者重放 200 且响应一致；另一 principal 相同 key/body 必须 403 且零写入。"""
    creator_id, foreign_id = uuid4(), uuid4()
    await _seed_principal(identity_sessions, creator_id)
    await _seed_principal(identity_sessions, foreign_id)
    organization_id = uuid4()
    app = FastAPI()
    app.include_router(
        create_organizations_router(
            actor_dependency=lambda: _no_org_actor(creator_id), sessions=sessions, identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    transport = ASGITransport(app=app)
    body = {"organization_id": str(organization_id)}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/organizations", json=body, headers={"Idempotency-Key": "bootstrap-key"}
        )
        assert first.status_code == 201
        replayed = await client.post(
            "/api/v1/organizations", json=body, headers={"Idempotency-Key": "bootstrap-key"}
        )
        assert replayed.status_code == 200
        assert replayed.json() == first.json()

    foreign_app = FastAPI()
    foreign_app.include_router(
        create_organizations_router(
            actor_dependency=lambda: _no_org_actor(foreign_id), sessions=sessions, identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=foreign_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/organizations", json=body, headers={"Idempotency-Key": "bootstrap-key"}
        )
        assert response.status_code == 403

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM memberships WHERE organization_id = $1", organization_id
        ) == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_bootstrap_concurrent_same_request(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """并发相同 actor/key/body：一个 201、一个 200；最终仅一个 Organization、Owner、幂等记录。"""
    creator_id = uuid4()
    await _seed_principal(identity_sessions, creator_id)
    organization_id = uuid4()
    app = FastAPI()
    app.include_router(
        create_organizations_router(
            actor_dependency=lambda: _no_org_actor(creator_id), sessions=sessions, identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"organization_id": str(organization_id)}
        headers = {"Idempotency-Key": "concurrent-bootstrap"}
        results = await asyncio.gather(
            client.post("/api/v1/organizations", json=body, headers=headers),
            client.post("/api/v1/organizations", json=body, headers=headers),
        )
    statuses = sorted(result.status_code for result in results)
    assert statuses == [200, 201]
    assert results[0].json() == results[1].json()

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM organizations WHERE id = $1", organization_id
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM memberships WHERE organization_id = $1", organization_id
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM idempotency_records WHERE organization_id = $1",
            organization_id,
        ) == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_existing_workspace_collision_rejected(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """已存在 workspace_id + 新幂等键：无论 payload 相同/不同均 409，原资源不变，零新记录。"""
    organization_id = uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, uuid4(), workspace_name="Sales"
    )
    app = FastAPI()
    ws_actor = _org_actor(organization_id)
    await _seed_actor_principal(identity_sessions, ws_actor.principal_id)
    app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: ws_actor, sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created_id = uuid4()
        body = {"workspace_id": str(created_id), "name": "Sales-2"}
        first = await client.post(
            f"/api/v1/organizations/{organization_id}/workspaces",
            json=body,
            headers={"Idempotency-Key": "ws-create-key"},
        )
        assert first.status_code == 201
        replayed = await client.post(
            f"/api/v1/organizations/{organization_id}/workspaces",
            json=body,
            headers={"Idempotency-Key": "ws-create-key"},
        )
        assert replayed.status_code == 200
        assert replayed.json() == first.json()

        # 已存在 id + 新 key：payload 相同 / 不同均 409
        for payload in (body, {"workspace_id": str(created_id), "name": "Renamed"}):
            response = await client.post(
                f"/api/v1/organizations/{organization_id}/workspaces",
                json=payload,
                headers={"Idempotency-Key": "new-key"},
            )
            assert response.status_code == 409

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT name FROM workspaces WHERE id = $1", created_id
        ) == "Sales-2"
        assert await connection.fetchval(
            "SELECT count(*) FROM idempotency_records "
            "WHERE organization_id = $1 AND scope = 'organization.workspace.create' "
            "AND idempotency_key = 'new-key'",
            organization_id,
        ) == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_workspace_membership_grant_idempotency(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """workspace membership 授予消费 Idempotency-Key（D-2：mutation 模式一致性）。

    - 同 key 同 body 重放 → 200（原结果，零新写）
    - 同 key 异 body → 409（IdempotencyConflict）
    """
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Idem"
    )
    admin = _org_actor(organization_id, workspace_id=workspace_id)
    # workspace_admin 绑定：FakePolicyEnforcer 不看角色，但绑定使 actor 语义真实
    target, other = uuid4(), uuid4()
    for principal_id in (admin.principal_id, target, other):
        await _seed_actor_principal(identity_sessions, principal_id)

    app = FastAPI()
    app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: admin, sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"principal_id": str(target), "role_bindings": ["agent_builder"]}
        first = await client.post(
            f"/api/v1/workspaces/{workspace_id}/memberships",
            json=body,
            headers={"Idempotency-Key": "grant-key"},
        )
        assert first.status_code == 201, first.text

        replayed = await client.post(
            f"/api/v1/workspaces/{workspace_id}/memberships",
            json=body,
            headers={"Idempotency-Key": "grant-key"},
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == first.json()

        # 同 key 异 body：不得静默授予第二个主体
        conflicting = await client.post(
            f"/api/v1/workspaces/{workspace_id}/memberships",
            json={"principal_id": str(other), "role_bindings": ["agent_builder"]},
            headers={"Idempotency-Key": "grant-key"},
        )
        assert conflicting.status_code == 409, conflicting.text

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM workspace_memberships WHERE workspace_id = $1"
            " AND principal_id = $2",
            workspace_id,
            other,
        ) == 0, "同 key 异 body 不得产生新授予"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_membership_listing_requires_matching_workspace_context(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """workspace 上下文 actor 读其他 workspace 的名单 → 404（GUC 纪律，防枚举）。"""
    organization_id, first_ws, second_ws = uuid4(), uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, first_ws, workspace_name="First"
    )
    second_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, second_context) as session:
        await TenantRepository(session, second_context).create_workspace(
            second_ws, name="Second"
        )
    actor = _org_actor(organization_id, workspace_id=first_ws)
    await _seed_actor_principal(identity_sessions, actor.principal_id)

    app = FastAPI()
    app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: actor, sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/workspaces/{second_ws}/memberships")
        assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_api_existing_group_collision_rejected(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    app = FastAPI()
    app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: _org_actor(
                organization_id, workspace_id=workspace_id
            ),
            sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        group_id = uuid4()
        body = {"group_id": str(group_id), "name": "Finance"}
        first = await client.post(
            f"/api/v1/workspaces/{workspace_id}/groups",
            json=body,
            headers={"Idempotency-Key": "group-create-key"},
        )
        assert first.status_code == 201
        replayed = await client.post(
            f"/api/v1/workspaces/{workspace_id}/groups",
            json=body,
            headers={"Idempotency-Key": "group-create-key"},
        )
        assert replayed.status_code == 200
        assert replayed.json() == first.json()

        for payload in (body, {"group_id": str(group_id), "name": "Ops"}):
            response = await client.post(
                f"/api/v1/workspaces/{workspace_id}/groups",
                json=payload,
                headers={"Idempotency-Key": "new-key"},
            )
            assert response.status_code == 409

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT name FROM groups WHERE id = $1", group_id
        ) == "Finance"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_request_models_reject_unknown_fields(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """请求模型 fail closed：未知字段一律 422。"""
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    principal_id = uuid4()
    await _seed_principal(identity_sessions, principal_id)
    org_app = FastAPI()
    org_app.include_router(
        create_organizations_router(
            actor_dependency=lambda: _org_actor(organization_id), sessions=sessions, identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    workspaces_app = FastAPI()
    workspaces_app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: _org_actor(
                organization_id, workspace_id=workspace_id
            ),
            sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    members_app = FastAPI()
    members_app.include_router(
        create_memberships_router(
            actor_dependency=lambda: _org_actor(organization_id), sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=org_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/organizations",
            json={"organization_id": str(uuid4()), "name": "extra"},
            headers={"Idempotency-Key": "k"},
        )
        assert response.status_code == 422

    async with AsyncClient(
        transport=ASGITransport(app=workspaces_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/organizations/{organization_id}/workspaces",
            json={"workspace_id": str(uuid4()), "name": "X", "owner": "extra"},
            headers={"Idempotency-Key": "k"},
        )
        assert response.status_code == 422
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/groups",
            json={"group_id": str(uuid4()), "name": "G", "extra": 1},
            headers={"Idempotency-Key": "k"},
        )
        assert response.status_code == 422

    async with AsyncClient(
        transport=ASGITransport(app=members_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/organizations/{organization_id}/members",
            json={"principal_id": str(principal_id), "role_bindings": ["member"], "note": "x"},
            headers={"Idempotency-Key": "k"},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_api_idempotency_key_rejects_missing_empty_whitespace(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """缺失、空字符串、纯空白 Idempotency-Key 一律 422。"""
    app = FastAPI()
    app.include_router(
        create_organizations_router(actor_dependency=_no_org_actor, sessions=sessions, identity_sessions=identity_sessions, policy_enforcer=FakePolicyEnforcer())
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for headers in (
            None,
            {"Idempotency-Key": ""},
            {"Idempotency-Key": "   "},
        ):
            response = await client.post(
                "/api/v1/organizations",
                json={"organization_id": str(uuid4())},
                headers=headers,
            )
            assert response.status_code == 422


# --------------------------------------------------------------------------- ABA 幂等重放契约（Repair-3）


@pytest.mark.asyncio
async def test_api_stale_add_replay_does_not_resurrect_membership(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """ADD 后 DELETE，重放旧 ADD：返回原结果，membership 保持不存在，幂等记录不新增。"""
    organization_id, principal_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, uuid4(), workspace_name="Sales"
    )
    await _seed_principal(identity_sessions, principal_id)
    context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        first = await add_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            role_bindings=frozenset({"member"}),
            idempotency=_idempotency("add-old", "1"),
        )
        assert first.created is True
        removal = await remove_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            idempotency=_idempotency("delete-new", "1"),
        )
        assert removal.created is True

    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        replayed = await add_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            role_bindings=frozenset({"member"}),
            idempotency=_idempotency("add-old", "1"),
        )
        assert replayed.created is False
        assert replayed.response == first.response
        assert (
            await repository.get_membership(
                principal_id=principal_id, organization_id=organization_id
            )
        ) is None

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM idempotency_records WHERE organization_id = $1",
            organization_id,
        ) == 2
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_stale_delete_replay_does_not_remove_replacement(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """DELETE 后用新 key 重新 ADD，重放旧 DELETE：替代 membership 及其角色保持不变。"""
    organization_id, principal_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, uuid4(), workspace_name="Sales"
    )
    await _seed_principal(identity_sessions, principal_id)
    context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        await add_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            role_bindings=frozenset({"member"}),
            idempotency=_idempotency("add-first", "1"),
        )
        removal = await remove_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            idempotency=_idempotency("delete-old", "1"),
        )
        assert removal.created is True
        replacement = await add_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            role_bindings=frozenset({"builder"}),
            idempotency=_idempotency("add-replacement", "1"),
        )
        assert replacement.created is True

    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        replayed = await remove_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            idempotency=_idempotency("delete-old", "1"),
        )
        assert replayed.created is False
        assert replayed.response == removal.response
        membership = await repository.get_membership(
            principal_id=principal_id, organization_id=organization_id
        )
        assert membership is not None
        assert membership.role_bindings == frozenset({"builder"})

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM idempotency_records WHERE organization_id = $1",
            organization_id,
        ) == 3
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_conflicting_member_replay_is_side_effect_free(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """同 key 不同 digest 的 ADD/DELETE 重放：409 前零写入，membership 逐字段不变。"""
    organization_id, principal_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, uuid4(), workspace_name="Sales"
    )
    await _seed_principal(identity_sessions, principal_id)
    context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        await add_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            role_bindings=frozenset({"member"}),
            idempotency=_idempotency("add-key", "1"),
        )
        await remove_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            idempotency=_idempotency("delete-key", "1"),
        )
        replacement = await add_org_membership(
            repository,
            principal_id=principal_id,
            organization_id=organization_id,
            role_bindings=frozenset({"builder"}),
            idempotency=_idempotency("add-replacement", "1"),
        )
        assert replacement.created is True

    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        with pytest.raises(IdempotencyConflict):
            await add_org_membership(
                repository,
                principal_id=principal_id,
                organization_id=organization_id,
                role_bindings=frozenset({"owner"}),
                idempotency=_idempotency("add-key", "2"),
            )
        with pytest.raises(IdempotencyConflict):
            await remove_org_membership(
                repository,
                principal_id=principal_id,
                organization_id=organization_id,
                idempotency=_idempotency("delete-key", "2"),
            )
        membership = await repository.get_membership(
            principal_id=principal_id, organization_id=organization_id
        )
        assert membership is not None
        assert membership.role_bindings == frozenset({"builder"})

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM idempotency_records WHERE organization_id = $1",
            organization_id,
        ) == 3
    finally:
        await connection.close()
