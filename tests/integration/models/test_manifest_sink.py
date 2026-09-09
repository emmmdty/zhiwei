"""F-R2-12 集成：ContextManifest/TransitionManifest 经 CanonicalManifestSink 落 PG。

manifest 落账走 canonical event 路径（canonical event + audit + outbox 同事务），
与 first_use 留痕同款机制；本文件钉住 PG 侧行为：事件行、审计行、outbox 行同事务
可见，payload 不携带敏感 header 值，幂等键防重。
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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.context.manifests import ContextManifest, TransitionManifest
from zhiwei.models.egress import (
    SWITCH_MANIFEST_EVENT_TYPE,
    WIRE_MANIFEST_EVENT_TYPE,
)
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.model_manifests import CanonicalManifestSink
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

_BODY_SHA = "sha256:" + "a" * 64


def _wire_manifest(sequence_no: int = 0) -> ContextManifest:
    return ContextManifest(
        manifest_id=f"ctx-manifest-{sequence_no}",
        body_sha256=_BODY_SHA,
        body_len=42,
        url="http://127.0.0.1:9/v1/chat/completions",
        method="POST",
        redacted_headers={"authorization": "<redacted>"},
        header_names=("authorization", "content-type"),
        captured_at="2026-09-07T00:00:00+00:00",
        sequence_no=sequence_no,
    )


def _transition_manifest() -> TransitionManifest:
    return TransitionManifest(
        manifest_id="trans-model-switch-1",
        transition_type="model.switch",
        wire_body_digest=_BODY_SHA,
        items_added=0,
        items_removed=0,
        items_unchanged=0,
        occurred_at="2026-09-07T00:00:00+00:00",
    )


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
        await repository.create_workspace(workspace_id, name="P3-manifest-sink")
    try:
        yield sessions, context
    finally:
        await engine.dispose()


async def _insert_run(
    sessions: async_sessionmaker[AsyncSession], context: TenantContext, run_id: object
) -> None:
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


@pytest_asyncio.fixture
async def run_id(database):
    sessions, context = database
    rid = uuid4()
    await _insert_run(sessions, context, rid)
    return rid


@pytest.mark.asyncio
class TestCanonicalManifestSink:
    async def test_wire_manifest_persists_event_audit_outbox(self, database, run_id) -> None:
        sessions, context = database
        sink = CanonicalManifestSink(sessions, context)

        created = await sink.record_wire_manifest(_wire_manifest(), run_id=run_id)
        assert created is True

        async with tenant_session(sessions, context) as session:
            events = (
                (
                    await session.execute(
                        select(CanonicalEvent).where(
                            CanonicalEvent.event_type == WIRE_MANIFEST_EVENT_TYPE
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 1
            assert events[0].payload["body_sha256"] == _BODY_SHA
            assert events[0].payload["redacted_headers"] == {"authorization": "<redacted>"}
            audit_count = await session.scalar(select(func.count()).select_from(AuditEvent))
            assert audit_count is not None and audit_count >= 1
            outbox_count = await session.scalar(
                select(func.count()).select_from(OutboxMessage)
            )
            assert outbox_count is not None and outbox_count >= 1

    async def test_transition_manifest_persists_with_wire_digest(
        self, database, run_id
    ) -> None:
        sessions, context = database
        sink = CanonicalManifestSink(sessions, context)

        created = await sink.record_transition_manifest(
            _transition_manifest(), run_id=run_id
        )
        assert created is True

        async with tenant_session(sessions, context) as session:
            event = await session.scalar(
                select(CanonicalEvent).where(
                    CanonicalEvent.event_type == SWITCH_MANIFEST_EVENT_TYPE
                )
            )
            assert event is not None
            assert event.payload["transition_type"] == "model.switch"
            assert event.payload["wire_body_digest"] == _BODY_SHA

    async def test_duplicate_manifest_is_idempotent(self, database, run_id) -> None:
        sessions, context = database
        sink = CanonicalManifestSink(sessions, context)

        first = await sink.record_wire_manifest(_wire_manifest(), run_id=run_id)
        second = await sink.record_wire_manifest(_wire_manifest(), run_id=run_id)
        assert first is True
        assert second is False

    async def test_distinct_manifests_both_persist(self, database, run_id) -> None:
        sessions, context = database
        sink = CanonicalManifestSink(sessions, context)

        await sink.record_wire_manifest(_wire_manifest(0), run_id=run_id)
        await sink.record_wire_manifest(_wire_manifest(1), run_id=run_id)

        async with tenant_session(sessions, context) as session:
            count = await session.scalar(
                select(func.count())
                .select_from(CanonicalEvent)
                .where(CanonicalEvent.event_type == WIRE_MANIFEST_EVENT_TYPE)
            )
            assert count == 2
