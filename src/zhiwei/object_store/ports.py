"""S3-compatible semantic port for temporary and immutable objects."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ObjectStoreError(RuntimeError):
    """Base error for object store contract violations."""


class ObjectNotFound(ObjectStoreError):
    """Raised when a tenant-scoped object does not exist."""


class InvalidObjectKey(ObjectStoreError):
    """Raised when an opaque object key has invalid syntax."""


class ImmutableObjectConflict(ObjectStoreError):
    """Raised when content does not match an immutable key or existing object."""


class ObjectMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    size_bytes: int = Field(ge=0)
    modified_at: datetime


class ImmutableObjectMetadata(ObjectMetadata):
    content_digest: str


class PromotionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    created: bool
    size_bytes: int = Field(ge=0)
    content_digest: str


class ObjectNamespace(BaseModel):
    """Explicit tenant scope for every physical object operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: UUID
    workspace_id: UUID


class ObjectStore(Protocol):
    def write_temporary(self, namespace: ObjectNamespace, chunks: Iterable[bytes]) -> str: ...

    def read_temporary(self, namespace: ObjectNamespace, key: str) -> Iterator[bytes]: ...

    def stat_temporary(self, namespace: ObjectNamespace, key: str) -> ObjectMetadata: ...

    def temporary_exists(self, namespace: ObjectNamespace, key: str) -> bool: ...

    def list_temporary_before(
        self, namespace: ObjectNamespace, cutoff: datetime
    ) -> list[ObjectMetadata]: ...

    def delete_temporary(self, namespace: ObjectNamespace, key: str) -> None: ...

    def immutable_key(self, namespace: ObjectNamespace, content_digest: str) -> str: ...

    def promote_temporary(
        self,
        namespace: ObjectNamespace,
        temporary_key: str,
        immutable_key: str,
        *,
        expected_digest: str,
        expected_size: int,
    ) -> PromotionResult: ...

    def read_immutable(self, namespace: ObjectNamespace, key: str) -> Iterator[bytes]: ...

    def stat_immutable(
        self, namespace: ObjectNamespace, key: str
    ) -> ImmutableObjectMetadata: ...

    def list_immutable_before(
        self, namespace: ObjectNamespace, cutoff: datetime
    ) -> list[ImmutableObjectMetadata]: ...

    def delete_immutable(self, namespace: ObjectNamespace, key: str) -> None: ...
