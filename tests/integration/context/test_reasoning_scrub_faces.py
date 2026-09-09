"""F-R2-06/T-P3.7：reasoning 销毁的持久化面覆盖（S3 登记债务范围）。

四持久化面中的两处补设防 + 测试钉住：
- Temporal 面：execute_task 的 activity 返回值（进 Temporal history）在出口
  scrub——PG canonical 行已被 UoW 覆盖（test_hidden_reasoning_persistence.py），
  但 history 是独立的持久化面；
- Redis/sink 面：outbox dispatcher 在 sink.publish 前对 payload scrub——增量
  通道当前只发元数据，但 sink 契约层必须自带销毁，防未来 sink 实现携带正文。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.outbox import MemoryOutboxSink, OutboxDelivery
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.base import TaskOutput
from zhiwei.runtime.outbox_handlers import OutboxSignalHandler
from zhiwei.workers.outbox_dispatcher import OutboxDispatcher, OutboxDispatcherConfig
from zhiwei.workflows.activities.base import ExecuteTaskInput
from zhiwei.workflows.activities.runtime import RuntimeActivities

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
ADMIN_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

REASONING_BODY = "REASONING-BODY-leak-check-faces-9001"


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def database() -> AsyncIterator[
    tuple[async_sessionmaker[AsyncSession], TenantContext]
]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="P3-scrub-faces")
    try:
        yield sessions, context
    finally:
        await engine.dispose()


class _ReasoningHandler:
    def validate_input(self, task_input: object) -> None:
        return None

    def execute(self, task_input: object) -> TaskOutput:
        return TaskOutput(
            output_values={
                "reasoning": REASONING_BODY,
                "answer": "safe-answer",
            }
        )

    def validate_output(self, output: TaskOutput) -> None:
        return None


class _StubRegistry:
    def get(self, task_type: str, version: int = 1) -> _ReasoningHandler:
        return _ReasoningHandler()


class _StubRepository:
    def __init__(self) -> None:
        self.delivered: list[OutboxDelivery] = []

    async def mark_delivered(self, message: OutboxDelivery) -> None:
        self.delivered.append(message)

    async def mark_failed(self, message: OutboxDelivery, error: str) -> None:
        raise AssertionError(f"unexpected dispatch failure: {error}")


class _NoopSender:
    async def start_workflow(self, **kwargs: Any) -> None:
        return None

    async def signal_workflow(self, **kwargs: Any) -> None:
        return None


def _message(payload: dict[str, Any]) -> OutboxDelivery:
    return OutboxDelivery(
        id=uuid4(),
        organization_id=uuid4(),
        workspace_id=uuid4(),
        topic="canonical.event.committed",
        event_key="task_completed",
        payload=payload,
        status="pending",
        attempts=0,
        available_at=datetime.now(tz=UTC),
        claimed_by="test",
        claim_token=uuid4(),
        lease_expires_at=datetime.now(tz=UTC),
    )


def _assert_scrubbed(value: Any, face: str) -> None:
    assert isinstance(value, dict) and str(
        value.get("opaque_ref", "")
    ).startswith("opaque:"), f"reasoning body must not reach the {face}: {value!r}"


@pytest.mark.asyncio
class TestTemporalFaceScrub:
    async def test_execute_task_output_is_scrubbed_before_history(
        self, database
    ) -> None:
        sessions, context = database
        run_id = uuid4()
        async with tenant_session(sessions, context) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO runs
                        (id, organization_id, workspace_id, status, schema_version)
                    VALUES (:id, :organization_id, :workspace_id, 'running', 1)
                    """
                ),
                {
                    "id": run_id,
                    "organization_id": context.organization_id,
                    "workspace_id": context.workspace_id,
                },
            )
        activities = RuntimeActivities(sessions, _StubRegistry())  # type: ignore[arg-type]
        result = await activities.execute_task(
            ExecuteTaskInput(
                run_id=str(run_id),
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                task_id="task-1",
                task_type="Plan",
                handler_version=1,
                attempt_id=str(uuid4()),
                attempt_no=1,
                input_values={},
            )
        )
        assert result.status == "completed"
        _assert_scrubbed(result.output_values["reasoning"], "Temporal history")
        assert result.output_values["answer"] == "safe-answer"


class TestSinkFaceScrub:
    def test_dispatcher_scrubs_payload_before_sink(self) -> None:
        sink = MemoryOutboxSink()
        repository = _StubRepository()
        dispatcher = OutboxDispatcher(
            repository,  # type: ignore[arg-type]
            OutboxSignalHandler(sender=_NoopSender()),  # type: ignore[arg-type]
            OutboxDispatcherConfig(),
            event_sink=sink,
        )
        message = _message({"reasoning": REASONING_BODY, "answer": "safe"})

        asyncio.run(dispatcher._dispatch_event(message))
        published = sink.deliveries[0].payload
        _assert_scrubbed(published["reasoning"], "stream sink")
        assert published["answer"] == "safe"
