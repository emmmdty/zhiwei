"""S6 integration: Ask 生产路径 —— Ask task → Runtime → Evidence/Case 落账。

事实源：specs/s6-evidence-ask.md §4（Ask contract 与 §4.1 Case lifecycle）。

执行走真实生产栈：RunCommandService（Run 行 + outbox 命令）→ OutboxDispatcher →
Temporal dev server → AgentRunWorkflow → RuntimeActivities（PG canonical events）。
Case 侧经生产命令层（create/attach/transition）创建 Case 并断言生命周期事件序列。

已知实现缺口（见交付报告，测试按 spec 契约编写）：
- Case 尚无 PG 持久化（InMemory 仓储），生命周期事件经命令层产出、由调用方落账。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from zhiwei.cases.commands import (
    attach_answer,
    attach_evidence_bundle,
    create_case,
    transition_case,
)
from zhiwei.cases.domain import CaseStatus
from zhiwei.cases.repositories import InMemoryCaseRepository
from zhiwei.evals.ask_contracts import (
    ASK_V1_UNITS,
)
from zhiwei.evals.executors.ask import AskRuntimeExecutor, build_ask_environment
from zhiwei.persistence.tenant import TenantContext, tenant_session

REPO_ROOT = Path(__file__).resolve().parents[3]

ADMIN_DSN = "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
APP_DSN = "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN)
    config.attributes["database_url"] = ADMIN_DSN
    command.upgrade(config, "head")
    yield


@pytest.fixture
def context() -> TenantContext:
    return TenantContext(organization_id=uuid4(), workspace_id=uuid4())


@pytest_asyncio.fixture
async def ask_environment(
    context: TenantContext,
) -> AsyncIterator[AskRuntimeExecutor]:
    from zhiwei.persistence.database import create_database_engine, create_session_factory
    from zhiwei.persistence.repositories import TenantRepository

    assert context.workspace_id is not None
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    # 生产命令路径要求 tenant（org/workspace 行）真实存在——与 CLI suite flow 同款准备
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        await repository.create_workspace(context.workspace_id, name="ask-v1")
    environment = await build_ask_environment(sessions=sessions, context=context)
    executor = AskRuntimeExecutor(environment)
    yield executor
    await environment.aclose()
    await engine.dispose()


class TestAskRuntimeIntegration:
    @pytest.mark.asyncio
    async def test_ask_scenario_reaches_terminal_in_pg(
        self, ask_environment: AskRuntimeExecutor, context: TenantContext
    ) -> None:
        unit = ASK_V1_UNITS[0]
        outcome = await ask_environment.execute(unit)
        assert outcome.status.value == "completed", outcome.result
        assert outcome.result["invariant"] == "cross_source_findings_present"

        # Run 终态真相在 PG：canonical events 可独立重放
        from zhiwei.persistence.runtime_events import RuntimeEventStore

        sessions = ask_environment.sessions
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            run_id = UUID(str(outcome.result["run_id"]))
            state = await store.reduce_state(run_id)
        assert state.status == "completed"

    @pytest.mark.asyncio
    async def test_all_six_ask_units_pass_production_path(
        self, ask_environment: AskRuntimeExecutor
    ) -> None:
        outcomes = [await ask_environment.execute(unit) for unit in ASK_V1_UNITS]
        failed = [
            (o.unit.unit_id, o.result) for o in outcomes if o.status.value != "completed"
        ]
        assert not failed, failed


class TestCaseFromAnswer:
    """用户把 Answer/selected Evidence 创建 Case 的路径（spec §4）。"""

    @pytest.mark.asyncio
    async def test_answer_creates_case_with_lifecycle_events(self) -> None:
        repo = InMemoryCaseRepository()
        answer_id = uuid4()
        bundle_id = uuid4()

        created = await create_case(
            repo,
            organization_id=uuid4(),
            workspace_id=uuid4(),
            title="武松战功数分歧排查",
            created_by=uuid4(),
        )
        case_id = UUID(created.case["id"])
        assert created.case["status"] == CaseStatus.CREATED
        assert created.events[0]["event_type"] == "case.created"

        await attach_answer(repo, case_id=case_id, answer_id=answer_id)
        await attach_evidence_bundle(repo, case_id=case_id, evidence_bundle_id=bundle_id)

        active = await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.ACTIVE
        )
        triaged = await transition_case(
            repo, case_id=case_id, target_status=CaseStatus.TRIAGED
        )

        events = [*created.events, *active.events, *triaged.events]
        assert [e["event_type"] for e in events] == [
            "case.created",
            "case.status_changed",
            "case.status_changed",
        ]
        assert events[1]["to_status"] == CaseStatus.ACTIVE
        assert events[2]["to_status"] == CaseStatus.TRIAGED
        # 不复制 transcript：Case 只持有 id 引用
        assert created.case["answer_ids"] == []
        stored = await repo.get_case(case_id)
        assert stored is not None
        assert stored.answer_ids == (answer_id,)
        assert stored.evidence_bundle_ids == (bundle_id,)

    @pytest.mark.asyncio
    async def test_created_cannot_jump_to_triaged(self) -> None:
        """spec §4.1 状态机：created → active → triaged，不允许静默跳变。"""
        from zhiwei.cases.commands import InvalidTransitionError

        repo = InMemoryCaseRepository()
        created = await create_case(
            repo,
            organization_id=uuid4(),
            workspace_id=uuid4(),
            title="T",
            created_by=uuid4(),
        )
        case_id = UUID(created.case["id"])
        with pytest.raises(InvalidTransitionError):
            await transition_case(repo, case_id=case_id, target_status=CaseStatus.TRIAGED)
