"""S1-T4 IDOR 契约冻结（A 档）：跨租户读/写一律失败，且不泄露存在性。

设计/验收方冻结（A 档）：
- 仓库层显式 TenantContext 谓词（repositories.py `_require_organization` /
  `_require_workspace` / `_require_org_level`）是业务授权第一道门；RLS 是纵深防御
  （PERMISSIONS §5），仓库谓词即使 RLS 被剥离也必须仍然挡得住；
- API 层：读跨租户或不存在资源统一 404（同一 detail，无存在性泄露）；已知资源上的
  越权 mutation 统一 403；路径/body 自报的 org 永远不能覆盖 actor 上下文；
- list/count/cursor 分页查询不得把其他租户的行或行数泄露进任何 offset 的页；
- 本文件是已实现行为的回归守卫（green-at-RED 冻结），不是新功能测试：断言的是
  今天仓库与 API 的真实行为，任何一条变绿为红都意味着防线被拆掉。

种子（全部经 zhiwei_migrator 直插，owner 绕过 FORCE RLS）：
两个固定易猜 UUID 的组织 org_a/org_b；两个都叫 'sales' 的 workspace（ws_a/ws_b）；
两个同名 'platform' 的 group（group_a/group_b）；alice 只属于 org_a+ws_a，
bob 只属于 org_b+ws_b——重叠名称用于证明同名不跨租户碰撞。

约定：zhiwei_app 一律经 API 层或显式 tenant_session；zhiwei_migrator 只做种子与
（两段防线测试中）临时操纵 RLS catalog，且操纵必须 try/finally 精确还原。
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
from fixtures.policy_fake import FakePolicyEnforcer
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from zhiwei.api.memberships import create_memberships_router
from zhiwei.api.organizations import create_organizations_router
from zhiwei.api.workspaces import create_workspaces_router
from zhiwei.identity.domain import ActorContext
from zhiwei.identity.repositories import IdentityRepository
from zhiwei.persistence.models import Workspace as WorkspaceRow
from zhiwei.persistence.tenant import (
    TenantContext,
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

# 固定、易猜、可读的 UUID：攻击者枚举成本为零，防线只允许依赖租户边界本身。
ORG_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
WS_A = UUID("11111111-1111-4111-8111-111111111111")
WS_B = UUID("22222222-2222-4222-8222-222222222222")
GROUP_A = UUID("33333333-3333-4333-8333-333333333333")
GROUP_B = UUID("44444444-4444-4444-8444-444444444444")
ALICE = UUID("55555555-5555-4555-8555-555555555555")
BOB = UUID("66666666-6666-4666-8666-666666666666")

# 与 migrations/versions/0002_identity.py `_TENANT_RLS_POLICIES["groups"]` 逐字一致，
# 两段防线测试还原 policy 时必须重建得一模一样。
_GROUPS_RLS_EXPRESSION = (
    "organization_id = NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid "
    "AND workspace_id = NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"
)


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
    """0002-0004 迁移：从 base 重建到 head 后保持，供本文件所有用例使用。"""
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
    """identity 引擎（zhiwei_identity 角色）：organizations API 的 membership resolver
    走 SECURITY DEFINER 窄函数 zhiwei_principal_memberships，只能由该角色调用。"""
    engine = create_async_engine(IDENTITY_SQLALCHEMY_URL, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(engine.dispose())


async def _seed_two_org_world() -> None:
    """用 migrator（owner，绕过 FORCE RLS）预置两个组织、同名资源与成员关系。

    幂等（ON CONFLICT DO NOTHING）：固定 UUID 世界在会话内被多个用例共享，
    重复种子不得重复插入。membership 只给 alice→org_a/ws_a、bob→org_b/ws_b，
    这样「alice 读 org_b 行」在数据真实存在的前提下必须失败。
    """
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.executemany(
            "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1) "
            "ON CONFLICT DO NOTHING",
            [(ORG_A,), (ORG_B,)],
        )
        await connection.executemany(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) "
            "VALUES ($1, $2, 'sales', 1) ON CONFLICT DO NOTHING",
            [(WS_A, ORG_A), (WS_B, ORG_B)],
        )
        await connection.executemany(
            "INSERT INTO principals (id, kind, status, schema_version) "
            "VALUES ($1, 'user', 'active', 1) ON CONFLICT DO NOTHING",
            [(ALICE,), (BOB,)],
        )
        await connection.executemany(
            "INSERT INTO external_identities (issuer, subject, principal_id) "
            "VALUES ('https://idp.example.com', $1, $2) ON CONFLICT DO NOTHING",
            [("alice-s1t4", ALICE), ("bob-s1t4", BOB)],
        )
        await connection.executemany(
            "INSERT INTO memberships (principal_id, organization_id, role_bindings) "
            "VALUES ($1, $2, '[\"member\"]'::jsonb) ON CONFLICT DO NOTHING",
            [(ALICE, ORG_A), (BOB, ORG_B)],
        )
        await connection.executemany(
            "INSERT INTO workspace_memberships "
            "(principal_id, organization_id, workspace_id, role_bindings) "
            "VALUES ($1, $2, $3, '[\"builder\"]'::jsonb) ON CONFLICT DO NOTHING",
            [(ALICE, ORG_A, WS_A), (BOB, ORG_B, WS_B)],
        )
        await connection.executemany(
            "INSERT INTO groups (id, organization_id, workspace_id, name, schema_version) "
            "VALUES ($1, $2, $3, 'platform', 1) ON CONFLICT DO NOTHING",
            [(GROUP_A, ORG_A, WS_A), (GROUP_B, ORG_B, WS_B)],
        )
    finally:
        await connection.close()


def _alice_org_actor() -> ActorContext:
    return ActorContext(principal_id=ALICE, organization_id=ORG_A)


def _alice_workspace_actor() -> ActorContext:
    return ActorContext(principal_id=ALICE, organization_id=ORG_A, workspace_id=WS_A)


def _no_org_actor() -> ActorContext:
    return ActorContext(principal_id=ALICE)


# --------------------------------------------------------------------------- 1/2. API 单资源跨租户：读 404、写 403


@pytest.mark.asyncio
async def test_api_cross_org_organization_read_is_uniform_404(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """跨租户 org 与不存在 UUID 的 GET 语义完全一致：同状态码、同 detail，无存在性泄露。"""
    await _seed_two_org_world()
    app = FastAPI()
    app.include_router(
        create_organizations_router(
            actor_dependency=_alice_org_actor,
            sessions=sessions,
            identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 正面控制：actor 自己的 org 可读；resolver 只返回 alice 的 org_a
        response = await client.get(f"/api/v1/organizations/{ORG_A}")
        assert response.status_code == 200
        assert response.json() == {"id": str(ORG_A), "status": "active"}
        response = await client.get("/api/v1/organizations")
        assert response.status_code == 200
        assert response.json() == [{"id": str(ORG_A), "status": "active"}]

        # org_b 真实存在（有 bob/ws_b/group_b），但与不存在 UUID 一样读不到
        for target in (ORG_B, uuid4()):
            response = await client.get(f"/api/v1/organizations/{target}")
            assert response.status_code == 404
            assert response.json() == {"detail": "resource not found"}

    no_org_app = FastAPI()
    no_org_app.include_router(
        create_organizations_router(
            actor_dependency=_no_org_actor,
            sessions=sessions,
            identity_sessions=identity_sessions,
            policy_enforcer=FakePolicyEnforcer(),
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=no_org_app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/organizations/{ORG_A}")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_cross_org_workspace_write_403_and_list_404(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """org_a actor 对 org_b 路径的 workspace 写 403、读 404，且零写入。"""
    await _seed_two_org_world()
    app = FastAPI()
    app.include_router(create_workspaces_router(actor_dependency=_alice_org_actor, sessions=sessions, policy_enforcer=FakePolicyEnforcer()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/organizations/{ORG_B}/workspaces",
            json={"workspace_id": str(uuid4()), "name": "sneak"},
            headers={"Idempotency-Key": "cross-org-ws"},
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "outside tenant scope"}

        response = await client.get(f"/api/v1/organizations/{ORG_B}/workspaces")
        assert response.status_code == 404
        assert response.json() == {"detail": "resource not found"}

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        # 零写入：org_b 仍只有 ws_b 一行，idempotency 无记录
        assert await connection.fetchval(
            "SELECT count(*) FROM workspaces WHERE organization_id = $1", ORG_B
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM idempotency_records WHERE organization_id = $1", ORG_B
        ) == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_workspace_level_cross_org_groups_404_403_and_no_name_leak(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Workspace 级跨 org：读 group 列表 404、写 403；同名 'platform' 不跨租户泄露。"""
    await _seed_two_org_world()
    app = FastAPI()
    app.include_router(
        create_workspaces_router(actor_dependency=_alice_workspace_actor, sessions=sessions, policy_enforcer=FakePolicyEnforcer())
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/workspaces/{WS_B}/groups")
        assert response.status_code == 404
        response = await client.post(
            f"/api/v1/workspaces/{WS_B}/groups",
            json={"group_id": str(uuid4()), "name": "sneak"},
            headers={"Idempotency-Key": "cross-org-group"},
        )
        assert response.status_code == 403

        # 自己的 workspace：只有 ws_a 的 'platform'，org_b 的同名行不可见
        response = await client.get(f"/api/v1/workspaces/{WS_A}/groups")
        assert response.status_code == 200
        groups = response.json()
        assert [group["id"] for group in groups] == [str(GROUP_A)]
        assert [group["name"] for group in groups] == ["platform"]


# --------------------------------------------------------------------------- 4. 自报 org 不能覆盖 actor 上下文


@pytest.mark.asyncio
async def test_self_declared_org_in_body_and_path_never_overrides_actor_context(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """body 的 workspace_id 指向 org_b 既有行 + 路径 org 是 org_b：actor 是 org_a，一律 403。

    仓库层同步冻结：读与写都按显式 TenantContext 校验目标 org，路径参数只是被校验的
    输入，不是决定作用域的凭据。
    """
    await _seed_two_org_world()
    app = FastAPI()
    app.include_router(create_workspaces_router(actor_dependency=_alice_org_actor, sessions=sessions, policy_enforcer=FakePolicyEnforcer()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/organizations/{ORG_B}/workspaces",
            json={"workspace_id": str(WS_B), "name": "sneak"},
            headers={"Idempotency-Key": "self-declared-org"},
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "outside tenant scope"}

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM workspaces WHERE organization_id = $1", ORG_B
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM idempotency_records WHERE organization_id = $1", ORG_B
        ) == 0
    finally:
        await connection.close()

    org_context = TenantContext(organization_id=ORG_A)
    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        with pytest.raises(TenantScopeError):
            await repository.get_organization(ORG_B)
        with pytest.raises(TenantScopeError):
            await repository.list_workspaces(organization_id=ORG_B)
        with pytest.raises(TenantScopeError):
            await repository.create_workspace(uuid4(), organization_id=ORG_B, name="sneak")


# --------------------------------------------------------------------------- 5/6. list/count/cursor 不泄露


@pytest.mark.asyncio
async def test_org_list_count_and_cursor_never_leak_org_b_rows(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """org_a 上下文的列表、count、按 id 翻页：org_b 的 'sales' 行永远不可见。"""
    await _seed_two_org_world()
    org_context = TenantContext(organization_id=ORG_A)
    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        # 仓库列表：org_b 也有同名 'sales'，列表却只有 org_a 的行
        workspaces = await repository.list_workspaces(organization_id=ORG_A)
        assert [workspace.id for workspace in workspaces] == [WS_A]

        # count 风格原始 SQL：对 org_b 数据的计数是 0
        assert await session.scalar(
            text("SELECT count(*) FROM workspaces WHERE organization_id = :org"),
            {"org": ORG_B},
        ) == 0

        # 同名查找不碰撞：'sales' 两个 org 都有，但只命中 org_a 自己的行
        assert set(
            (await session.execute(text("SELECT id FROM workspaces WHERE name = 'sales'"))).scalars()
        ) == {WS_A}

        # 游标翻页：RLS 在 LIMIT/OFFSET 之前过滤，任意 offset 都见不到 org_b 的行
        page: list[UUID] = []
        for offset in range(4):
            page.extend(
                (
                    await session.execute(
                        text("SELECT id FROM workspaces ORDER BY id LIMIT 1 OFFSET :offset"),
                        {"offset": offset},
                    )
                ).scalars()
            )
        assert set(page) == {WS_A}

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        # 数据确实存在（migrator 全量视角）——不可见是隔离的证明，不是数据缺失
        assert await connection.fetchval("SELECT count(*) FROM workspaces") == 2
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_group_page_count_never_implies_org_b_rows(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """org_a+ws_a 上下文的分页查询：页数与行数只反映可见数据，不隐含 org_b 的行。"""
    await _seed_two_org_world()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    page_size = 1
    async with tenant_session(sessions, context) as session:
        visible_count = await session.scalar(text("SELECT count(*) FROM groups"))
        # org_b 也有同名 'platform' 行，但可见总数是 1 不是 2
        assert visible_count == 1

        ids: list[UUID] = []
        for offset in range(3):
            ids.extend(
                (
                    await session.execute(
                        text("SELECT id FROM groups ORDER BY id LIMIT :limit OFFSET :offset"),
                        {"limit": page_size, "offset": offset},
                    )
                ).scalars()
            )
        assert set(ids) == {GROUP_A}

        # org_b 的行一旦泄露，count 会变 2、页数会变 2——页数只由可见行数决定
        page_count = (visible_count + page_size - 1) // page_size
        assert page_count == 1
        assert await session.scalar(
            text("SELECT count(*) FROM groups WHERE id = :id"), {"id": GROUP_B}
        ) == 0


# --------------------------------------------------------------------------- 7. 两段防线：RLS 被剥离时仓库谓词仍然挡住


@pytest.mark.asyncio
async def test_repository_predicate_blocks_org_b_group_with_rls_stripped(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """两层独立防线：(a) RLS 层拦截裸 SQL；(b) 剥掉 RLS 后仓库谓词仍然拦截。

    (b) 用 migrator 先 DROP policy 再 DISABLE RLS（owner 权限），并以裸 count==2
    证明 RLS 层确实已不存在——这样仓库返回 None/TenantScopeError 才能归因于谓词层。
    try/finally 必须把 ENABLE + FORCE + 原样重建 policy 做完，共享库不留残迹。
    """
    await _seed_two_org_world()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)

    # (a) RLS-only：org_a+ws_a GUC 下，裸 SQL 按 org_b 的 group id 查不到行
    async with tenant_session(sessions, context) as session:
        assert await session.scalar(
            text("SELECT count(*) FROM groups WHERE id = :id"), {"id": GROUP_B}
        ) == 0
        assert await session.scalar(text("SELECT count(*) FROM groups")) == 1

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute("DROP POLICY groups_tenant_isolation ON groups")
        await admin.execute("ALTER TABLE groups DISABLE ROW LEVEL SECURITY")
    finally:
        await admin.close()

    try:
        # (b) predicate-only：先证明 RLS 层确实没了（裸 count 能看到全部 2 行）
        async with tenant_session(sessions, context) as session:
            assert await session.scalar(text("SELECT count(*) FROM groups")) == 2
            repository = IdentityRepository(session, context)
            # 目标 id 是 org_b 的 group，但参数 org/ws 与上下文一致：SQL 谓词过滤后是 None
            assert (
                await repository.get_group(GROUP_B, organization_id=ORG_A, workspace_id=WS_A)
            ) is None
            # 参数 org/ws 声明为 org_b：显式作用域校验直接拒绝
            with pytest.raises(TenantScopeError):
                await repository.get_group(GROUP_B, organization_id=ORG_B, workspace_id=WS_B)
    finally:
        restore = await asyncpg.connect(ADMIN_DSN)
        try:
            await restore.execute("ALTER TABLE groups ENABLE ROW LEVEL SECURITY")
            await restore.execute("ALTER TABLE groups FORCE ROW LEVEL SECURITY")
            await restore.execute(
                "CREATE POLICY groups_tenant_isolation ON groups "
                f"USING ({_GROUPS_RLS_EXPRESSION}) WITH CHECK ({_GROUPS_RLS_EXPRESSION})"
            )
            rows = await restore.fetch(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity, p.polname,
                       pg_get_expr(p.polqual, p.polrelid) AS using_expr,
                       pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_policy AS p ON p.polrelid = c.oid
                WHERE n.nspname = 'public' AND c.relname = 'groups'
                """
            )
            # 还原本身也要被验证：还原失败必须让测试红，而不是悄悄污染共享库
            assert len(rows) == 1
            assert rows[0]["relrowsecurity"] is True
            assert rows[0]["relforcerowsecurity"] is True
            assert rows[0]["polname"] == "groups_tenant_isolation"
            for column in ("using_expr", "check_expr"):
                assert "zhiwei.organization_id" in rows[0][column]
                assert "zhiwei.workspace_id" in rows[0][column]
        finally:
            await restore.close()

    # 还原后的功能验证：org_a+ws_a 只看到自己的 group，org_b 的行重新不可见
    async with tenant_session(sessions, context) as session:
        assert await session.scalar(text("SELECT count(*) FROM groups")) == 1
        assert await session.scalar(
            text("SELECT count(*) FROM groups WHERE id = :id"), {"id": GROUP_B}
        ) == 0


# --------------------------------------------------------------------------- 8. workspaces 表上的猜 UUID


@pytest.mark.asyncio
async def test_guessed_workspace_uuids_unreadable_names_do_not_collide(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """org_a actor 对 org_b 的 workspace 行：repository get 风格与裸 SQL 都不可读。"""
    await _seed_two_org_world()
    org_context = TenantContext(organization_id=ORG_A)
    async with tenant_session(sessions, org_context) as session:
        # session.get 的按主键查询同样受 RLS 约束，等同 repository get 语义
        assert (await session.get(WorkspaceRow, WS_B)) is None
        assert (await session.get(WorkspaceRow, WS_A)) is not None

        # 裸 SQL 猜 UUID：org_b 的 workspace 行不可见
        assert await session.scalar(
            text("SELECT count(*) FROM workspaces WHERE id = :id"), {"id": WS_B}
        ) == 0

        # 同名不碰撞：'sales' 名称查找只命中 org_a 自己的行
        rows = (await session.execute(text("SELECT id FROM workspaces WHERE name = 'sales'"))).scalars()
        assert set(rows) == {WS_A}

    workspace_context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    async with tenant_session(sessions, workspace_context) as session:
        assert await session.scalar(
            text("SELECT count(*) FROM workspaces WHERE id = :id"), {"id": WS_B}
        ) == 0


# --------------------------------------------------------------------------- 9. membership / workspace_membership IDOR


@pytest.mark.asyncio
async def test_membership_and_workspace_membership_idor_blocked(
    migrated_database: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """alice（org_a）读/删 org_b 的 membership 一律拒绝；org_a GUC 下裸计数为 0。"""
    await _seed_two_org_world()
    org_context = TenantContext(organization_id=ORG_A)
    async with tenant_session(sessions, org_context) as session:
        repository = IdentityRepository(session, org_context)
        # 正面控制：org_a 内自己的 membership 可读
        assert (
            await repository.get_membership(principal_id=ALICE, organization_id=ORG_A)
        ) is not None
        assert [
            m.principal_id for m in await repository.list_memberships(organization_id=ORG_A)
        ] == [ALICE]

        # org_b 的 membership 行（bob 的，以及 alice 名义的）一律拒绝
        with pytest.raises(TenantScopeError):
            await repository.get_membership(principal_id=BOB, organization_id=ORG_B)
        with pytest.raises(TenantScopeError):
            await repository.get_membership(principal_id=ALICE, organization_id=ORG_B)
        with pytest.raises(TenantScopeError):
            await repository.list_memberships(organization_id=ORG_B)
        with pytest.raises(TenantScopeError):
            await repository.remove_membership(principal_id=BOB, organization_id=ORG_B)

        # org_a GUC 下裸计数：org_b 的 membership 与 ws_b 的 workspace_membership 都是 0
        assert await session.scalar(
            text("SELECT count(*) FROM memberships WHERE organization_id = :org"),
            {"org": ORG_B},
        ) == 0
        assert await session.scalar(
            text("SELECT count(*) FROM workspace_memberships WHERE workspace_id = :ws"),
            {"ws": WS_B},
        ) == 0

    workspace_context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    async with tenant_session(sessions, workspace_context) as session:
        repository = IdentityRepository(session, workspace_context)
        with pytest.raises(TenantScopeError):
            await repository.get_workspace_membership(principal_id=ALICE, workspace_id=WS_B)
        with pytest.raises(TenantScopeError):
            await repository.list_workspace_memberships(workspace_id=WS_B)

    # API 层：读 404、删 403；org_a 自己的成员列表正常
    app = FastAPI()
    app.include_router(create_memberships_router(actor_dependency=_alice_org_actor, sessions=sessions, policy_enforcer=FakePolicyEnforcer()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/organizations/{ORG_A}/members")
        assert response.status_code == 200
        assert [member["principal_id"] for member in response.json()] == [str(ALICE)]
        response = await client.get(f"/api/v1/organizations/{ORG_B}/members")
        assert response.status_code == 404
        response = await client.delete(
            f"/api/v1/organizations/{ORG_B}/members/{BOB}",
            headers={"Idempotency-Key": "cross-org-remove"},
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "outside tenant scope"}

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        # bob 的 org_b membership 原样保留：403 不是「静默删除」的伪装
        assert await connection.fetchval(
            "SELECT count(*) FROM memberships WHERE principal_id = $1 AND organization_id = $2",
            BOB,
            ORG_B,
        ) == 1
    finally:
        await connection.close()
