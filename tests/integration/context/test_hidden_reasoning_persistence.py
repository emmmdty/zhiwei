"""S3 §5 集成：canonical event 落库路径的 reasoning 正文销毁。

冻结契约（tests/security/model_egress/test_hidden_reasoning.py）只覆盖内存投影/
编译路径；本文件钉住 PG 侧：canonical event 行与 projection cache 是持久化单元，
含 hidden_reasoning 正文的 payload 在 append_event 入口被销毁后才参与 digest、
落库与投影——行内只有 opaque ref，链验证不受影响。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.context.opaque import OPAQUE_REF_PREFIX, opaque_reasoning_ref
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.events import (
    EventCommand,
    build_event_digest,
    event_data_from_row,
    verify_event_chain,
)
from zhiwei.persistence.models import CanonicalEvent, CanonicalProjection
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.persistence.unit_of_work import CanonicalUnitOfWork

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
ADMIN_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

BODY = "HIDDEN-REASONING-PG-leak-check-c47d"


class ContextCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any]


def _registry() -> Any:
    from zhiwei.contracts.envelope import SchemaRegistry

    registry = SchemaRegistry()
    registry.register("context.created", 1, ContextCreatedPayload)
    return registry


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def database() -> AsyncIterator[
    tuple[async_sessionmaker[AsyncSession], TenantContext, UUID]
]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id, run_id = uuid4(), uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="S3-opaque")
        await session.execute(
            text(
                """
                INSERT INTO runs
                    (id, organization_id, workspace_id, status, schema_version)
                VALUES (:id, :organization_id, :workspace_id, 'running', 1)
                """
            ),
            {"id": run_id, "organization_id": organization_id, "workspace_id": workspace_id},
        )
    try:
        yield sessions, context, run_id
    finally:
        await engine.dispose()


def _command(run_id: UUID) -> EventCommand:
    return EventCommand(
        run_id=run_id,
        event_type="context.created",
        payload_schema_version=1,
        payload={"content": {"id": "o1", "hidden_reasoning": BODY}},
        actor_ref="test:opaque",
        idempotency_key="opaque:e1",
    )


@pytest.mark.asyncio
async def test_reasoning_body_absent_from_event_row_and_projection(
    database: tuple[async_sessionmaker[AsyncSession], TenantContext, UUID],
) -> None:
    sessions, context, run_id = database
    async with tenant_session(sessions, context) as session:
        uow = CanonicalUnitOfWork(session, context, schema_registry=_registry())
        result = await uow.append_event(_command(run_id))
        await session.flush()

        assert result.created is True
        row = await session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.id == result.event_id)
        )
        assert row is not None
        assert BODY not in str(row.payload)
        expected_ref = opaque_reasoning_ref(BODY)
        assert row.payload["content"]["hidden_reasoning"] == expected_ref
        assert row.payload["content"]["hidden_reasoning"]["opaque_ref"].startswith(
            OPAQUE_REF_PREFIX + "sha256:"
        )

        projection = await session.scalar(
            select(CanonicalProjection).where(CanonicalProjection.run_id == run_id)
        )
        assert projection is not None
        assert BODY not in str(projection.state)

        # digest 链按落库 payload 复算仍成立——scrub 发生在 digest 之前且确定性。
        rows = list(
            (
                await session.scalars(
                    select(CanonicalEvent)
                    .where(CanonicalEvent.run_id == run_id)
                    .order_by(CanonicalEvent.sequence_no)
                )
            ).all()
        )
        verify_event_chain(event_data_from_row(event) for event in rows)
        workspace_id = context.workspace_id
        assert workspace_id is not None
        assert rows[-1].event_digest == build_event_digest(
            organization_id=context.organization_id,
            workspace_id=workspace_id,
            command=EventCommand(
                run_id=run_id,
                event_type=rows[-1].event_type,
                payload_schema_version=rows[-1].payload_schema_version,
                payload=rows[-1].payload,
                actor_ref=rows[-1].actor_ref,
                idempotency_key=rows[-1].idempotency_key,
            ),
            sequence_no=rows[-1].sequence_no,
            previous_event_digest=rows[-1].previous_event_digest,
        )


@pytest.mark.asyncio
async def test_reasoning_scrub_is_deterministic_across_retries(
    database: tuple[async_sessionmaker[AsyncSession], TenantContext, UUID],
) -> None:
    """同一命令重试（同幂等键）零新行：重放 scrub 与首次 scrub 逐字节一致。"""
    sessions, context, run_id = database
    async with tenant_session(sessions, context) as session:
        uow = CanonicalUnitOfWork(session, context, schema_registry=_registry())
        first = await uow.append_event(_command(run_id))
        second = await uow.append_event(_command(run_id))
        assert first.created is True and second.created is False
        assert first.event_digest == second.event_digest
