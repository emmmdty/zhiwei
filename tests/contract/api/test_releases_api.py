"""S9-T4 API 契约：release/claim 路由的认证门、SoD 拒绝与机器可读拒绝面。

- 无组织上下文的 actor 一律 403（fail closed，不编造租户作用域）；
- 角色拒绝路径：policy fake 放行后由域层 SoD 拒绝 → 409 + failed 审计；
- claim 升级拒绝返回结构化 detail：{"reason": ...} 机器可读，release checker
  与前端按 reason 分支而不是解析消息文本。
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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from zhiwei.agents.release import ReleaseManifest, ReleaseService
from zhiwei.agents.rollout import RollbackPolicy, RolloutPolicy
from zhiwei.api.claims import create_claims_router
from zhiwei.api.releases import create_releases_router

from tests.fixtures.policy_fake import FakePolicyEnforcer
from zhiwei.identity.domain import ActorContext, ActorRoleBinding
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import AuditEvent
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

DatabaseFixture = tuple[
    async_sessionmaker[AsyncSession],
    TenantContext,
    PosixObjectStore,
]


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
async def stack(tmp_path: Path) -> AsyncIterator[DatabaseFixture]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="releases-api")
    try:
        yield sessions, context, PosixObjectStore(tmp_path / "objects")
    finally:
        await engine.dispose()


async def _seed_agent(sessions: async_sessionmaker[AsyncSession], context: TenantContext) -> None:
    async with tenant_session(sessions, context) as session:
        await session.execute(
            text(
                "INSERT INTO agent_definitions"
                " (id, organization_id, workspace_id, name, schema_version)"
                " VALUES (:id, :org, :ws, 'api-contract-agent', 1)"
            ),
            {"id": uuid4(), "org": context.organization_id, "ws": context.workspace_id},
        )


def _manifest(agent_id: UUID) -> ReleaseManifest:
    return ReleaseManifest(
        agent_id=agent_id,
        agent_version=1,
        pack_digest="sha256:" + "a" * 64,
        model_digest="sha256:" + "b" * 64,
        knowledge_digest="sha256:" + "c" * 64,
        memory_digest="sha256:" + "d" * 64,
        capability_digest="sha256:" + "e" * 64,
        policy_digest="sha256:" + "f" * 64,
        eval_digests=("sha256:" + "1" * 64,),
        approver="alice",
        rollout=RolloutPolicy(default_version=1, cohorts=()),
        rollback=RollbackPolicy(in_flight="complete"),
    )


def _actor(context: TenantContext | None, *roles: str) -> ActorContext:
    if context is None:
        return ActorContext(principal_id=uuid4())
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


def _releases_app(sessions: async_sessionmaker[AsyncSession], actor: ActorContext) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_releases_router(
            actor_dependency=lambda: actor,
            sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(allow=True),
        )
    )
    return app


def _claims_app(
    sessions: async_sessionmaker[AsyncSession],
    actor: ActorContext,
    store: PosixObjectStore,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_claims_router(
            actor_dependency=lambda: actor,
            sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(allow=True),
            object_store=store,
        )
    )
    return app


class TestAuthRequired:
    async def test_actor_without_organization_context_is_403(self, stack: DatabaseFixture) -> None:
        sessions, _context, store = stack
        scope = {
            "mode": "offline",
            "model": "reference-fixture",
            "version": "1",
            "date": "2026-09-05",
            "corpus": "factqa-v1",
            "environment": "offline-fixture",
        }
        app = _releases_app(sessions, _actor(None))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/v1/releases")
            assert listed.status_code == 403, listed.text
        claims_app = _claims_app(sessions, _actor(None), store)
        claims_transport = ASGITransport(app=claims_app)
        async with AsyncClient(transport=claims_transport, base_url="http://test") as client:
            registered = await client.post(
                "/api/v1/claims",
                json={
                    "claim_id": "factqa-v1.accuracy",
                    "statement": "accuracy {{accuracy}}",
                    "scope": scope,
                },
            )
            assert registered.status_code == 403, registered.text


class TestReleaseEndpoints:
    async def test_create_and_read_release_via_api(self, stack: DatabaseFixture) -> None:
        sessions, context, _store = stack
        await _seed_agent(sessions, context)
        agent_id = uuid4()
        async with tenant_session(sessions, context) as session:
            await session.execute(
                text(
                    "INSERT INTO agent_definitions"
                    " (id, organization_id, workspace_id, name, schema_version)"
                    " VALUES (:id, :org, :ws, 'api-release-agent', 1)"
                ),
                {"id": agent_id, "org": context.organization_id, "ws": context.workspace_id},
            )
        actor = _actor(context, "agent_builder")
        app = _releases_app(sessions, actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/releases",
                json={
                    "agent_id": str(agent_id),
                    "agent_version": 1,
                    "pack_digest": "sha256:" + "a" * 64,
                    "model_digest": "sha256:" + "b" * 64,
                    "knowledge_digest": "sha256:" + "c" * 64,
                    "memory_digest": "sha256:" + "d" * 64,
                    "capability_digest": "sha256:" + "e" * 64,
                    "policy_digest": "sha256:" + "f" * 64,
                    "eval_digests": ["sha256:" + "1" * 64],
                    "rollout": {"default_version": 1, "cohorts": []},
                    "rollback": {"in_flight": "complete"},
                },
            )
            assert created.status_code == 201, created.text
            release_id = created.json()["release_id"]
            assert created.json()["state"] == "draft"

            listed = await client.get("/api/v1/releases")
            assert listed.status_code == 200
            assert any(item["release_id"] == release_id for item in listed.json())

            detail = await client.get(f"/api/v1/releases/{release_id}")
            assert detail.status_code == 200
            assert detail.json()["state"] == "draft"

    async def test_advance_role_refusal_is_machine_readable_and_audited(
        self, stack: DatabaseFixture
    ) -> None:
        sessions, context, _store = stack
        agent_id = uuid4()
        async with tenant_session(sessions, context) as session:
            await session.execute(
                text(
                    "INSERT INTO agent_definitions"
                    " (id, organization_id, workspace_id, name, schema_version)"
                    " VALUES (:id, :org, :ws, 'api-sod-agent', 1)"
                ),
                {"id": agent_id, "org": context.organization_id, "ws": context.workspace_id},
            )
            created = await ReleaseService(session, context).create_draft(_manifest(agent_id))
        builder = _actor(context, "agent_builder")
        app = _releases_app(sessions, builder)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/releases/{created.release_id}/advance",
                json={"target_state": "review"},
            )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["reason"] == "release_transition_denied"
        async with tenant_session(sessions, context) as session:
            audits = (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.action == "agent.release.advance")
                )
            ).all()
        assert audits
        assert audits[-1].result == "failed"

    async def test_unknown_release_advance_is_404(self, stack: DatabaseFixture) -> None:
        sessions, context, _store = stack
        actor = _actor(context, "agent_builder")
        app = _releases_app(sessions, actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/releases/{uuid4()}/advance",
                json={"target_state": "sandbox"},
            )
        assert response.status_code == 404, response.text


class TestClaimEndpoints:
    async def test_register_and_upgrade_refusal_shape(self, stack: DatabaseFixture) -> None:
        sessions, context, store = stack
        actor = _actor(context, "agent_builder")
        app = _claims_app(sessions, actor, store)
        transport = ASGITransport(app=app)
        scope = {
            "mode": "offline",
            "model": "reference-fixture",
            "version": "1",
            "date": "2026-09-05",
            "corpus": "factqa-v1",
            "environment": "offline-fixture",
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            registered = await client.post(
                "/api/v1/claims",
                json={
                    "claim_id": "factqa-v1.accuracy",
                    "statement": "FactQA accuracy {{accuracy}}",
                    "scope": scope,
                },
            )
            assert registered.status_code == 201, registered.text
            assert registered.json()["status"] == "planned"

            refused = await client.post(
                "/api/v1/claims/factqa-v1.accuracy/upgrade",
                json={"target": "offline_verified", "eval_run_id": None},
            )
            assert refused.status_code == 409, refused.text
            detail = refused.json()["detail"]
            assert detail["reason"] == "claim_upgrade_denied"
            assert "claim_id" in detail

            listed = await client.get("/api/v1/claims")
            assert listed.status_code == 200
            assert [item["claim_id"] for item in listed.json()] == ["factqa-v1.accuracy"]
