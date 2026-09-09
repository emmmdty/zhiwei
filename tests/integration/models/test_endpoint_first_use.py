"""S3 集成：unverified endpoint 首次使用留痕落 PG（ADR-011 §6）。

真实 PG（55432）+ 生产 CanonicalUnitOfWork 落账路径——canonical event + audit +
outbox 同事务；「首次」判定走事件流查重 + advisory lock，跨 run 并发下恰一条。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.models.contracts import (
    ClassificationCeiling,
    NetworkZone,
    TrustTier,
)
from zhiwei.models.first_use import (
    FIRST_USE_EVENT_TYPE,
    EndpointFirstUseDeclaration,
)
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.model_first_use import CanonicalEndpointFirstUseSink
from zhiwei.persistence.models import AuditEvent, CanonicalEvent, OutboxMessage
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
ADMIN_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

_URL_A = "http://127.0.0.1:9/v1"
_URL_B = "http://127.0.0.1:9/v2"


def _declaration(
    base_url: str, declared_by: str = "operator:env-override"
) -> EndpointFirstUseDeclaration:
    return EndpointFirstUseDeclaration(
        base_url=base_url,
        trust_tier=TrustTier.UNVERIFIED,
        network_zone=NetworkZone.UNKNOWN,
        classification_ceiling=ClassificationCeiling.PUBLIC,
        declared_by=declared_by,
    )


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def database() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], TenantContext]]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="S3-first-use")
    try:
        yield sessions, context
    finally:
        await engine.dispose()


async def _insert_run(sessions: async_sessionmaker[AsyncSession], context: TenantContext,
                      run_id: UUID) -> None:
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


async def _event_count(
    sessions: async_sessionmaker[AsyncSession], context: TenantContext
) -> int:
    async with tenant_session(sessions, context) as session:
        count = await session.scalar(
            select(func.count()).select_from(CanonicalEvent).where(
                CanonicalEvent.organization_id == context.organization_id,
                CanonicalEvent.workspace_id == context.workspace_id,
                CanonicalEvent.event_type == FIRST_USE_EVENT_TYPE,
            )
        )
        assert count is not None
        return int(count)


@pytest.mark.asyncio
async def test_first_use_writes_canonical_event_and_audit_atomically(
    database: tuple[async_sessionmaker[AsyncSession], TenantContext],
) -> None:
    sessions, context = database
    run_id = uuid4()
    await _insert_run(sessions, context, run_id)
    sink = CanonicalEndpointFirstUseSink(sessions, context)

    assert await sink.record_first_use(_declaration(_URL_A), run_id=run_id) is True

    async with tenant_session(sessions, context) as session:
        event = await session.scalar(
            select(CanonicalEvent).where(
                CanonicalEvent.event_type == FIRST_USE_EVENT_TYPE,
            )
        )
        assert event is not None
        assert event.payload == {
            "base_url": _URL_A,
            "trust_tier": "unverified",
            "network_zone": "unknown",
            "classification_ceiling": "public",
            "declared_by": "operator:env-override",
        }
        assert event.actor_ref == "system:models"
        assert event.run_id == run_id
        # 同事务审计：payload_digest 指向该 canonical event 的 digest。
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.payload_digest == event.event_digest)
        )
        assert audit is not None
        assert audit.action == "canonical_event.append"
        assert audit.organization_id == context.organization_id
        outbox = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.event_key == str(event.id))
        )
        assert outbox is not None


@pytest.mark.asyncio
async def test_second_use_of_same_endpoint_is_deduplicated(
    database: tuple[async_sessionmaker[AsyncSession], TenantContext],
) -> None:
    sessions, context = database
    sink = CanonicalEndpointFirstUseSink(sessions, context)
    first_run, second_run = uuid4(), uuid4()
    await _insert_run(sessions, context, first_run)
    await _insert_run(sessions, context, second_run)

    assert await sink.record_first_use(_declaration(_URL_A), run_id=first_run) is True
    # 跨 run 再次使用同一 base_url：事件流查重判定已见过 → False，零新行。
    assert await sink.record_first_use(_declaration(_URL_A), run_id=second_run) is False
    assert await _event_count(sessions, context) == 1


@pytest.mark.asyncio
async def test_different_base_urls_are_recorded_separately(
    database: tuple[async_sessionmaker[AsyncSession], TenantContext],
) -> None:
    sessions, context = database
    sink = CanonicalEndpointFirstUseSink(sessions, context)
    run_id = uuid4()
    await _insert_run(sessions, context, run_id)

    assert await sink.record_first_use(_declaration(_URL_A), run_id=run_id) is True
    assert await sink.record_first_use(_declaration(_URL_B), run_id=run_id) is True
    assert await _event_count(sessions, context) == 2


@pytest.mark.asyncio
async def test_concurrent_first_use_records_exactly_once(
    database: tuple[async_sessionmaker[AsyncSession], TenantContext],
) -> None:
    """两个 run 并发首次使用同一 endpoint：advisory lock 串行化查重，恰一条记录。"""
    sessions, context = database
    run_one, run_two = uuid4(), uuid4()
    await _insert_run(sessions, context, run_one)
    await _insert_run(sessions, context, run_two)
    sink = CanonicalEndpointFirstUseSink(sessions, context)

    results = await asyncio.gather(
        sink.record_first_use(_declaration(_URL_A), run_id=run_one),
        sink.record_first_use(_declaration(_URL_A), run_id=run_two),
    )

    assert sorted(results) == [False, True]
    assert await _event_count(sessions, context) == 1
