"""Source Ledger: immutable source tracking with version management.

ObjectStore manifest is the content truth; OpenSearch/Context Graph are derived.
Updates create new versions; old Evidence is marked stale but never rewritten.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceObject,
    SourceVersion,
    SourceVersionState,
)


class VersionNotFoundError(Exception):
    """Raised when a requested SourceVersion does not exist."""


class ObjectNotFoundError(Exception):
    """Raised when a requested SourceObject does not exist."""


class DuplicateVersionError(Exception):
    """Raised when attempting to create a version with a duplicate content digest."""


class SourceLedger:
    """In-memory Source Ledger for unit testing and domain logic.

    In production, this is backed by PostgreSQL + ObjectStore; here we model
    the invariants with in-memory dicts to prove correctness independently
    of the persistence layer.
    """

    def __init__(self) -> None:
        self._objects: dict[UUID, SourceObject] = {}
        self._versions: dict[UUID, SourceVersion] = {}
        self._versions_by_object: dict[UUID, list[UUID]] = {}

    def register_object(self, obj: SourceObject) -> None:
        """Register a new SourceObject. Idempotent for same id."""
        if obj.id in self._objects:
            return
        self._objects[obj.id] = obj
        self._versions_by_object[obj.id] = []

    def get_object(self, object_id: UUID) -> SourceObject:
        """Retrieve a SourceObject by id."""
        if object_id not in self._objects:
            raise ObjectNotFoundError(f"SourceObject {object_id} not found")
        return self._objects[object_id]

    def create_version(
        self,
        object_id: UUID,
        *,
        locator: Locator,
        content_digest: str,
        observed_at: datetime,
        valid_at: datetime,
        acl: ACLSnapshot | None = None,
        classification: Classification | None = None,
        parent_version_id: UUID | None = None,
        connector_version: str = "1",
        parser_version: str = "1",
        index_version: str = "1",
        metadata: dict | None = None,
    ) -> SourceVersion:
        """Create a new immutable version of a SourceObject.

        Raises DuplicateVersionError if a version with the same content_digest
        already exists for this object.
        """
        obj = self.get_object(object_id)

        version_seq = len(self._versions_by_object.get(object_id, [])) + 1

        # Enforce no duplicate content digest per object
        existing_digests = {
            self._versions[vid].content_digest
            for vid in self._versions_by_object.get(object_id, [])
        }
        if content_digest in existing_digests:
            raise DuplicateVersionError(
                f"Object {object_id} already has version with digest {content_digest}"
            )

        version = SourceVersion(
            id=new_id(),
            source_object_id=object_id,
            version_seq=version_seq,
            locator=locator,
            content_digest=content_digest,
            observed_at=observed_at,
            valid_at=valid_at,
            acl=acl or obj.acl,
            classification=classification or obj.classification,
            state=SourceVersionState.ACTIVE,
            parent_version_id=parent_version_id,
            connector_version=connector_version,
            parser_version=parser_version,
            index_version=index_version,
            metadata=metadata or {},
        )

        self._versions[version.id] = version
        self._versions_by_object[object_id].append(version.id)
        return version

    def get_version(self, version_id: UUID) -> SourceVersion:
        """Retrieve a SourceVersion by id."""
        if version_id not in self._versions:
            raise VersionNotFoundError(f"SourceVersion {version_id} not found")
        return self._versions[version_id]

    def list_versions(self, object_id: UUID) -> list[SourceVersion]:
        """List all versions of a SourceObject in version_seq order."""
        self.get_object(object_id)  # raises if missing
        version_ids = self._versions_by_object.get(object_id, [])
        return [self._versions[vid] for vid in version_ids]

    def mark_stale(self, version_id: UUID) -> SourceVersion:
        """Mark a version as stale. The version remains in the ledger.

        This is called when a newer version supersedes an older one.
        Evidence derived from a stale version is still valid for historical
        Runs but will not be surfaced for new queries.
        """
        version = self.get_version(version_id)
        if version.state == SourceVersionState.REVOKED:
            raise ValueError("Cannot mark a revoked version as stale")
        if version.tombstone:
            raise ValueError("Cannot mark a tombstone version as stale")

        updated = version.model_copy(update={"state": SourceVersionState.STALE})
        self._versions[version_id] = updated
        return updated

    def revoke_version(self, version_id: UUID) -> SourceVersion:
        """Revoke a version (delete/revoke priority from spec §3).

        Revoked versions are not surfaced for any query. Historical Runs
        retain the Evidence reference but visibility is denied (ADR-006).
        """
        version = self.get_version(version_id)
        updated = version.model_copy(
            update={"state": SourceVersionState.REVOKED, "tombstone": True}
        )
        self._versions[version_id] = updated
        return updated

    def latest_version(self, object_id: UUID) -> SourceVersion | None:
        """Return the latest active version of a SourceObject, or None."""
        self.get_object(object_id)  # raises if missing
        version_ids = self._versions_by_object.get(object_id, [])
        for vid in reversed(version_ids):
            version = self._versions[vid]
            if version.state == SourceVersionState.ACTIVE:
                return version
        return None
