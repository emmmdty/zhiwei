"""S10 冻结契约：Agent Studio draft 编辑 API（A 档，S10-T2/T3）。

draft revision 的 ETag/CAS 乐观并发、无旁路生命周期写、validate 只读校验、
发布必须经 S9 release commands——语义在本文件冻结；GREEN 阶段不得修改本文件。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.policy_fake import FakePolicyEnforcer
from zhiwei.api.agents import create_agents_router
from zhiwei.api.releases import create_releases_router
from zhiwei.identity.domain import ActorContext, ActorRoleBinding
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_URL = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)

pytestmark = pytest.mark.asyncio

CLEAN_GRAPH = {
    "tasks": [
        {
            "task_id": "t1",
            "task_type": "Retrieve",
            "required_capability": "knowledge.retrieve@1",
            "budget": {"max_model_calls": 2},
        }
    ],
    "edges": [],
}
BAD_GRAPH = {
    "tasks": [
        {
            "task_id": "t1",
            "task_type": "Retrieve",
            "required_capability": "repo.destroy@9",
        }
    ],
    "edges": [],
}


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    config.attributes["database_url"] = ADMIN_DSN
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], TenantContext]]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
    assert context.organization_id is not None and context.workspace_id is not None
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        await repository.create_workspace(context.workspace_id, name="studio-api")
    try:
        yield sessions, context
    finally:
        await engine.dispose()


def _actor(context: TenantContext, *roles: str) -> ActorContext:
    assert context.organization_id is not None and context.workspace_id is not None
    return ActorContext(
        principal_id=uuid4(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        role_bindings=tuple(
            ActorRoleBinding(
                name=role,
                scope="workspace",
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
            )
            for role in roles
        ),
    )


def _app(
    sessions: async_sessionmaker[AsyncSession],
    actor: ActorContext,
    *,
    with_releases: bool = False,
) -> FastAPI:
    app = FastAPI()
    enforcer = FakePolicyEnforcer(allow=True)
    app.include_router(
        create_agents_router(
            actor_dependency=lambda: actor,
            sessions=sessions,
            policy_enforcer=enforcer,
        )
    )
    if with_releases:
        app.include_router(
            create_releases_router(
                actor_dependency=lambda: actor,
                sessions=sessions,
                policy_enforcer=enforcer,
            )
        )
    return app


async def _create_agent(
    client: AsyncClient, *, capabilities: list[str] | None = None
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/agents",
        json={
            "name": "studio-agent",
            "description": "S10 studio contract",
            "capabilities": capabilities or ["knowledge.retrieve@1"],
        },
    )
    assert response.status_code == 201, response.text
    return cast_dict(response.json())


def cast_dict(body: object) -> dict[str, object]:
    assert isinstance(body, dict)
    return body


class TestDraftRevisionCCAS:
    async def test_create_returns_etag_and_revision(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        async with AsyncClient(
            transport=ASGITransport(app=_app(sessions, _actor(context, "agent_builder"))),
            base_url="http://test",
        ) as client:
            body = await _create_agent(client)
            assert "revision" in body
            list_response = await client.get(f"/api/v1/agents/{body['agent_id']}")
            assert list_response.status_code == 200
            assert list_response.headers.get("etag"), "GET 必须携带 ETag"

    async def test_put_with_stale_if_match_conflicts(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        async with AsyncClient(
            transport=ASGITransport(app=_app(sessions, _actor(context, "agent_builder"))),
            base_url="http://test",
        ) as client:
            body = await _create_agent(client)
            agent_id = body["agent_id"]
            first = await client.get(f"/api/v1/agents/{agent_id}")
            etag = first.headers["etag"]
            ok = await client.put(
                f"/api/v1/agents/{agent_id}",
                json={"description": "rev2"},
                headers={"If-Match": etag},
            )
            assert ok.status_code == 200, ok.text
            # 持旧 ETag 再写 → 412 机器可读冲突（不是静默覆盖）。
            stale = await client.put(
                f"/api/v1/agents/{agent_id}",
                json={"description": "rev3"},
                headers={"If-Match": etag},
            )
            assert stale.status_code == 412
            detail = cast_dict(stale.json())
            assert detail.get("reason") == "revision_conflict"

    async def test_put_without_if_match_refused(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        async with AsyncClient(
            transport=ASGITransport(app=_app(sessions, _actor(context, "agent_builder"))),
            base_url="http://test",
        ) as client:
            body = await _create_agent(client)
            missing = await client.put(
                f"/api/v1/agents/{body['agent_id']}", json={"description": "x"}
            )
            assert missing.status_code == 428
            assert cast_dict(missing.json()).get("reason") == "if_match_required"


class TestNoLifecycleBypass:
    async def test_status_patch_route_absent(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        async with AsyncClient(
            transport=ASGITransport(app=_app(sessions, _actor(context, "agent_builder"))),
            base_url="http://test",
        ) as client:
            body = await _create_agent(client)
            direct = await client.patch(
                f"/api/v1/agents/{body['agent_id']}/status", json={"status": "published"}
            )
            # 无旁路生命周期写入口：路由必须不存在（405/404 均视为不存在该写路径）。
            assert direct.status_code in {404, 405}


class TestValidationEndpoint:
    async def test_validate_reports_machine_issues(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        async with AsyncClient(
            transport=ASGITransport(app=_app(sessions, _actor(context, "agent_builder"))),
            base_url="http://test",
        ) as client:
            body = await _create_agent(client)
            bad = await client.post(
                f"/api/v1/agents/{body['agent_id']}/validate", json=BAD_GRAPH
            )
            assert bad.status_code == 200
            issues = cast_dict(bad.json())["issues"]
            assert isinstance(issues, list) and issues
            codes = {cast_dict(i)["code"] for i in issues}
            assert "unknown_capability" in codes
            good = await client.post(
                f"/api/v1/agents/{body['agent_id']}/validate", json=CLEAN_GRAPH
            )
            assert cast_dict(good.json())["issues"] == []


class TestPublishGoesThroughReleaseCommands:
    async def test_release_creation_via_agents_router(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        app = _app(sessions, _actor(context, "agent_builder"), with_releases=True)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = await _create_agent(client)
            created = await client.post(
                f"/api/v1/agents/{body['agent_id']}/releases",
                json={
                    "pack_digest": "sha256:" + "a" * 64,
                    "model_digest": "sha256:" + "b" * 64,
                    "knowledge_digest": "sha256:" + "c" * 64,
                    "memory_digest": "sha256:" + "d" * 64,
                    "capability_digest": "sha256:" + "e" * 64,
                    "policy_digest": "sha256:" + "f" * 64,
                    "eval_digests": ["sha256:" + "1" * 64],
                    "approver": "alice",
                    "rollout": {"default_version": 1, "cohorts": []},
                    "rollback": {"in_flight": "complete"},
                },
            )
            assert created.status_code == 201, created.text
            release = cast_dict(created.json())
            # 列表形状遵循既有 S9 锁定契约（test_releases_api.py：裸数组，不信封）。
            listing = await client.get("/api/v1/releases")
            releases = listing.json()
            assert isinstance(releases, list)
            release_ids = {UUID(str(cast_dict(r)["release_id"])) for r in releases}
            assert UUID(str(release["release_id"])) in release_ids
