"""S9-T4 集成：release 生命周期 SoD、canary 路由与 rollback（真实 PostgreSQL）。

- 全生命周期 draft→…→retired 逐级推进；角色分离由域层 SoD 拒绝错误角色，
  状态不被污染；retired 终态不可复活；
- 错误角色的 advance 经 API 层拒绝（policy fake 放行 → 域层拒绝）并写 failed
  审计——refusal 落账是 API 纵切的职责，服务层只负责拒绝；
- canary：user cohort > workspace cohort > default pin；security suspend 不受
  pin 保护，一律拒绝路由；无 default 且无 cohort 命中 fail closed；
- rollback 只改 default pin（对新 Run 生效），cohort pin 不重写，在途 Run 只
  声明 complete/terminate disposition，不由域层执行。
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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.fixtures.policy_fake import FakePolicyEnforcer
from zhiwei.agents.release import (
    ReleaseManifest,
    ReleaseNotFound,
    ReleaseService,
    ReleaseState,
    ReleaseTransitionDenied,
)
from zhiwei.agents.rollout import (
    Cohort,
    RollbackNotApplicable,
    RollbackPolicy,
    RolloutNotConfigured,
    RolloutPolicy,
)
from zhiwei.api.releases import create_releases_router
from zhiwei.identity.domain import ActorContext, ActorRoleBinding
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

LIFECYCLE_STEPS = (
    ("sandbox", "builder"),
    ("evaluated", "builder"),
    ("review", "reviewer"),
    ("staged", "approver"),
    ("published", "release_manager"),
    ("deprecated", "release_manager"),
    ("retired", "release_manager"),
)

DatabaseFixture = tuple[AsyncEngine, async_sessionmaker[AsyncSession], TenantContext]


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1))
    config.attributes["database_url"] = ADMIN_DSN
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[DatabaseFixture]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="S9-T4-release")
    try:
        yield engine, sessions, context
    finally:
        await engine.dispose()


async def _seed_agent(sessions: async_sessionmaker[AsyncSession], context: TenantContext) -> UUID:
    """release 的 agent_id 复合外键引用 agent_definitions，需要先落定义行。"""
    agent_id = uuid4()
    async with tenant_session(sessions, context) as session:
        await session.execute(
            text(
                "INSERT INTO agent_definitions"
                " (id, organization_id, workspace_id, name, schema_version)"
                " VALUES (:id, :org, :ws, 'release-under-test', 1)"
            ),
            {"id": agent_id, "org": context.organization_id, "ws": context.workspace_id},
        )
    return agent_id


def _manifest(agent_id: UUID, **overrides: object) -> ReleaseManifest:
    fields: dict[str, object] = {
        "agent_id": agent_id,
        "agent_version": 5,
        "pack_digest": "sha256:" + "a" * 64,
        "model_digest": "sha256:" + "b" * 64,
        "knowledge_digest": "sha256:" + "c" * 64,
        "memory_digest": "sha256:" + "d" * 64,
        "capability_digest": "sha256:" + "e" * 64,
        "policy_digest": "sha256:" + "f" * 64,
        "eval_digests": ("sha256:" + "1" * 64,),
        "approver": "alice",
        "rollout": RolloutPolicy(default_version=5, cohorts=()),
        "rollback": RollbackPolicy(in_flight="complete"),
    }
    fields.update(overrides)
    return ReleaseManifest(**fields)  # type: ignore[arg-type]


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


class TestLifecycleRoleSeparation:
    @pytest.mark.asyncio
    async def test_full_lifecycle_advances_only_through_role_gated_steps(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(_manifest(agent_id))
            release_id = created.release_id
            assert created.state is ReleaseState.DRAFT
            assert created.manifest.content_digest == _manifest(agent_id).content_digest

        for target, role in LIFECYCLE_STEPS:
            async with tenant_session(sessions, context) as session:
                record = await ReleaseService(session, context).advance(
                    release_id, target=ReleaseState(target), role=role
                )
                assert record.state is ReleaseState(target)

        # retired 是终态：任何角色都不能复活已退役版本
        with pytest.raises(ReleaseTransitionDenied):
            async with tenant_session(sessions, context) as session:
                await ReleaseService(session, context).advance(
                    release_id, target=ReleaseState.PUBLISHED, role="release_manager"
                )

    @pytest.mark.asyncio
    async def test_wrong_role_refusal_leaves_state_untouched(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(_manifest(agent_id))
        async with tenant_session(sessions, context) as session:
            service = ReleaseService(session, context)
            await service.advance(created.release_id, target=ReleaseState.SANDBOX, role="builder")
            await service.advance(created.release_id, target=ReleaseState.EVALUATED, role="builder")
        with pytest.raises(ReleaseTransitionDenied):
            async with tenant_session(sessions, context) as session:
                await ReleaseService(session, context).advance(
                    created.release_id, target=ReleaseState.REVIEW, role="builder"
                )
        async with tenant_session(sessions, context) as session:
            record = await ReleaseService(session, context).get(created.release_id)
        assert record.state is ReleaseState.EVALUATED

    @pytest.mark.asyncio
    async def test_unknown_role_and_missing_release_refuse(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(_manifest(agent_id))
        with pytest.raises(ReleaseTransitionDenied):
            async with tenant_session(sessions, context) as session:
                await ReleaseService(session, context).advance(
                    created.release_id, target=ReleaseState.SANDBOX, role="intern"
                )
        with pytest.raises(ReleaseNotFound):
            async with tenant_session(sessions, context) as session:
                await ReleaseService(session, context).get(uuid4())

    @pytest.mark.asyncio
    async def test_listing_is_scoped_to_the_explicit_tenant(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        other = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        assert other.workspace_id is not None
        async with tenant_session(sessions, other) as session:
            await TenantRepository(session, other).create_organization(
                other.organization_id, status="active"
            )
            await TenantRepository(session, other).create_workspace(
                other.workspace_id, name="S9-T4-other"
            )
        async with tenant_session(sessions, context) as session:
            await ReleaseService(session, context).create_draft(_manifest(agent_id))
        async with tenant_session(sessions, context) as session:
            mine = await ReleaseService(session, context).list()
        async with tenant_session(sessions, other) as session:
            theirs = await ReleaseService(session, other).list()
        assert len(mine) == 1
        assert theirs == []


class TestRoleRefusalAuditTrail:
    @pytest.mark.asyncio
    async def test_wrong_role_advance_is_recorded_as_failed_audit(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(_manifest(agent_id))
            service = ReleaseService(session, context)
            # builder 先把 release 推进到 evaluated：之后的 evaluated→review 是
            # reviewer 职责，builder 的 advance 必须被 SoD 拒绝并记录 failed 审计
            await service.advance(created.release_id, target=ReleaseState.SANDBOX, role="builder")
            await service.advance(created.release_id, target=ReleaseState.EVALUATED, role="builder")
        # agent_builder 不能复核：evaluated→review 的 SoD 拒绝（policy fake 放行后由域层拒绝）
        builder = _actor(context, "agent_builder")
        app = FastAPI()
        app.include_router(
            create_releases_router(
                actor_dependency=lambda: builder,
                sessions=sessions,
                policy_enforcer=FakePolicyEnforcer(allow=True),
            )
        )
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


class TestCanaryRouting:
    @pytest.mark.asyncio
    async def test_user_cohort_wins_then_workspace_then_default(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        pinned_workspace, pinned_user = uuid4(), uuid4()
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(
                _manifest(
                    agent_id,
                    rollout=RolloutPolicy(
                        default_version=1,
                        cohorts=(
                            Cohort(kind="workspace", selector_id=pinned_workspace, version=2),
                            Cohort(kind="user", selector_id=pinned_user, version=3),
                        ),
                    ),
                )
            )
        async with tenant_session(sessions, context) as session:
            service = ReleaseService(session, context)
            assert (
                await service.route(created.release_id, workspace_id=uuid4(), user_id=pinned_user)
                == 3
            )
            assert (
                await service.route(
                    created.release_id, workspace_id=pinned_workspace, user_id=uuid4()
                )
                == 2
            )
            assert (
                await service.route(created.release_id, workspace_id=uuid4(), user_id=uuid4()) == 1
            )

    @pytest.mark.asyncio
    async def test_security_suspend_blocks_routing_despite_pin(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(_manifest(agent_id))
        with pytest.raises(RolloutNotConfigured):
            async with tenant_session(sessions, context) as session:
                await ReleaseService(session, context).route(
                    created.release_id,
                    workspace_id=context.workspace_id or uuid4(),
                    user_id=uuid4(),
                    suspended=True,
                )

    @pytest.mark.asyncio
    async def test_unconfigured_rollout_refuses_routing(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(
                _manifest(agent_id, rollout=RolloutPolicy(default_version=None, cohorts=()))
            )
        with pytest.raises(RolloutNotConfigured):
            async with tenant_session(sessions, context) as session:
                await ReleaseService(session, context).route(
                    created.release_id, workspace_id=uuid4(), user_id=uuid4()
                )


class TestRollbackNewRunsOnly:
    @pytest.mark.asyncio
    async def test_rollback_rewrites_default_pin_and_declares_disposition(
        self, database: DatabaseFixture
    ) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        canary_workspace = uuid4()
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(
                _manifest(
                    agent_id,
                    rollout=RolloutPolicy(
                        default_version=5,
                        cohorts=(Cohort(kind="workspace", selector_id=canary_workspace, version=5),),
                    ),
                )
            )
        in_flight = (uuid4(), uuid4())
        async with tenant_session(sessions, context) as session:
            outcome = await ReleaseService(session, context).rollback(
                created.release_id, to_version=4, in_flight_run_ids=in_flight
            )
        assert outcome.applies_to == "new_runs_only"
        assert outcome.executed is False
        assert outcome.in_flight_disposition == "complete"
        assert outcome.in_flight_run_ids == in_flight
        assert outcome.policy.default_version == 4
        # cohort pin 属 canary 计划：rollback 不重写
        assert outcome.policy.cohorts == (
            Cohort(kind="workspace", selector_id=canary_workspace, version=5),
        )
        async with tenant_session(sessions, context) as session:
            record = await ReleaseService(session, context).get(created.release_id)
        assert record.rollout.default_version == 4
        assert record.state is ReleaseState.DRAFT

    @pytest.mark.asyncio
    async def test_rollback_to_same_version_refuses(self, database: DatabaseFixture) -> None:
        _, sessions, context = database
        agent_id = await _seed_agent(sessions, context)
        async with tenant_session(sessions, context) as session:
            created = await ReleaseService(session, context).create_draft(_manifest(agent_id))
        with pytest.raises(RollbackNotApplicable):
            async with tenant_session(sessions, context) as session:
                await ReleaseService(session, context).rollback(created.release_id, to_version=5)
