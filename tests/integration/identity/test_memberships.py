"""S1-T1 RED：identity 迁移、RLS 隔离与 tenant-scoped repository 集成契约。

冻结的事实源：
- 边界裁决 S1-T1：Principal / ExternalIdentity 是 identity-global；Membership、
  WorkspaceMembership、Group、GroupMember 是 tenant-owned，缺 tenant context fail closed，
  repository 显式携带 tenant predicate，RLS 只是纵深防御；
- PERMISSIONS §5：所有租户表启用 FORCE RLS；app role 不是 owner/BYPASSRLS；
  两个 Organization 用重名 Workspace/Group 互不泄露；guessed cross-org ID 返回 absent/拒绝；
- 总设计 §9.2：repository 仍显式传 org/workspace。

API 层（S1-T1 "API 基础"）：routers 必须经显式 actor dependency 注入身份与 tenant context，
没有默认 allow；缺少时拒绝。OIDC 真实依赖注入在 S1-T2，本文件只用测试 stub。
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
from fastapi import FastAPI
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
    PrincipalDisabledError,
    add_group_member,
    add_org_membership,
    add_workspace_membership,
    create_group,
    create_user,
    disable_principal,
    remove_org_membership,
)
from zhiwei.identity.domain import ActorContext, PrincipalKind, PrincipalStatus
from zhiwei.identity.repositories import IdentityRepository
from zhiwei.persistence.repositories import TenantRepository
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
    sessions: async_sessionmaker[AsyncSession], principal_id: UUID
) -> None:
    async with sessions() as session, session.begin():
        repository = IdentityRepository(session, context=None)
        await repository.create_principal(
            principal_id, kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
        )


def _org_actor(organization_id: UUID, *, workspace_id: UUID | None = None) -> ActorContext:
    return ActorContext(
        principal_id=uuid4(), organization_id=organization_id, workspace_id=workspace_id
    )


# --------------------------------------------------------------------------- migration 与 RLS 结构


@pytest.mark.asyncio
async def test_identity_tables_exist_with_expected_rls_structure(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
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
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    principal_id = uuid4()
    async with sessions() as session:
        repository = IdentityRepository(session, context=None)
        principal = await repository.create_principal(
            principal_id, kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
        )
        assert principal.id == principal_id
        assert (await repository.get_principal(principal_id)) == principal
        identity = await repository.bind_external_identity(
            issuer="https://idp.example.com", subject="alice", principal_id=principal_id
        )
        assert identity.stable_key == ("https://idp.example.com", "alice")
        assert (
            await repository.get_external_identity(
                issuer="https://idp.example.com", subject="alice"
            )
        ) == identity
        with pytest.raises(TenantContextRequired):
            await repository.add_membership(
                principal_id=principal_id, organization_id=uuid4(), role_bindings=frozenset()
            )


@pytest.mark.asyncio
async def test_external_identity_issuer_subject_unique_at_database_level(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    first_id, second_id = uuid4(), uuid4()
    async with sessions() as session:
        repository = IdentityRepository(session, context=None)
        await repository.create_principal(
            first_id, kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
        )
        await repository.create_principal(
            second_id, kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
        )
        await repository.bind_external_identity(
            issuer="https://idp.example.com", subject="alice", principal_id=first_id
        )
        with pytest.raises(ExternalIdentityConflictError):
            await repository.bind_external_identity(
                issuer="https://idp.example.com", subject="alice", principal_id=second_id
            )


@pytest.mark.asyncio
async def test_missing_tenant_context_denies_identity_tenant_tables(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
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
            "INSERT INTO groups (id, organization_id, name, schema_version) VALUES ($1, $2, 'seeded', 1)",
            group_id,
            organization_id,
        )
        await connection.execute(
            """
            INSERT INTO group_members (group_id, organization_id, principal_id)
            VALUES ($1, $2, $3)
            """,
            group_id,
            organization_id,
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
                "INSERT INTO groups (id, organization_id, name, schema_version) VALUES ($1, $2, 'blocked', 1)",
                uuid4(),
                organization_id,
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_same_name_workspace_and_group_do_not_leak_across_orgs(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
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
    await _seed_principal(sessions, first_member)
    await _seed_principal(sessions, second_member)

    async with tenant_session(sessions, TenantContext(organization_id=first_org)) as session:
        repository = IdentityRepository(session, TenantContext(organization_id=first_org))
        await repository.create_group(first_group, organization_id=first_org, name="Finance")
        await repository.add_group_member(
            group_id=first_group, organization_id=first_org, principal_id=first_member
        )
    async with tenant_session(sessions, TenantContext(organization_id=second_org)) as session:
        repository = IdentityRepository(session, TenantContext(organization_id=second_org))
        await repository.create_group(second_group, organization_id=second_org, name="Finance")
        await repository.add_group_member(
            group_id=second_group, organization_id=second_org, principal_id=second_member
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
                    text("SELECT principal_id FROM group_members WHERE group_id = :group_id"),
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
                    text("SELECT principal_id FROM group_members WHERE group_id = :group_id"),
                    {"group_id": first_group},
                )
            ).scalars()
        ) == set()


@pytest.mark.asyncio
async def test_guessed_cross_org_ids_return_absent_or_reject(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
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
    await _seed_principal(sessions, principal_id)

    async with tenant_session(sessions, TenantContext(organization_id=first_org)) as session:
        repository = IdentityRepository(session, TenantContext(organization_id=first_org))
        await repository.add_membership(
            principal_id=principal_id, organization_id=first_org, role_bindings=frozenset({"member"})
        )
        await repository.create_group(group_id, organization_id=first_org, name="Finance")

    second_context = TenantContext(organization_id=second_org)
    async with tenant_session(sessions, second_context) as session:
        repository = IdentityRepository(session, second_context)
        with pytest.raises(TenantScopeError):
            await repository.get_membership(
                principal_id=principal_id, organization_id=first_org
            )
        assert (
            await repository.get_membership(
                principal_id=principal_id, organization_id=second_org
            )
        ) is None
        with pytest.raises(TenantScopeError):
            await repository.get_group(group_id, organization_id=first_org)
        assert (await repository.get_group(group_id, organization_id=second_org)) is None
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


@pytest.mark.asyncio
async def test_membership_commands_and_scope_separation(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    async with sessions() as session, session.begin():
        identity_repository = IdentityRepository(session, context=None)
        principal = await create_user(
            identity_repository, issuer="https://idp.example.com", subject="alice-scopes"
        )

    org_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        membership = await add_org_membership(
            repository,
            principal_id=principal.id,
            organization_id=organization_id,
            role_bindings=frozenset({"owner"}),
        )
        assert (membership.principal_id, membership.organization_id) == (
            principal.id,
            organization_id,
        )

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
        assert await remove_org_membership(
            repository, principal_id=principal.id, organization_id=organization_id
        ) is True
        assert await repository.list_memberships(organization_id=organization_id) == []


@pytest.mark.asyncio
async def test_principal_can_belong_to_multiple_organizations(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    first_org, second_org = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, first_org, uuid4(), workspace_name="Sales"
    )
    await _seed_organization_and_workspace(
        sessions, second_org, uuid4(), workspace_name="Eng"
    )
    async with sessions() as session, session.begin():
        identity_repository = IdentityRepository(session, context=None)
        principal = await create_user(
            identity_repository, issuer="https://idp.example.com", subject="alice-multi-org"
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
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
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
    await _seed_principal(sessions, principal_id)

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
async def test_disabled_principal_blocked_from_new_memberships(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    async with sessions() as session, session.begin():
        identity_repository = IdentityRepository(session, context=None)
        principal = await create_user(
            identity_repository, issuer="https://idp.example.com", subject="alice-disabled"
        )

    org_context = TenantContext(organization_id=organization_id)
    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        await add_org_membership(
            repository,
            principal_id=principal.id,
            organization_id=organization_id,
        )
        await disable_principal(repository, principal.id)

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
        group_id = uuid4()
        await repository.create_group(
            group_id, organization_id=organization_id, name="Finance"
        )
        with pytest.raises(PrincipalDisabledError):
            await add_group_member(
                repository,
                group_id=group_id,
                organization_id=organization_id,
                principal_id=principal.id,
            )
        assert await remove_org_membership(
            repository, principal_id=principal.id, organization_id=organization_id
        ) is True

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


@pytest.mark.asyncio
async def test_group_member_add_is_idempotent_on_retry(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    organization_id = uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, uuid4(), workspace_name="Sales"
    )
    principal_id = uuid4()
    await _seed_principal(sessions, principal_id)
    context = TenantContext(organization_id=organization_id)
    group_id = uuid4()
    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        await create_group(repository, group_id=group_id, organization_id=organization_id, name="Finance")

    async with tenant_session(sessions, context) as session:
        repository = IdentityRepository(session, context)
        first = await add_group_member(
            repository,
            group_id=group_id,
            organization_id=organization_id,
            principal_id=principal_id,
        )
        second = await add_group_member(
            repository,
            group_id=group_id,
            organization_id=organization_id,
            principal_id=principal_id,
        )
        assert first == second
        members = await repository.list_group_members(
            group_id=group_id, organization_id=organization_id
        )
        assert members == [first]


# --------------------------------------------------------------------------- API 基础


def test_api_composition_requires_explicit_actor_dependency() -> None:
    with pytest.raises(TypeError):
        create_organizations_router()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_workspaces_router()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_memberships_router()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_api_organization_read_is_scoped_to_actor_org(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
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
            actor_dependency=lambda: _org_actor(first_org), sessions=sessions
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/organizations/{first_org}")
        assert response.status_code == 200
        assert response.json()["id"] == str(first_org)
        # 读操作防枚举：跨租户 org 与不存在的 id 都返回 404，不泄露组织是否存在
        response = await client.get(f"/organizations/{second_org}")
        assert response.status_code == 404
        response = await client.get(f"/organizations/{uuid4()}")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_membership_and_group_endpoints_enforce_actor_scope(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    organization_id, workspace_id = uuid4(), uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, workspace_id, workspace_name="Sales"
    )
    other_org = uuid4()
    await _seed_organization_and_workspace(
        sessions, other_org, uuid4(), workspace_name="Eng"
    )
    active_id, disabled_id = uuid4(), uuid4()
    await _seed_principal(sessions, active_id)
    await _seed_principal(sessions, disabled_id)
    async with sessions() as session, session.begin():
        identity_repository = IdentityRepository(session, context=None)
        await disable_principal(identity_repository, disabled_id)

    app = FastAPI()
    app.include_router(
        create_memberships_router(
            actor_dependency=lambda: _org_actor(organization_id), sessions=sessions
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/organizations/{organization_id}/members",
            json={"principal_id": str(active_id), "role_bindings": ["member"]},
        )
        assert response.status_code == 201
        assert response.json()["principal_id"] == str(active_id)
        response = await client.get(f"/organizations/{organization_id}/members")
        assert response.status_code == 200
        assert [member["principal_id"] for member in response.json()] == [str(active_id)]

        response = await client.post(
            f"/organizations/{other_org}/members",
            json={"principal_id": str(active_id), "role_bindings": ["member"]},
        )
        assert response.status_code == 403

        response = await client.post(
            f"/organizations/{organization_id}/members",
            json={"principal_id": str(disabled_id), "role_bindings": ["member"]},
        )
        assert response.status_code == 409

        response = await client.delete(
            f"/organizations/{organization_id}/members/{active_id}"
        )
        assert response.status_code == 204
        response = await client.get(f"/organizations/{organization_id}/members")
        assert response.json() == []

        response = await client.post(
            f"/organizations/{organization_id}/groups", json={"name": "Finance"}
        )
        assert response.status_code == 201
        group_id = response.json()["id"]
        assert response.json()["organization_id"] == str(organization_id)

        response = await client.post(
            f"/groups/{group_id}/members", json={"principal_id": str(active_id)}
        )
        assert response.status_code == 201
        retried = await client.post(
            f"/groups/{group_id}/members", json={"principal_id": str(active_id)}
        )
        assert retried.status_code == 201
        assert retried.json() == response.json()
        response = await client.get(f"/groups/{group_id}/members")
        assert response.status_code == 200
        assert [member["principal_id"] for member in response.json()] == [str(active_id)]

    workspace_app = FastAPI()
    workspace_app.include_router(
        create_memberships_router(
            actor_dependency=lambda: _org_actor(
                organization_id, workspace_id=workspace_id
            ),
            sessions=sessions,
        )
    )
    workspace_transport = ASGITransport(app=workspace_app)
    async with AsyncClient(
        transport=workspace_transport, base_url="http://test"
    ) as client:
        response = await client.post(
            f"/workspaces/{workspace_id}/members",
            json={"principal_id": str(active_id), "role_bindings": ["builder"]},
        )
        assert response.status_code == 201
        assert response.json()["workspace_id"] == str(workspace_id)
        response = await client.get(f"/workspaces/{workspace_id}/members")
        assert response.status_code == 200
        assert [member["principal_id"] for member in response.json()] == [str(active_id)]

    org_only_app = FastAPI()
    org_only_app.include_router(
        create_memberships_router(
            actor_dependency=lambda: _org_actor(organization_id), sessions=sessions
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=org_only_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/workspaces/{workspace_id}/members",
            json={"principal_id": str(active_id), "role_bindings": ["builder"]},
        )
        assert response.status_code == 403
        response = await client.get(f"/workspaces/{workspace_id}/members")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_workspace_creation_requires_org_only_actor(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    organization_id = uuid4()
    await _seed_organization_and_workspace(
        sessions, organization_id, uuid4(), workspace_name="Sales"
    )
    org_app = FastAPI()
    org_app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: _org_actor(organization_id), sessions=sessions
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=org_app), base_url="http://test"
    ) as client:
        response = await client.post("/workspaces", json={"name": "Eng"})
        assert response.status_code == 201
        assert response.json()["name"] == "Eng"
        assert response.json()["organization_id"] == str(organization_id)

    workspace_app = FastAPI()
    workspace_app.include_router(
        create_workspaces_router(
            actor_dependency=lambda: _org_actor(
                organization_id, workspace_id=uuid4()
            ),
            sessions=sessions,
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=workspace_app), base_url="http://test"
    ) as client:
        response = await client.post("/workspaces", json={"name": "Blocked"})
        assert response.status_code == 403
