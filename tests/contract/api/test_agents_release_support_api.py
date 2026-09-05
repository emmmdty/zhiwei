"""S10-T3 契约：Studio 发布流支撑面（release-readiness / 版本 diff / manifest 读）。

事实源：specs/s10 §3（发布按钮调用正式 commands + 版本 diff 展示）、plan Task 3；
S9 生命周期命令本身已冻结于 test_releases_api.py / test_agents_studio_frozen.py，
本文件只覆盖 T3 新增的支撑读面：

- GET /api/v1/agents/{id}/release-readiness → {ready, missing:[{kind, detail}]}：
  可诚实计算的检查（eval_seal）必须真算（eval_runs.run_id → runs.agent_version_id
  → agent_versions 链路），查不到数据=missing，绝不假设满足；无法从 agent 记录
  泛化计算的检查（connection / capability_publish）以 kind=unknown 如实呈报原因
  （fail closed：unknown 不计入 ready）。
- GET /api/v1/agents/{id}/diff?from_revision=&to_revision= → {fields:[...]}：
  revision 解析到 agent_versions 行（优先，不可变既定参照）或 current draft
  revision，解析失败一律 404（fail closed）；字段级 diff 只来自真实存储
  （release manifest digest 集 / content_digest / schema_version）。
- GET /api/v1/releases/{id}/manifest → manifest 全字段 verbatim（digest 不截断、
  approver 原样）——Studio 不可变 manifest 展示的数据面。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.policy_fake import FakePolicyEnforcer
from zhiwei.agents.release import ReleaseManifest, ReleaseService
from zhiwei.agents.rollout import RollbackPolicy, RolloutPolicy
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

# 本文件的契约面止于 0016（Studio draft 列）：fixture 只保证迁移到 0016，不追
# head——并发 Task 的迁移可能正处于 WIP 状态（0016 之后的头是不可控输入），且
# DB 已在 0016 之后时绝不 downgrade（对共享测试库非破坏）。链内序号同前缀单调，
# 字典序比较在本迁移链上等价于拓扑序。
_BASELINE_REVISION = "0016_studio_agents"


async def _current_revision() -> str | None:
    try:
        conn = await asyncpg.connect(ADMIN_DSN)
    except asyncpg.UndefinedTableError:
        return None
    try:
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    except asyncpg.UndefinedTableError:
        return None
    finally:
        await conn.close()


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    current = asyncio.run(_current_revision())
    if current != _BASELINE_REVISION and not (
        current is not None and current > _BASELINE_REVISION
    ):
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option(
            "sqlalchemy.url", ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
        )
        config.attributes["database_url"] = ADMIN_DSN
        command.upgrade(config, _BASELINE_REVISION)
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
        await repository.create_workspace(context.workspace_id, name="studio-release-api")
    try:
        yield sessions, context
    finally:
        await engine.dispose()


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


def _app(
    sessions: async_sessionmaker[AsyncSession],
    actor: ActorContext,
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
    app.include_router(
        create_releases_router(
            actor_dependency=lambda: actor,
            sessions=sessions,
            policy_enforcer=enforcer,
        )
    )
    return app


@asynccontextmanager
async def _client_for(
    sessions: async_sessionmaker[AsyncSession], actor: ActorContext
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=_app(sessions, actor)), base_url="http://test"
    ) as client:
        yield client


async def _seed_agent(
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
    *,
    capabilities: list[str] | None = None,
) -> object:
    """经 API 建 draft（既有 T2 面），返回 agent_id 字符串。"""
    actor = _actor(context, "agent_builder")
    async with AsyncClient(
        transport=ASGITransport(app=_app(sessions, actor)), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "release-support-agent",
                "capabilities": capabilities or ["knowledge.retrieve@1"],
            },
        )
        assert created.status_code == 201, created.text
        return created.json()["agent_id"]


async def _seed_agent_version(
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
    agent_id: object,
    *,
    version: int,
    content_digest: str,
) -> None:
    async with tenant_session(sessions, context) as session:
        await session.execute(
            text(
                "INSERT INTO agent_versions"
                " (id, organization_id, workspace_id, agent_definition_id, version,"
                "  content_digest, schema_version)"
                " VALUES (:id, :org, :ws, :agent_id, :version, :digest, 1)"
            ),
            {
                "id": uuid4(),
                "org": context.organization_id,
                "ws": context.workspace_id,
                "agent_id": agent_id,
                "version": version,
                "digest": content_digest,
            },
        )


def _manifest(agent_id: object, agent_version: int, *, pack: str, knowledge: str) -> ReleaseManifest:
    return ReleaseManifest(
        agent_id=agent_id,  # type: ignore[arg-type]
        agent_version=agent_version,
        pack_digest=pack,
        model_digest="sha256:" + "b" * 64,
        knowledge_digest=knowledge,
        memory_digest="sha256:" + "d" * 64,
        capability_digest="sha256:" + "e" * 64,
        policy_digest="sha256:" + "f" * 64,
        eval_digests=("sha256:" + "1" * 64,),
        approver="alice",
        rollout=RolloutPolicy(default_version=1, cohorts=()),
        rollback=RollbackPolicy(in_flight="complete"),
    )


class TestReleaseReadiness:
    async def test_requires_organization_context(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, _context = stack
        async with _client_for(sessions, _actor(None)) as client:
            response = await client.get(
                f"/api/v1/agents/{uuid4()}/release-readiness"
            )
        assert response.status_code == 403, response.text

    async def test_unknown_agent_is_404(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        async with _client_for(sessions, _actor(context, "agent_builder")) as client:
            response = await client.get(f"/api/v1/agents/{uuid4()}/release-readiness")
        assert response.status_code == 404, response.text

    async def test_missing_eval_seal_and_unknown_checks_reported_honestly(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        agent_id = await _seed_agent(sessions, context)
        async with _client_for(sessions, _actor(context, "agent_builder")) as client:
            response = await client.get(f"/api/v1/agents/{agent_id}/release-readiness")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ready"] is False
        kinds = [item["kind"] for item in body["missing"]]
        # 可计算的检查如实报告缺失；不可泛化计算的检查以 unknown 呈报而非伪装
        assert "eval_seal" in kinds
        assert kinds.count("unknown") >= 1
        for item in body["missing"]:
            assert item["detail"], "每个检查必须携带解释（缺失原因或不可计算原因）"

    async def test_sealed_eval_run_bound_to_agent_clears_eval_seal(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        agent_id = await _seed_agent(sessions, context)
        version_id, run_id, eval_run_id = uuid4(), uuid4(), uuid4()
        dataset_version_id, suite_version_id = uuid4(), uuid4()
        async with tenant_session(sessions, context) as session:
            await session.execute(
                text(
                    "INSERT INTO agent_versions"
                    " (id, organization_id, workspace_id, agent_definition_id, version,"
                    "  content_digest, schema_version)"
                    " VALUES (:id, :org, :ws, :agent_id, 1, :digest, 1)"
                ),
                {
                    "id": version_id,
                    "org": context.organization_id,
                    "ws": context.workspace_id,
                    "agent_id": agent_id,
                    "digest": "sha256:" + "a" * 64,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO runs"
                    " (id, organization_id, workspace_id, agent_version_id, status,"
                    "  schema_version)"
                    " VALUES (:id, :org, :ws, :version_id, 'completed', 1)"
                ),
                {
                    "id": run_id,
                    "org": context.organization_id,
                    "ws": context.workspace_id,
                    "version_id": version_id,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO dataset_versions"
                    " (id, organization_id, workspace_id, dataset_id, version,"
                    "  content_digest, status, schema_version)"
                    " VALUES (:id, :org, :ws, :dataset_id, 1, :digest, 'active', 1)"
                ),
                {
                    "id": dataset_version_id,
                    "org": context.organization_id,
                    "ws": context.workspace_id,
                    "dataset_id": uuid4(),
                    "digest": "sha256:" + "c" * 64,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO eval_suite_versions"
                    " (id, organization_id, workspace_id, suite_id, version,"
                    "  content_digest, status, schema_version)"
                    " VALUES (:id, :org, :ws, :suite_id, 1, :digest, 'active', 1)"
                ),
                {
                    "id": suite_version_id,
                    "org": context.organization_id,
                    "ws": context.workspace_id,
                    "suite_id": uuid4(),
                    "digest": "sha256:" + "e" * 64,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO eval_runs"
                    " (id, organization_id, workspace_id, run_id, dataset_version_id,"
                    "  eval_suite_version_id, mode, status, code_digest, config_digest,"
                    "  schema_digest, schema_version, sealed_at)"
                    " VALUES (:id, :org, :ws, :run_id, :dataset_version_id,"
                    "  :suite_version_id, 'offline', 'complete', :code, :config,"
                    "  :schema_digest, 1, now())"
                ),
                {
                    "id": eval_run_id,
                    "org": context.organization_id,
                    "ws": context.workspace_id,
                    "run_id": run_id,
                    "dataset_version_id": dataset_version_id,
                    "suite_version_id": suite_version_id,
                    "code": "sha256:" + "1" * 64,
                    "config": "sha256:" + "2" * 64,
                    "schema_digest": "sha256:" + "3" * 64,
                },
            )
        async with _client_for(sessions, _actor(context, "agent_builder")) as client:
            response = await client.get(f"/api/v1/agents/{agent_id}/release-readiness")
        assert response.status_code == 200, response.text
        kinds = [item["kind"] for item in response.json()["missing"]]
        assert "eval_seal" not in kinds


class TestAgentRevisionDiff:
    async def test_requires_both_revisions(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        agent_id = await _seed_agent(sessions, context)
        async with _client_for(sessions, _actor(context, "agent_builder")) as client:
            missing_param = await client.get(f"/api/v1/agents/{agent_id}/diff")
            inverted = await client.get(
                f"/api/v1/agents/{agent_id}/diff?from_revision=2&to_revision=1"
            )
        assert missing_param.status_code == 422, missing_param.text
        assert inverted.status_code == 422, inverted.text

    async def test_unknown_agent_or_revision_is_404(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        agent_id = await _seed_agent(sessions, context)
        async with _client_for(sessions, _actor(context, "agent_builder")) as client:
            unknown_agent = await client.get(
                f"/api/v1/agents/{uuid4()}/diff?from_revision=1&to_revision=2"
            )
            unknown_revision = await client.get(
                f"/api/v1/agents/{agent_id}/diff?from_revision=1&to_revision=9"
            )
        assert unknown_agent.status_code == 404, unknown_agent.text
        assert unknown_revision.status_code == 404, unknown_revision.text

    async def test_manifest_changes_diffed_by_kind(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        agent_id = await _seed_agent(sessions, context)
        await _seed_agent_version(
            sessions, context, agent_id, version=1, content_digest="sha256:" + "7" * 64
        )
        await _seed_agent_version(
            sessions, context, agent_id, version=2, content_digest="sha256:" + "8" * 64
        )
        async with tenant_session(sessions, context) as session:
            service = ReleaseService(session, context)
            await service.create_draft(
                _manifest(
                    agent_id, 1, pack="sha256:" + "a" * 64, knowledge="sha256:" + "c" * 64
                )
            )
            await service.create_draft(
                _manifest(
                    agent_id, 2, pack="sha256:" + "9" * 64, knowledge="sha256:" + "c" * 64
                )
            )
        async with _client_for(sessions, _actor(context, "agent_builder")) as client:
            response = await client.get(
                f"/api/v1/agents/{agent_id}/diff?from_revision=1&to_revision=2"
            )
        assert response.status_code == 200, response.text
        fields = response.json()["fields"]
        by_field = {item["field"]: item for item in fields}
        # 依赖 digest 变化 → kind=dependency（pack 变了、knowledge 没变）
        assert by_field["pack_digest"]["kind"] == "dependency"
        assert by_field["pack_digest"]["from"] == "sha256:" + "a" * 64
        assert by_field["pack_digest"]["to"] == "sha256:" + "9" * 64
        assert "knowledge_digest" not in by_field
        assert "capability_digest" not in by_field
        assert "policy_digest" not in by_field
        # 两个版本的聚合 content_digest 不同 → kind=other
        assert by_field["content_digest"]["kind"] == "other"
        # schema_version 相同 → 不出现 schema 字段
        assert "schema_version" not in by_field


class TestReleaseManifestEndpoint:
    async def test_manifest_fields_verbatim(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        agent_id = await _seed_agent(sessions, context)
        async with tenant_session(sessions, context) as session:
            record = await ReleaseService(session, context).create_draft(
                _manifest(
                    agent_id, 1, pack="sha256:" + "a" * 64, knowledge="sha256:" + "c" * 64
                )
            )
            release_id = record.release_id
        async with _client_for(sessions, _actor(context, "agent_builder")) as client:
            response = await client.get(f"/api/v1/releases/{release_id}/manifest")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pack_digest"] == "sha256:" + "a" * 64
        assert body["knowledge_digest"] == "sha256:" + "c" * 64
        assert body["model_digest"] == "sha256:" + "b" * 64
        assert body["memory_digest"] == "sha256:" + "d" * 64
        assert body["capability_digest"] == "sha256:" + "e" * 64
        assert body["policy_digest"] == "sha256:" + "f" * 64
        assert body["eval_digests"] == ["sha256:" + "1" * 64]
        assert body["approver"] == "alice"
        assert body["manifest_digest"] == record.manifest.content_digest
        assert body["rollout"] == {"default_version": 1, "cohorts": []}
        assert body["rollback"] == {"in_flight": "complete"}

    async def test_unknown_release_is_404(
        self, stack: tuple[async_sessionmaker[AsyncSession], TenantContext]
    ) -> None:
        sessions, context = stack
        async with _client_for(sessions, _actor(context, "agent_builder")) as client:
            response = await client.get(f"/api/v1/releases/{uuid4()}/manifest")
        assert response.status_code == 404, response.text
