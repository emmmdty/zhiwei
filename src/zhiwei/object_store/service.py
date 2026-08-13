"""Verified object promotion and PostgreSQL artifact manifest commit."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.envelope import SchemaRegistry
from zhiwei.contracts.time import ensure_utc
from zhiwei.object_store.manifests import (
    ArtifactManifestCommand,
    ArtifactVerificationError,
    CommittedArtifact,
)
from zhiwei.object_store.ports import ObjectNamespace, ObjectNotFound, ObjectStore
from zhiwei.persistence.models import ArtifactManifest
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired


class ArtifactService:
    """Tenant-explicit coordinator for immutable objects and relational manifests."""

    def __init__(
        self,
        session: AsyncSession,
        context: TenantContext | None,
        store: ObjectStore,
        schema_registry: SchemaRegistry,
    ) -> None:
        if context is None or context.workspace_id is None:
            raise TenantContextRequired("artifact operations require workspace context")
        self._session = session
        self._context = context
        self._workspace_id = context.workspace_id
        self._namespace = ObjectNamespace(
            organization_id=context.organization_id, workspace_id=context.workspace_id
        )
        self._store = store
        self._schema_registry = schema_registry

    async def commit_upload(
        self, temporary_key: str, command: ArtifactManifestCommand
    ) -> CommittedArtifact:
        command = command.model_copy(deep=True)
        self._resolve_schema(command.artifact_schema_id, command.artifact_schema_version)
        object_key = self._store.immutable_key(self._namespace, command.content_digest)
        await self._lock_temporary(temporary_key)
        await self._lock_object(command.content_digest)
        try:
            digest, size = self._digest_chunks(
                self._store.read_temporary(self._namespace, temporary_key)
            )
            if digest != command.content_digest or size != command.size_bytes:
                raise ArtifactVerificationError("temporary object digest or size mismatch")
            self._store.promote_temporary(
                self._namespace,
                temporary_key,
                object_key,
                expected_digest=command.content_digest,
                expected_size=command.size_bytes,
            )
        except BaseException:
            self._discard_temporary(temporary_key)
            raise
        return await self._commit_manifest(object_key, command)

    async def commit_existing(
        self, command: ArtifactManifestCommand
    ) -> CommittedArtifact:
        command = command.model_copy(deep=True)
        self._resolve_schema(command.artifact_schema_id, command.artifact_schema_version)
        object_key = self._store.immutable_key(self._namespace, command.content_digest)
        await self._lock_object(command.content_digest)
        digest, size = self._digest_chunks(
            self._store.read_immutable(self._namespace, object_key)
        )
        if digest != command.content_digest or size != command.size_bytes:
            raise ArtifactVerificationError("immutable object digest or size mismatch")
        return await self._commit_manifest(object_key, command)

    async def verify_manifest(self, manifest_id: UUID) -> CommittedArtifact:
        row = await self._session.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.id == manifest_id,
                ArtifactManifest.organization_id == self._context.organization_id,
                ArtifactManifest.workspace_id == self._workspace_id,
            )
        )
        if row is None:
            raise ObjectNotFound("artifact manifest is missing from tenant scope")
        self._resolve_schema(row.artifact_schema_id, row.schema_version)
        digest, size = self._digest_chunks(
            self._store.read_immutable(self._namespace, row.object_key)
        )
        if digest != row.content_digest or size != row.size_bytes:
            raise ArtifactVerificationError("artifact object digest or size does not match manifest")
        return self._result(row)

    async def reconcile_orphans(self, *, cutoff: datetime) -> list[str]:
        cutoff = ensure_utc(cutoff)
        candidates = self._store.list_immutable_before(self._namespace, cutoff)
        removed: list[str] = []
        for candidate in candidates:
            await self._lock_object(candidate.content_digest)
            try:
                current = self._store.stat_immutable(self._namespace, candidate.key)
            except ObjectNotFound:
                continue
            if current.modified_at > cutoff:
                continue
            manifest_exists = await self._session.scalar(
                select(ArtifactManifest.id)
                .where(
                    ArtifactManifest.organization_id == self._context.organization_id,
                    ArtifactManifest.workspace_id == self._workspace_id,
                    ArtifactManifest.object_key == candidate.key,
                )
                .limit(1)
            )
            if manifest_exists is None:
                try:
                    self._store.delete_immutable(self._namespace, candidate.key)
                except ObjectNotFound:
                    continue
                else:
                    removed.append(candidate.key)
        return removed

    async def reconcile_temporary(self, *, cutoff: datetime) -> list[str]:
        cutoff = ensure_utc(cutoff)
        candidates = self._store.list_temporary_before(self._namespace, cutoff)
        removed: list[str] = []
        for candidate in candidates:
            await self._lock_temporary(candidate.key)
            try:
                current = self._store.stat_temporary(self._namespace, candidate.key)
            except ObjectNotFound:
                continue
            if current.modified_at > cutoff:
                continue
            try:
                self._store.delete_temporary(self._namespace, candidate.key)
            except ObjectNotFound:
                continue
            else:
                removed.append(candidate.key)
        return removed

    async def _commit_manifest(
        self, object_key: str, command: ArtifactManifestCommand
    ) -> CommittedArtifact:
        existing = await self._session.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.organization_id == self._context.organization_id,
                ArtifactManifest.workspace_id == self._workspace_id,
                ArtifactManifest.object_key == object_key,
            )
        )
        if existing is not None:
            self._assert_manifest_matches(existing, command)
            return self._result(existing)

        row = ArtifactManifest(
            id=uuid4(),
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            owner_resource_type=command.owner_resource_type,
            owner_resource_id=command.owner_resource_id,
            object_key=object_key,
            content_digest=command.content_digest,
            size_bytes=command.size_bytes,
            media_type=command.media_type,
            artifact_schema_id=command.artifact_schema_id,
            schema_version=command.artifact_schema_version,
            classification=command.classification,
            retention=command.retention,
            encryption_key_ref=command.encryption_key_ref,
        )
        self._session.add(row)
        await self._session.flush()
        return self._result(row)

    @staticmethod
    def _assert_manifest_matches(
        row: ArtifactManifest, command: ArtifactManifestCommand
    ) -> None:
        if (
            row.owner_resource_type != command.owner_resource_type
            or row.owner_resource_id != command.owner_resource_id
            or row.content_digest != command.content_digest
            or row.size_bytes != command.size_bytes
            or row.media_type != command.media_type
            or row.artifact_schema_id != command.artifact_schema_id
            or row.schema_version != command.artifact_schema_version
            or row.classification != command.classification
            or row.retention != command.retention
            or row.encryption_key_ref != command.encryption_key_ref
        ):
            raise ArtifactVerificationError("existing manifest metadata conflicts with command")

    async def _lock_object(self, content_digest: str) -> None:
        await self._lock_scope("immutable", content_digest)

    async def _lock_temporary(self, temporary_key: str) -> None:
        await self._lock_scope("temporary", temporary_key)

    async def _lock_scope(self, kind: str, identity: str) -> None:
        lock_identity = (
            f"{self._context.organization_id.hex}:{self._workspace_id.hex}:{kind}:{identity}"
        )
        lock_digest = hashlib.sha256(lock_identity.encode("utf-8")).hexdigest()
        raw = int(lock_digest[:16], 16)
        lock_key = raw if raw < 2**63 else raw - 2**64
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
        )

    def _resolve_schema(self, schema_id: str, schema_version: int) -> None:
        self._schema_registry.resolve(schema_id, schema_version)

    def _discard_temporary(self, temporary_key: str) -> None:
        with suppress(ObjectNotFound):
            self._store.delete_temporary(self._namespace, temporary_key)

    @staticmethod
    def _result(row: ArtifactManifest) -> CommittedArtifact:
        return CommittedArtifact(
            manifest_id=row.id,
            object_key=row.object_key,
            content_digest=row.content_digest,
            size_bytes=row.size_bytes,
        )

    @staticmethod
    def _digest_chunks(chunks: Iterable[bytes]) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("object chunks must be bytes")
            hasher.update(chunk)
            size += len(chunk)
        return f"sha256:{hasher.hexdigest()}", size
