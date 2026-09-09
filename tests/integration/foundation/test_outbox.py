"""S0-T4 RED: atomic canonical event, projection, audit and outbox behavior."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from zhiwei.contracts.envelope import SchemaRegistry, UnknownSchemaError
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.events import EventCommand, event_data_from_row, verify_event_chain
from zhiwei.persistence.models import (
    AuditEvent,
    CanonicalEvent,
    CanonicalProjection,
    OutboxMessage,
)
from zhiwei.persistence.outbox import (
    MemoryOutboxSink,
    OutboxDelivery,
    OutboxRepository,
    OutboxStateError,
    dispatch_claimed,
)
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.persistence.unit_of_work import (
    AuditChainError,
    CanonicalUnitOfWork,
    EventIdempotencyConflict,
    ProjectionMismatch,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
ADMIN_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)


class NotePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None
    index: int | None = None


def _registry() -> SchemaRegistry:
    registry = SchemaRegistry()
    registry.register("run.note.added", 1, NotePayload)
    return registry


def _uow(session: AsyncSession, context: TenantContext) -> CanonicalUnitOfWork:
    return CanonicalUnitOfWork(session, context, schema_registry=_registry())


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def database() -> AsyncIterator[
    tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ]
]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id, run_id = uuid4(), uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="T4")
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
        yield engine, sessions, context, run_id
    finally:
        await engine.dispose()


def _command(run_id: UUID, key: str, payload: dict[str, Any]) -> EventCommand:
    return EventCommand(
        run_id=run_id,
        event_type="run.note.added",
        payload_schema_version=1,
        payload=payload,
        actor_ref="test:operator",
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_append_atomically_writes_event_projection_audit_and_outbox(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    async with tenant_session(sessions, context) as session:
        result = await _uow(session, context).append_event(
            _command(run_id, "atomic", {"note": "committed"})
        )
        assert result.created is True
        assert result.sequence_no == 1

    async with tenant_session(sessions, context) as session:
        events = (await session.scalars(select(CanonicalEvent))).all()
        projection = await session.get(CanonicalProjection, run_id)
        audits = (await session.scalars(select(AuditEvent))).all()
        messages = (await session.scalars(select(OutboxMessage))).all()

    assert len(events) == len(audits) == len(messages) == 1
    assert projection is not None
    assert projection.sequence_no == 1
    assert projection.head_event_digest == events[0].event_digest
    assert projection.state["events"][0]["payload"] == {"note": "committed", "index": None}
    assert audits[0].payload_digest == events[0].event_digest
    assert messages[0].event_key == str(events[0].id)
    assert messages[0].payload["event_digest"] == events[0].event_digest


@pytest.mark.asyncio
async def test_append_fails_closed_for_unknown_or_invalid_event_schema(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    unknown = _command(run_id, "unknown", {"note": "unknown"}).model_copy(
        update={"event_type": "run.unknown", "payload_schema_version": 99}
    )
    with pytest.raises(UnknownSchemaError):
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(unknown)

    with pytest.raises(ValueError):
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(
                _command(run_id, "invalid", {"note": "valid", "unexpected": True})
            )

    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(CanonicalEvent))).all()) == 0


@pytest.mark.asyncio
async def test_append_snapshots_payload_before_first_await(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    payload: dict[str, Any] = {"note": "before"}
    command_value = _command(run_id, "snapshot", payload)
    async with tenant_session(sessions, context) as session:
        append_task = asyncio.create_task(_uow(session, context).append_event(command_value))
        await asyncio.sleep(0)
        payload["note"] = "after"
        result = await append_task

    async with tenant_session(sessions, context) as session:
        event = await session.get(CanonicalEvent, result.event_id)
        projection = await session.get(CanonicalProjection, run_id)
    assert event is not None
    assert projection is not None
    assert event.payload == {"note": "before", "index": None}
    assert projection.state["events"][0]["payload"] == event.payload
    verify_event_chain([event_data_from_row(event)])


@pytest.mark.asyncio
async def test_append_rolls_back_all_four_writes(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    with pytest.raises(RuntimeError, match="rollback"):
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(
                _command(run_id, "rollback", {"note": "discarded"})
            )
            raise RuntimeError("rollback")

    async with tenant_session(sessions, context) as session:
        for model in (CanonicalEvent, CanonicalProjection, AuditEvent, OutboxMessage):
            assert len((await session.scalars(select(model))).all()) == 0


@pytest.mark.asyncio
async def test_idempotent_append_returns_original_and_rejects_changed_payload(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    command_value = _command(run_id, "stable", {"note": "same"})
    async with tenant_session(sessions, context) as session:
        first = await _uow(session, context).append_event(command_value)
    async with tenant_session(sessions, context) as session:
        repeated = await _uow(session, context).append_event(command_value)

    assert first.created is True
    assert repeated.created is False
    assert repeated.event_id == first.event_id
    assert repeated.event_digest == first.event_digest

    with pytest.raises(EventIdempotencyConflict):
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(
                _command(run_id, "stable", {"note": "different"})
            )

    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(CanonicalEvent))).all()) == 1
        assert len((await session.scalars(select(OutboxMessage))).all()) == 1


@pytest.mark.asyncio
async def test_concurrent_idempotent_replay_creates_one_transaction_bundle(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    command_value = _command(run_id, "concurrent-replay", {"note": "same"})

    async def append() -> tuple[UUID, bool]:
        async with tenant_session(sessions, context) as session:
            result = await _uow(session, context).append_event(command_value)
            return result.event_id, result.created

    first, second = await asyncio.gather(append(), append())
    assert first[0] == second[0]
    assert sorted((first[1], second[1])) == [False, True]

    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(CanonicalEvent))).all()) == 1
        assert len((await session.scalars(select(CanonicalProjection))).all()) == 1
        assert len((await session.scalars(select(AuditEvent))).all()) == 1
        assert len((await session.scalars(select(OutboxMessage))).all()) == 1


@pytest.mark.asyncio
async def test_concurrent_appends_have_stable_sequence_chain_and_no_missing_outbox(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database

    async def append(index: int) -> None:
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(
                _command(run_id, f"concurrent-{index}", {"index": index})
            )

    await asyncio.gather(*(append(index) for index in range(8)))

    async with tenant_session(sessions, context) as session:
        events = (
            await session.scalars(
                select(CanonicalEvent).order_by(CanonicalEvent.sequence_no)
            )
        ).all()
        projection = await session.get(CanonicalProjection, run_id)
        outbox_count = len((await session.scalars(select(OutboxMessage))).all())
        audits = (await session.scalars(select(AuditEvent))).all()

    assert [event.sequence_no for event in events] == list(range(1, 9))
    verify_event_chain(event_data_from_row(event) for event in events)
    assert projection is not None
    assert projection.sequence_no == 8
    assert projection.head_event_digest == events[-1].event_digest
    assert outbox_count == 8
    audit_successors = {
        audit.previous_event_digest: audit.event_digest for audit in audits
    }
    audit_digest = audit_successors.pop(None)
    for _ in range(7):
        audit_digest = audit_successors.pop(audit_digest)
    assert audit_successors == {}


@pytest.mark.asyncio
async def test_projection_can_be_rebuilt_from_committed_events(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    for index in range(3):
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(
                _command(run_id, f"rebuild-{index}", {"index": index})
            )

    async with tenant_session(sessions, context) as session:
        projection = await session.get(CanonicalProjection, run_id)
        assert projection is not None
        expected_state = projection.state
        projection.state = {"corrupted": True}
        projection.sequence_no = 0
        projection.head_event_digest = None

    async with tenant_session(sessions, context) as session:
        rebuilt = await _uow(session, context).rebuild_projection(run_id)

    assert rebuilt.sequence_no == 3
    assert rebuilt.state == expected_state


@pytest.mark.asyncio
async def test_append_rejects_projection_that_disagrees_with_canonical_event_head(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    async with tenant_session(sessions, context) as session:
        await _uow(session, context).append_event(_command(run_id, "first", {"index": 1}))
    async with tenant_session(sessions, context) as session:
        projection = await session.get(CanonicalProjection, run_id)
        assert projection is not None
        projection.sequence_no = 50
        projection.head_event_digest = "sha256:" + "f" * 64

    with pytest.raises(ProjectionMismatch):
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(
                _command(run_id, "must-rebuild", {"index": 2})
            )

    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(CanonicalEvent))).all()) == 1
        await _uow(session, context).rebuild_projection(run_id)
    async with tenant_session(sessions, context) as session:
        result = await _uow(session, context).append_event(
            _command(run_id, "after-rebuild", {"index": 2})
        )
        assert result.sequence_no == 2


@pytest.mark.asyncio
async def test_append_rejects_existing_audit_chain_tampering(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    forged_previous = "sha256:" + uuid4().hex + uuid4().hex
    forged_digest = "sha256:" + uuid4().hex + uuid4().hex
    async with tenant_session(sessions, context) as session:
        await _uow(session, context).append_event(_command(run_id, "first", {"index": 1}))
        session.add(
            AuditEvent(
                id=uuid4(),
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="forged",
                resource_type="run",
                resource_id=run_id,
                actor_ref="attacker",
                payload_digest="sha256:" + "a" * 64,
                previous_event_digest=forged_previous,
                event_digest=forged_digest,
                schema_version=1,
            )
        )

    with pytest.raises(AuditChainError):
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(
                _command(run_id, "blocked-by-audit", {"index": 2})
            )
    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(CanonicalEvent))).all()) == 1


@pytest.mark.asyncio
async def test_outbox_database_rejects_unknown_or_unfenced_processing_state(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, _ = database

    async def insert(status: str) -> None:
        async with tenant_session(sessions, context) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO outbox
                        (id, organization_id, workspace_id, topic, event_key, payload,
                         status, attempts, available_at, schema_version)
                    VALUES
                        (:id, :organization_id, :workspace_id, 'invalid', 'invalid',
                         '{}'::jsonb, :status, 0, now(), 1)
                    """
                ),
                {
                    "id": uuid4(),
                    "organization_id": context.organization_id,
                    "workspace_id": context.workspace_id,
                    "status": status,
                },
            )

    with pytest.raises(DBAPIError):
        await insert("unknown")
    with pytest.raises(DBAPIError):
        await insert("processing")
    with pytest.raises(DBAPIError):
        await insert("dead_letter")


@pytest.mark.asyncio
async def test_outbox_claim_is_concurrent_safe_and_dispatches_to_test_sink(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    for index in range(4):
        async with tenant_session(sessions, context) as session:
            await _uow(session, context).append_event(
                _command(run_id, f"dispatch-{index}", {"index": index})
            )

    now = datetime.now(UTC) + timedelta(seconds=1)

    async def claim(worker_id: str) -> list[OutboxDelivery]:
        async with tenant_session(sessions, context) as session:
            return await OutboxRepository(session, context).claim_batch(
                worker_id=worker_id, limit=3, now=now
            )

    first, second = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert {message.id for message in first}.isdisjoint(message.id for message in second)
    assert len(first) + len(second) == 4

    sink = MemoryOutboxSink()
    reclaim_at = now + timedelta(seconds=31)
    async with tenant_session(sessions, context) as session:
        repository = OutboxRepository(session, context)
        reclaimed = await repository.claim_batch(
            worker_id="dispatcher",
            limit=4,
            now=reclaim_at,
            lease_duration=timedelta(seconds=30),
        )
        assert len(reclaimed) == 4

    stale = first[0]
    async with tenant_session(sessions, context) as session:
        repository = OutboxRepository(session, context)
        with pytest.raises(OutboxStateError):
            await dispatch_claimed(repository, [stale], sink, now=reclaim_at)
    assert sink.deliveries == []

    forged = reclaimed[0].model_copy(
        update={"topic": "forged", "event_key": "forged", "payload": {"forged": True}}
    )
    async with tenant_session(sessions, context) as session:
        repository = OutboxRepository(session, context)
        await dispatch_claimed(repository, [forged], sink, now=reclaim_at)
        await dispatch_claimed(repository, reclaimed[1:], sink, now=reclaim_at)

    assert len(sink.deliveries) == 4
    assert all(message.topic == "canonical.event.committed" for message in sink.deliveries)
    assert all(message.payload != {"forged": True} for message in sink.deliveries)
    async with tenant_session(sessions, context) as session:
        statuses = set((await session.scalars(select(OutboxMessage.status))).all())
        assert statuses == {"delivered"}


@pytest.mark.asyncio
async def test_outbox_reclaim_uses_the_persisted_lease_deadline(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    async with tenant_session(sessions, context) as session:
        await _uow(session, context).append_event(
            _command(run_id, "long-lease", {"note": "leased"})
        )

    now = datetime.now(UTC) + timedelta(seconds=1)
    async with tenant_session(sessions, context) as session:
        claimed = await OutboxRepository(session, context).claim_batch(
            worker_id="long-worker",
            limit=1,
            now=now,
            lease_duration=timedelta(minutes=10),
        )
        assert len(claimed) == 1

    async with tenant_session(sessions, context) as session:
        early = await OutboxRepository(session, context).claim_batch(
            worker_id="short-worker",
            limit=1,
            now=now + timedelta(seconds=31),
            lease_duration=timedelta(seconds=30),
        )
        assert early == []

    async with tenant_session(sessions, context) as session:
        expired = await OutboxRepository(session, context).claim_batch(
            worker_id="recovery-worker",
            limit=1,
            now=now + timedelta(minutes=10),
        )
        assert len(expired) == 1
        assert expired[0].claim_token != claimed[0].claim_token


@pytest.mark.asyncio
async def test_outbox_retry_uses_backoff_then_dead_letters(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    async with tenant_session(sessions, context) as session:
        await _uow(session, context).append_event(
            _command(run_id, "retry", {"note": "failure"})
        )

    now = datetime.now(UTC) + timedelta(seconds=1)
    async with tenant_session(sessions, context) as session:
        repository = OutboxRepository(session, context)
        message = (await repository.claim_batch(worker_id="worker", limit=1, now=now))[0]
        first_retry = await repository.mark_failed(
            message,
            error="temporary",
            now=now,
            max_attempts=2,
            base_delay=timedelta(seconds=5),
        )
        assert first_retry.status == "pending"
        assert first_retry.attempts == 1
        assert first_retry.available_at == now + timedelta(seconds=5)

    later = now + timedelta(seconds=5)
    async with tenant_session(sessions, context) as session:
        repository = OutboxRepository(session, context)
        message = (await repository.claim_batch(worker_id="worker", limit=1, now=later))[0]
        dead = await repository.mark_failed(
            message,
            error="permanent",
            now=later,
            max_attempts=2,
            base_delay=timedelta(seconds=5),
        )
        assert dead.status == "dead_letter"
        assert dead.attempts == 2
        assert dead.dead_lettered_at == later

    async with tenant_session(sessions, context) as session:
        assert await OutboxRepository(session, context).claim_batch(
            worker_id="worker", limit=1, now=later + timedelta(days=1)
        ) == []


@pytest.mark.asyncio
async def test_dispatch_failure_retries_and_sink_deduplicates_after_database_rollback(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
    ],
) -> None:
    _, sessions, context, run_id = database
    async with tenant_session(sessions, context) as session:
        await _uow(session, context).append_event(
            _command(run_id, "dispatch-failure", {"note": "failure"})
        )

    class FailingSink:
        async def publish(self, message: OutboxDelivery) -> None:
            raise RuntimeError(f"cannot publish {message.id}")

    now = datetime.now(UTC) + timedelta(seconds=1)
    async with tenant_session(sessions, context) as session:
        repository = OutboxRepository(session, context)
        claimed = await repository.claim_batch(worker_id="worker", limit=1, now=now)
        await dispatch_claimed(
            repository,
            claimed,
            FailingSink(),
            now=now,
            max_attempts=2,
            base_delay=timedelta(seconds=1),
        )
    async with tenant_session(sessions, context) as session:
        message = (await session.scalars(select(OutboxMessage))).one()
        assert message.status == "pending"
        assert message.attempts == 1

    retry_at = now + timedelta(seconds=1)
    sink = MemoryOutboxSink()
    with pytest.raises(RuntimeError, match="rollback after publish"):
        async with tenant_session(sessions, context) as session:
            repository = OutboxRepository(session, context)
            claimed = await repository.claim_batch(worker_id="worker", limit=1, now=retry_at)
            await dispatch_claimed(repository, claimed, sink, now=retry_at)
            raise RuntimeError("rollback after publish")

    async with tenant_session(sessions, context) as session:
        repository = OutboxRepository(session, context)
        claimed = await repository.claim_batch(worker_id="worker", limit=1, now=retry_at)
        await dispatch_claimed(repository, claimed, sink, now=retry_at)

    assert len(sink.deliveries) == 1
