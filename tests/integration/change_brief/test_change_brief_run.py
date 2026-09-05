"""S10-T6 integration：ChangeBrief 生产路径全链路。

事实源：specs/s10-studio-third-app.md §4/§7、plan Task 6。

执行走真实生产栈（与 ask-v1 integration 同构）：load pack bundle（conformance clean）
→ RunCommandService（Run 行 + outbox 命令，同事务）→ OutboxDispatcher → Temporal dev
server → AgentRunWorkflow → RuntimeActivities（PG canonical events）。handler 注册表
由 pack runtime（solution-packs/change-brief/runtime/）经公共 TaskHandlerRegistry 机制
构造——没有 ChangeBrief 专属的 Core handler/DB 列/API route。

六个冻结 fixture 全部经 Trigger → Run → TaskGraph（Retrieve/Analyze/Verify/Synthesize/
EmitArtifact/Finish）→ Evidence/brief artifact 路径执行；brief 内容按 fixture expected
断言；unknown-symbol 场景必须产出诚实 unknowns，绝不编造快照之外的符号。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from zhiwei.evals.change_brief_suites import CHANGE_BRIEF_V1, resolve_change_brief_suite
from zhiwei.evals.executors.change_brief import (
    ChangeBriefPackExecutor,
    build_change_brief_environment,
    build_change_brief_registry,
)

from zhiwei.agents.pack_files import load_pack_dir, validate_pack_bundle
from zhiwei.evals.domain import RegisteredUnit
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.registry import TaskHandlerRegistryError

REPO_ROOT = Path(__file__).resolve().parents[3]

ADMIN_DSN = "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
APP_DSN = "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

BRIEF_REQUIRED_KEYS = {
    "affected_symbols",
    "affected_dependencies",
    "affected_tests",
    "related_prs",
    "related_issues",
    "related_checks",
    "risks",
    "unknowns",
    "code_refs",
    "github_refs",
}


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    # CURRENT HEAD 跟随并发任务（0016 可能已落库）：显式 upgrade head，不钉 revision。
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN)
    config.attributes["database_url"] = ADMIN_DSN
    command.upgrade(config, "head")
    yield


@pytest.fixture
def context() -> TenantContext:
    return TenantContext(organization_id=uuid4(), workspace_id=uuid4())


@pytest_asyncio.fixture
async def executor(
    context: TenantContext,
) -> AsyncIterator[ChangeBriefPackExecutor]:
    from zhiwei.persistence.database import create_database_engine, create_session_factory
    from zhiwei.persistence.repositories import TenantRepository

    assert context.workspace_id is not None
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    # 生产命令路径要求 tenant（org/workspace 行）真实存在——与 CLI suite flow 同款准备
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        await repository.create_workspace(context.workspace_id, name=CHANGE_BRIEF_V1)
    environment = await build_change_brief_environment(
        sessions=sessions, context=context, suite=resolve_change_brief_suite(CHANGE_BRIEF_V1)
    )
    yield ChangeBriefPackExecutor(environment, resolve_change_brief_suite(CHANGE_BRIEF_V1))
    await environment.aclose()
    await engine.dispose()


class TestChangeBriefProductionPath:
    @pytest.mark.asyncio
    async def test_all_six_fixtures_through_production_path(
        self, executor: ChangeBriefPackExecutor
    ) -> None:
        suite = resolve_change_brief_suite(CHANGE_BRIEF_V1)
        for unit in suite.registered_units:
            outcome = await executor.execute(unit)
            assert outcome.status.value == "completed", (
                unit.unit_id,
                outcome.result.get("failures") or outcome.result.get("error"),
            )
            assert outcome.result["verdict"] == "pass", (unit.unit_id, outcome.result)

    @pytest.mark.asyncio
    async def test_brief_artifact_lands_in_pg_canonical(
        self, executor: ChangeBriefPackExecutor, context: TenantContext
    ) -> None:
        """Evidence/brief artifact 真相在 PG：事件可独立重放出 canonical brief。"""
        from zhiwei.persistence.runtime_events import RuntimeEventStore

        unit = RegisteredUnit(sample_id="github-commit", unit_id="github-commit")
        outcome = await executor.execute(unit)
        assert outcome.status.value == "completed", outcome.result

        sessions = executor.sessions
        run_id = UUID(str(outcome.result["run_id"]))
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(run_id)
        assert state.status == "completed"
        # pack task_graph 的全部任务都必须完成（事件序列里可复核）
        assert len(state.tasks) == 6
        assert all(task.status == "completed" for task in state.tasks.values())

        brief = state.canonical["brief"]
        assert set(brief) == BRIEF_REQUIRED_KEYS
        assert state.canonical["verification_result"]["verification_ok"] is True
        assert state.canonical["artifact_kind"] == "verified-brief"
        assert state.canonical["artifact_id"].startswith("artifact:")
        # CodeRef/GitHubRef 证据词汇（schemas/verified-brief.yaml）
        for ref in brief["code_refs"]:
            assert {"file_path", "line_start", "line_end", "code_digest"} <= set(ref)
        for ref in brief["github_refs"]:
            assert "repository" in ref

    @pytest.mark.asyncio
    async def test_unknown_symbol_unit_reports_honest_unknowns(
        self, executor: ChangeBriefPackExecutor
    ) -> None:
        unit = RegisteredUnit(sample_id="unknown-symbol", unit_id="unknown-symbol")
        outcome = await executor.execute(unit)
        assert outcome.status.value == "completed", outcome.result
        brief = outcome.result["brief"]
        names = {symbol["name"] for symbol in brief["affected_symbols"]}
        assert names == {"FreshnessWindow"}
        # 快照之外的符号绝不编造进影响面；unknowns 必须指名它
        assert "compute_age" not in names
        assert any("compute_age" in u for u in brief["unknowns"])

    @pytest.mark.asyncio
    async def test_no_impact_unit_does_not_silently_answer(
        self, executor: ChangeBriefPackExecutor
    ) -> None:
        unit = RegisteredUnit(sample_id="no-impact", unit_id="no-impact")
        outcome = await executor.execute(unit)
        assert outcome.status.value == "completed", outcome.result
        brief = outcome.result["brief"]
        assert brief["affected_symbols"] == []
        assert brief["affected_tests"] == []
        assert brief["unknowns"], "无影响面必须披露 unknowns，不允许静默空答"


class TestGenericityFailClosed:
    def test_pack_bundle_conformance_clean(self) -> None:
        suite = resolve_change_brief_suite(CHANGE_BRIEF_V1)
        bundle = load_pack_dir(suite.pack_dir)
        assert validate_pack_bundle(bundle, suite.pack_dir) == ()

    def test_registry_raises_on_unknown_primitive(self) -> None:
        """通用性负例：pack 之外的 primitive 无 handler，注册表必须 fail closed。"""
        suite = resolve_change_brief_suite(CHANGE_BRIEF_V1)
        registry = build_change_brief_registry(suite)
        with pytest.raises(TaskHandlerRegistryError):
            registry.validate_completeness({"Teleport"})
