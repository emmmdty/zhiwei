"""S9-T7 补口 API 契约：eval run 路由的认证门、前端字段契约与机器可读拒绝面。

- 无组织上下文的 actor 一律 403（fail closed，不编造租户作用域）；policy deny
  同为 403（读/写路径 PEP 前置）；
- 列表/详情字段名与 apps/web/src/features/evals 的 TS 接口逐字段对齐
  （EvalRunListItem / EvalRunDetail），outcome 只回 metadata（status +
  result_digest），result 正文永不进响应；
- 业务拒绝返回结构化 detail：{"reason", "message"} 机器可读——未完备不可 seal
  （eval_seal_refused）、未 sealed 不可出报告（not_sealed）；
- seal 请求体与 EvalFoundationService.seal 签名逐参数对齐（migration_revision +
  test_report 缺一不可），不弱化服务契约。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.policy_fake import FakePolicyEnforcer
from zhiwei.api.evals import create_evals_router
from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.runs import CreatedEvalRun, CreateEvalRunCommand, EvalFoundationService
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

# 当前单一 alembic head；seal 密封件引用的迁移基线（同 evals 集成测试口径）
MIGRATION_REVISION = "0015_release_claims"

pytestmark = pytest.mark.asyncio

UNITS = (
    RegisteredUnit(sample_id="s-1", unit_id="u-1"),
    RegisteredUnit(sample_id="s-2", unit_id="u-1"),
)

DatabaseFixture = tuple[async_sessionmaker[AsyncSession], TenantContext, PosixObjectStore]


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
        await repository.create_workspace(workspace_id, name="evals-api")
    try:
        yield sessions, context, PosixObjectStore(tmp_path / "objects")
    finally:
        await engine.dispose()


async def _seed_eval_run(
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
    store: PosixObjectStore,
    *,
    outcomes: int = 0,
    pause: bool = False,
) -> CreatedEvalRun:
    """经真实 EvalFoundationService 播种：outcomes 个 COMPLETED 终态（结果互异），
    其余单位保持 registered；pause=True 时落为 partial。
    """
    async with tenant_session(sessions, context) as session:
        service = EvalFoundationService(session, context, store)
        created = await service.create(
            CreateEvalRunCommand(
                mode=EvalMode.OFFLINE,
                registered_units=UNITS,
                dataset_payload={"samples": [unit.sample_id for unit in UNITS]},
                code_digest=digest_bytes(b"code"),
                config_digest=digest_bytes(b"config"),
                schema_digest=digest_bytes(b"schema"),
            )
        )
        for index in range(outcomes):
            await service.record_outcome(
                created.eval_run_id,
                SampleOutcome(
                    unit=UNITS[index],
                    status=SampleStatus.COMPLETED,
                    result={"passed": True, "answer": "42", "index": index},
                ),
            )
        if pause:
            await service.pause(created.eval_run_id)
    return created


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


def _evals_app(
    sessions: async_sessionmaker[AsyncSession],
    actor: ActorContext,
    store: PosixObjectStore,
    *,
    allow: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_evals_router(
            actor_dependency=lambda: actor,
            sessions=sessions,
            policy_enforcer=FakePolicyEnforcer(allow=allow),
            object_store=store,
        )
    )
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


REPORT_SCOPE_PARAMS = {
    "model": "internal-llm",
    "version": "agent-2026.09",
    "date": "2026-09-05",
    "corpus": "internal-120",
    "environment": "offline",
}


class TestAuthRequired:
    async def test_actor_without_organization_context_is_403(
        self, stack: DatabaseFixture
    ) -> None:
        sessions, _context, store = stack
        app = _evals_app(sessions, _actor(None), store)
        async with _client(app) as client:
            listed = await client.get("/api/v1/evals")
            assert listed.status_code == 403, listed.text
            sealed = await client.post(
                f"/api/v1/evals/{uuid4()}/seal",
                json={"migration_revision": MIGRATION_REVISION, "test_report": {}},
            )
            assert sealed.status_code == 403, sealed.text

    async def test_policy_deny_is_403(self, stack: DatabaseFixture) -> None:
        sessions, context, store = stack
        app = _evals_app(sessions, _actor(context, "agent_builder"), store, allow=False)
        async with _client(app) as client:
            denied = await client.get("/api/v1/evals")
            assert denied.status_code == 403, denied.text
            assert denied.json()["detail"] == "policy denied"


class TestEvalRunEndpoints:
    async def test_list_and_detail_match_frontend_field_contract(
        self, stack: DatabaseFixture
    ) -> None:
        sessions, context, store = stack
        created = await _seed_eval_run(sessions, context, store, outcomes=1)
        app = _evals_app(sessions, _actor(context, "agent_builder"), store)
        async with _client(app) as client:
            listed = await client.get("/api/v1/evals")
            assert listed.status_code == 200, listed.text
            items = listed.json()
            assert len(items) == 1
            item = items[0]
            # 前端 EvalRunListItem 逐字段（apps/web/src/features/evals/EvalRunsView.tsx）
            assert item["eval_run_id"] == str(created.eval_run_id)
            assert item["run_id"] == str(created.run_id)
            assert item["mode"] == "offline"
            assert item["status"] == "running"
            assert item["sealed_at"] is None
            assert item["registered_units"] == 2
            # 后端补充的治理元数据（S9 冻结引用 + 时间戳）
            assert item["terminal_units"] == 1
            assert item["campaign_id"] is None
            assert item["prereg_manifest_id"] is None
            assert item["model_manifest_id"] is None
            assert item["source_manifest_id"] is None
            assert item["attempt_manifest_id"] is None
            assert item["created_at"]

            detail_response = await client.get(f"/api/v1/evals/{created.eval_run_id}")
            assert detail_response.status_code == 200, detail_response.text
            detail = detail_response.json()
            # 前端 EvalRunDetail 逐字段（EvalRunDetailView.tsx）
            assert detail["eval_run_id"] == str(created.eval_run_id)
            assert detail["run_id"] == str(created.run_id)
            assert detail["mode"] == "offline"
            assert detail["status"] == "running"
            assert detail["sealed_at"] is None
            assert detail["registered_units"] == [
                {"sample_id": "s-1", "unit_id": "u-1"},
                {"sample_id": "s-2", "unit_id": "u-1"},
            ]
            # outcome 只回 metadata：status + result_digest，result 正文缺席
            assert len(detail["outcomes"]) == 1
            outcome = detail["outcomes"][0]
            assert outcome["unit"] == {"sample_id": "s-1", "unit_id": "u-1"}
            assert outcome["status"] == "completed"
            assert outcome["result_digest"].startswith("sha256:")
            assert "result" not in outcome
            # 完整状态分解（含 0 计数的封闭状态集）
            assert detail["status_breakdown"] == {
                "completed": 1,
                "failed": 0,
                "refused": 0,
                "error": 0,
                "registered": 1,
                "running": 0,
            }
            # 详情不猜报告：scope 标签只能由调用方显式声明（经 /report 现取）
            assert detail["report"] is None

    async def test_seal_non_terminal_run_is_409_machine_readable_and_audited(
        self, stack: DatabaseFixture
    ) -> None:
        sessions, context, store = stack
        created = await _seed_eval_run(sessions, context, store, outcomes=1, pause=True)
        app = _evals_app(sessions, _actor(context, "workspace_admin"), store)
        async with _client(app) as client:
            refused = await client.post(
                f"/api/v1/evals/{created.eval_run_id}/seal",
                json={"migration_revision": MIGRATION_REVISION, "test_report": {}},
            )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["reason"] == "eval_seal_refused"
        assert "message" in detail
        async with tenant_session(sessions, context) as session:
            audits = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "eval.run.seal",
                        AuditEvent.resource_id == created.eval_run_id,
                    )
                )
            ).all()
        assert audits
        assert audits[-1].result == "failed"

    async def test_resume_and_seal_lifecycle_returns_detail(
        self, stack: DatabaseFixture
    ) -> None:
        sessions, context, store = stack
        created = await _seed_eval_run(sessions, context, store, outcomes=2, pause=True)
        app = _evals_app(sessions, _actor(context, "workspace_admin"), store)
        async with _client(app) as client:
            resumed = await client.post(f"/api/v1/evals/{created.eval_run_id}/resume")
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["status"] == "running"

            sealed = await client.post(
                f"/api/v1/evals/{created.eval_run_id}/seal",
                json={"migration_revision": MIGRATION_REVISION, "test_report": {"status": "passed"}},
            )
            assert sealed.status_code == 200, sealed.text
            detail = sealed.json()
            assert detail["status"] == "sealed"
            assert detail["sealed_at"] is not None
            assert detail["registered_units"] == [
                {"sample_id": "s-1", "unit_id": "u-1"},
                {"sample_id": "s-2", "unit_id": "u-1"},
            ]
            assert detail["status_breakdown"]["completed"] == 2

        async with tenant_session(sessions, context) as session:
            audits = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.resource_id == created.eval_run_id,
                        AuditEvent.action.in_(["eval.run.resume", "eval.run.seal"]),
                    )
                )
            ).all()
        assert {audit.action: audit.result for audit in audits} == {
            "eval.run.resume": "allowed",
            "eval.run.seal": "allowed",
        }

    async def test_seal_requires_service_inputs(self, stack: DatabaseFixture) -> None:
        sessions, context, store = stack
        created = await _seed_eval_run(sessions, context, store, outcomes=2)
        app = _evals_app(sessions, _actor(context, "workspace_admin"), store)
        async with _client(app) as client:
            missing = await client.post(f"/api/v1/evals/{created.eval_run_id}/seal", json={})
            assert missing.status_code == 422, missing.text

    async def test_report_on_unsealed_run_is_409_not_sealed(
        self, stack: DatabaseFixture
    ) -> None:
        sessions, context, store = stack
        created = await _seed_eval_run(sessions, context, store, outcomes=2)
        app = _evals_app(sessions, _actor(context, "agent_builder"), store)
        async with _client(app) as client:
            refused = await client.get(
                f"/api/v1/evals/{created.eval_run_id}/report", params=REPORT_SCOPE_PARAMS
            )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["reason"] == "not_sealed"
        assert "message" in detail

    async def test_report_on_sealed_run_builds_payload_from_seal(
        self, stack: DatabaseFixture
    ) -> None:
        sessions, context, store = stack
        created = await _seed_eval_run(sessions, context, store, outcomes=2)
        async with tenant_session(sessions, context) as session:
            sealed = await EvalFoundationService(session, context, store).seal(
                created.eval_run_id,
                migration_revision=MIGRATION_REVISION,
                test_report={"status": "passed", "scope": "evals-api"},
            )
        app = _evals_app(sessions, _actor(context, "agent_builder"), store)
        async with _client(app) as client:
            report_response = await client.get(
                f"/api/v1/evals/{created.eval_run_id}/report", params=REPORT_SCOPE_PARAMS
            )
        assert report_response.status_code == 200, report_response.text
        report = report_response.json()
        # 前端 ReportArtifact 逐字段（EvalRunDetailView.tsx / e2e EvalReport）
        assert report["schema_id"] == "eval.report"
        assert report["schema_version"] == 1
        assert report["scope"] == {
            "mode": "offline",
            "model": "internal-llm",
            "version": "agent-2026.09",
            "date": "2026-09-05",
            "corpus": "internal-120",
            "environment": "offline",
        }
        assert report["generated_from"]["eval_run_id"] == str(created.eval_run_id)
        assert report["generated_from"]["seal_digest"] == sealed.seal_digest
        denominator = report["quality"][0]["denominator"]
        assert denominator == {
            "n_total": 2,
            "n_completed": 2,
            "n_failed": 0,
            "n_refused": 0,
            "n_error": 0,
        }

    async def test_unknown_eval_run_is_404(self, stack: DatabaseFixture) -> None:
        sessions, context, store = stack
        app = _evals_app(sessions, _actor(context, "agent_builder"), store)
        async with _client(app) as client:
            missing_id = uuid4()
            detail = await client.get(f"/api/v1/evals/{missing_id}")
            assert detail.status_code == 404, detail.text
            resumed = await client.post(f"/api/v1/evals/{missing_id}/resume")
            assert resumed.status_code == 404, resumed.text
            report = await client.get(
                f"/api/v1/evals/{missing_id}/report", params=REPORT_SCOPE_PARAMS
            )
            assert report.status_code == 404, report.text
