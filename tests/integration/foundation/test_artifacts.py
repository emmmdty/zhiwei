"""S0-T5 RED: verified object promotion and tenant manifest commit."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.contracts.envelope import SchemaRegistry, UnknownSchemaError
from zhiwei.object_store.manifests import (
    ArtifactManifestCommand,
    ArtifactVerificationError,
)
from zhiwei.object_store.ports import ImmutableObjectConflict, ObjectNamespace, ObjectNotFound
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.object_store.service import ArtifactService
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import ArtifactManifest
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


class BinaryArtifactSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


ARTIFACT_SCHEMAS = SchemaRegistry()
ARTIFACT_SCHEMAS.register("artifact.binary", 1, BinaryArtifactSchema)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[
    tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ]
]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="T5")
    try:
        yield engine, sessions, context, uuid4(), PosixObjectStore(tmp_path / "objects")
    finally:
        await engine.dispose()


def _command(
    owner_id: UUID,
    *,
    digest: str,
    size: int,
) -> ArtifactManifestCommand:
    return ArtifactManifestCommand(
        owner_resource_type="run",
        owner_resource_id=owner_id,
        content_digest=digest,
        size_bytes=size,
        media_type="application/octet-stream",
        artifact_schema_id="artifact.binary",
        artifact_schema_version=1,
        classification="PUBLIC",
        retention={"policy": "test"},
    )


def _namespace(context: TenantContext) -> ObjectNamespace:
    assert context.workspace_id is not None
    return ObjectNamespace(
        organization_id=context.organization_id, workspace_id=context.workspace_id
    )


def _digest_lock_key(context: TenantContext, content_digest: str) -> int:
    assert context.workspace_id is not None
    identity = (
        f"{context.organization_id.hex}:{context.workspace_id.hex}:immutable:{content_digest}"
    )
    raw = int(digest_bytes(identity.encode("utf-8")).removeprefix("sha256:")[:16], 16)
    return raw if raw < 2**63 else raw - 2**64


def _service(
    session: AsyncSession,
    context: TenantContext,
    store: PosixObjectStore,
    registry: SchemaRegistry = ARTIFACT_SCHEMAS,
) -> ArtifactService:
    return ArtifactService(session, context, store, registry)


@pytest.mark.asyncio
async def test_temporary_verify_promote_then_manifest_commit(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"verified artifact"
    temporary = store.write_temporary(_namespace(context), [content])
    command_value = _command(owner_id, digest=digest_bytes(content), size=len(content))

    async with tenant_session(sessions, context) as session:
        committed = await _service(session, context, store).commit_upload(
            temporary, command_value
        )

    async with tenant_session(sessions, context) as session:
        row = await session.get(ArtifactManifest, committed.manifest_id)
        verified = await _service(session, context, store).verify_manifest(
            committed.manifest_id
        )
    assert row is not None
    assert verified.content_digest == digest_bytes(content)
    assert verified.object_key == committed.object_key
    assert b"".join(store.read_immutable(_namespace(context), row.object_key)) == content


@pytest.mark.asyncio
async def test_concurrent_identical_uploads_reuse_one_manifest(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"concurrent artifact"
    command_value = _command(owner_id, digest=digest_bytes(content), size=len(content))
    first_temporary = store.write_temporary(_namespace(context), [content])
    second_temporary = store.write_temporary(_namespace(context), [content])

    async def commit(temporary_key: str) -> UUID:
        async with tenant_session(sessions, context) as session:
            result = await _service(session, context, store).commit_upload(
                temporary_key, command_value
            )
            return result.manifest_id

    first, second = await asyncio.gather(commit(first_temporary), commit(second_temporary))
    assert first == second
    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(ArtifactManifest))).all()) == 1


@pytest.mark.asyncio
async def test_digest_mismatch_never_promotes_or_commits_manifest(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    temporary = store.write_temporary(_namespace(context), [b"actual"])
    command_value = _command(owner_id, digest=digest_bytes(b"expected"), size=len(b"expected"))
    with pytest.raises(ArtifactVerificationError, match="digest"):
        async with tenant_session(sessions, context) as session:
            await _service(session, context, store).commit_upload(temporary, command_value)

    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(ArtifactManifest))).all()) == 0
    assert store.list_immutable_before(
        _namespace(context), datetime.max.replace(tzinfo=UTC)
    ) == []
    assert not store.temporary_exists(_namespace(context), temporary)


@pytest.mark.parametrize(
    "schema_update",
    [
        {"artifact_schema_id": "artifact.unknown"},
        {"artifact_schema_version": 2},
    ],
    ids=["unknown-id", "unknown-version"],
)
@pytest.mark.asyncio
async def test_unknown_artifact_schema_is_rejected_before_promotion(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
    schema_update: dict[str, object],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"unknown artifact schema"
    temporary = store.write_temporary(_namespace(context), [content])
    command_value = _command(
        owner_id, digest=digest_bytes(content), size=len(content)
    ).model_copy(update=schema_update)

    with pytest.raises(UnknownSchemaError):
        async with tenant_session(sessions, context) as session:
            await _service(session, context, store).commit_upload(temporary, command_value)

    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(ArtifactManifest))).all()) == 0
    assert store.list_immutable_before(
        _namespace(context), datetime.max.replace(tzinfo=UTC)
    ) == []


@pytest.mark.parametrize(
    ("schema_column", "schema_value"),
    [("artifact_schema_id", "artifact.unknown"), ("schema_version", 2)],
    ids=["unknown-id", "unknown-version"],
)
@pytest.mark.asyncio
async def test_manifest_verification_rejects_unknown_persisted_schema(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
    schema_column: str,
    schema_value: str | int,
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"persisted schema must remain registered"
    temporary = store.write_temporary(_namespace(context), [content])
    async with tenant_session(sessions, context) as session:
        committed = await _service(session, context, store).commit_upload(
            temporary,
            _command(owner_id, digest=digest_bytes(content), size=len(content)),
        )

    admin = create_database_engine(ADMIN_URL)
    admin_sessions = create_session_factory(admin)
    try:
        async with admin_sessions() as session, session.begin():
            update_statement = (
                "UPDATE artifact_manifests SET artifact_schema_id = :schema_value "
                "WHERE id = :manifest_id"
                if schema_column == "artifact_schema_id"
                else "UPDATE artifact_manifests SET schema_version = :schema_value "
                "WHERE id = :manifest_id"
            )
            await session.execute(
                text(update_statement),
                {"manifest_id": committed.manifest_id, "schema_value": schema_value},
            )
    finally:
        await admin.dispose()

    with pytest.raises(UnknownSchemaError):
        async with tenant_session(sessions, context) as session:
            await _service(session, context, store).verify_manifest(committed.manifest_id)


@pytest.mark.asyncio
async def test_manifest_cannot_be_committed_before_immutable_object_exists(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"missing"
    command_value = _command(owner_id, digest=digest_bytes(content), size=len(content))
    with pytest.raises(ObjectNotFound):
        async with tenant_session(sessions, context) as session:
            await _service(session, context, store).commit_existing(command_value)

    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(ArtifactManifest))).all()) == 0


@pytest.mark.asyncio
async def test_missing_or_corrupt_committed_object_fails_verification(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"will be corrupted"
    command_value = _command(owner_id, digest=digest_bytes(content), size=len(content))
    temporary = store.write_temporary(_namespace(context), [content])
    async with tenant_session(sessions, context) as session:
        committed = await _service(session, context, store).commit_upload(
            temporary, command_value
        )

    store.debug_replace_immutable(_namespace(context), committed.object_key, b"tampered")
    with pytest.raises(ArtifactVerificationError, match="digest"):
        async with tenant_session(sessions, context) as session:
            await _service(session, context, store).verify_manifest(committed.manifest_id)

    store.delete_immutable(_namespace(context), committed.object_key)
    with pytest.raises(ObjectNotFound):
        async with tenant_session(sessions, context) as session:
            await _service(session, context, store).verify_manifest(committed.manifest_id)


@pytest.mark.asyncio
async def test_immutable_collision_discards_failed_temporary_upload(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"collision content"
    command_value = _command(owner_id, digest=digest_bytes(content), size=len(content))
    namespace = _namespace(context)
    object_key = store.immutable_key(namespace, command_value.content_digest)
    first = store.write_temporary(namespace, [content])
    store.promote_temporary(
        namespace,
        first,
        object_key,
        expected_digest=command_value.content_digest,
        expected_size=command_value.size_bytes,
    )
    store.debug_replace_immutable(namespace, object_key, b"corrupt")
    failed = store.write_temporary(namespace, [content])

    with pytest.raises(ImmutableObjectConflict, match="immutable"):
        async with tenant_session(sessions, context) as session:
            await _service(session, context, store).commit_upload(failed, command_value)

    assert not store.temporary_exists(namespace, failed)


@pytest.mark.asyncio
async def test_database_rollback_leaves_reconcilable_orphan(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"orphan after rollback"
    command_value = _command(owner_id, digest=digest_bytes(content), size=len(content))
    namespace = _namespace(context)
    object_key = store.immutable_key(namespace, command_value.content_digest)
    temporary = store.write_temporary(namespace, [content])
    with pytest.raises(RuntimeError, match="rollback"):
        async with tenant_session(sessions, context) as session:
            await _service(session, context, store).commit_upload(temporary, command_value)
            raise RuntimeError("rollback")

    assert b"".join(store.read_immutable(namespace, object_key)) == content
    async with tenant_session(sessions, context) as session:
        assert len((await session.scalars(select(ArtifactManifest))).all()) == 0

    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    async with tenant_session(sessions, context) as session:
        removed = await _service(session, context, store).reconcile_orphans(cutoff=cutoff)
    assert removed == [object_key]
    with pytest.raises(ObjectNotFound):
        tuple(store.read_immutable(namespace, object_key))


@pytest.mark.asyncio
async def test_abandoned_temporary_upload_is_reconcilable_after_cutoff(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, _, store = database
    temporary = store.write_temporary(_namespace(context), [b"abandoned temporary"])

    async with tenant_session(sessions, context) as session:
        removed = await _service(session, context, store).reconcile_temporary(
            cutoff=datetime.now(UTC) + timedelta(seconds=1)
        )

    assert removed == [temporary]
    assert not store.temporary_exists(_namespace(context), temporary)


@pytest.mark.asyncio
async def test_reconciler_cannot_race_manifest_commit_and_delete_its_object(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"commit and reconcile race"
    command_value = _command(owner_id, digest=digest_bytes(content), size=len(content))
    namespace = _namespace(context)
    object_key = store.immutable_key(namespace, command_value.content_digest)
    temporary = store.write_temporary(namespace, [content])
    store.promote_temporary(
        namespace,
        temporary,
        object_key,
        expected_digest=command_value.content_digest,
        expected_size=command_value.size_bytes,
    )
    store.debug_set_mtime(namespace, object_key, datetime.now(UTC) - timedelta(hours=2))

    async def commit() -> UUID:
        async with tenant_session(sessions, context) as session:
            result = await _service(session, context, store).commit_existing(command_value)
            return result.manifest_id

    async def reconcile() -> list[str]:
        async with tenant_session(sessions, context) as session:
            return await _service(session, context, store).reconcile_orphans(
                cutoff=datetime.now(UTC) - timedelta(hours=1)
            )

    async with tenant_session(sessions, context) as blocker:
        await blocker.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _digest_lock_key(context, command_value.content_digest)},
        )
        commit_task = asyncio.create_task(commit())
        await asyncio.sleep(0.05)
        reconcile_task = asyncio.create_task(reconcile())
        await asyncio.sleep(0.05)

    manifest_id, removed = await asyncio.gather(commit_task, reconcile_task)
    assert removed == []
    async with tenant_session(sessions, context) as session:
        verified = await _service(session, context, store).verify_manifest(manifest_id)
    assert verified.object_key == object_key


@pytest.mark.asyncio
async def test_reconciler_rechecks_cutoff_after_waiting_for_object_lock(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, _, store = database
    namespace = _namespace(context)
    content = b"refreshed orphan candidate"
    content_digest = digest_bytes(content)
    object_key = store.immutable_key(namespace, content_digest)
    old_temporary = store.write_temporary(namespace, [content])
    store.promote_temporary(
        namespace,
        old_temporary,
        object_key,
        expected_digest=content_digest,
        expected_size=len(content),
    )
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    store.debug_set_mtime(namespace, object_key, cutoff - timedelta(hours=1))

    async def reconcile() -> list[str]:
        async with tenant_session(sessions, context) as session:
            return await _service(session, context, store).reconcile_orphans(cutoff=cutoff)

    async with tenant_session(sessions, context) as blocker:
        await blocker.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _digest_lock_key(context, content_digest)},
        )
        reconcile_task = asyncio.create_task(reconcile())
        await asyncio.sleep(0.05)
        store.delete_immutable(namespace, object_key)
        fresh_temporary = store.write_temporary(namespace, [content])
        store.promote_temporary(
            namespace,
            fresh_temporary,
            object_key,
            expected_digest=content_digest,
            expected_size=len(content),
        )

    assert await reconcile_task == []
    assert b"".join(store.read_immutable(namespace, object_key)) == content


@pytest.mark.asyncio
async def test_workspace_reconciliation_cannot_delete_another_workspace_object(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    protected_content = b"workspace A manifest"
    temporary = store.write_temporary(_namespace(context), [protected_content])
    async with tenant_session(sessions, context) as session:
        protected = await _service(session, context, store).commit_upload(
            temporary,
            _command(
                owner_id,
                digest=digest_bytes(protected_content),
                size=len(protected_content),
            ),
        )

    other_workspace_id = uuid4()
    admin = create_database_engine(ADMIN_URL)
    admin_sessions = create_session_factory(admin)
    try:
        async with admin_sessions() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO workspaces (id, organization_id, name, schema_version)
                    VALUES (:id, :organization_id, 'other workspace', 1)
                    """
                ),
                {"id": other_workspace_id, "organization_id": context.organization_id},
            )
    finally:
        await admin.dispose()

    other_context = TenantContext(
        organization_id=context.organization_id, workspace_id=other_workspace_id
    )
    orphan_content = b"workspace B orphan"
    orphan_digest = digest_bytes(orphan_content)
    orphan_key = store.immutable_key(_namespace(other_context), orphan_digest)
    orphan_temporary = store.write_temporary(_namespace(other_context), [orphan_content])
    store.promote_temporary(
        _namespace(other_context),
        orphan_temporary,
        orphan_key,
        expected_digest=orphan_digest,
        expected_size=len(orphan_content),
    )

    async with tenant_session(sessions, other_context) as session:
        removed = await _service(session, other_context, store).reconcile_orphans(
            cutoff=datetime.now(UTC) + timedelta(seconds=1)
        )

    assert removed == [orphan_key]
    assert (
        b"".join(store.read_immutable(_namespace(context), protected.object_key))
        == protected_content
    )


@pytest.mark.asyncio
async def test_manifest_and_object_namespace_reject_cross_tenant_access(
    database: tuple[
        AsyncEngine,
        async_sessionmaker[AsyncSession],
        TenantContext,
        UUID,
        PosixObjectStore,
    ],
) -> None:
    _, sessions, context, owner_id, store = database
    content = b"tenant private"
    temporary = store.write_temporary(_namespace(context), [content])
    async with tenant_session(sessions, context) as session:
        committed = await _service(session, context, store).commit_upload(
            temporary, _command(owner_id, digest=digest_bytes(content), size=len(content))
        )

    other_context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
    admin = create_database_engine(ADMIN_URL)
    admin_sessions = create_session_factory(admin)
    try:
        async with admin_sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO organizations (id, status, schema_version) "
                    "VALUES (:id, 'active', 1)"
                ),
                {"id": other_context.organization_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO workspaces (id, organization_id, name, schema_version)
                    VALUES (:id, :organization_id, 'other', 1)
                    """
                ),
                {
                    "id": other_context.workspace_id,
                    "organization_id": other_context.organization_id,
                },
            )
    finally:
        await admin.dispose()

    with pytest.raises(ObjectNotFound):
        async with tenant_session(sessions, other_context) as session:
            await _service(session, other_context, store).verify_manifest(
                committed.manifest_id
            )
    with pytest.raises(ObjectNotFound):
        tuple(store.read_immutable(_namespace(other_context), committed.object_key))
