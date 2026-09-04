"""S5 file connector: local filesystem → Source Ledger with stable locators.

Reads files, computes content digests, and produces SourceObject + SourceVersion
entries for the Source Ledger.  Deterministic: same bytes → same digest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.knowledge.contracts import Locator, SourceObject, SourceVersion


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FileSnapshot(_FrozenModel):
    """Immutable snapshot of a file read from disk."""

    path: str
    locator: Locator
    content: bytes = Field(repr=False)
    content_digest: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)


class FileConnector:
    """Read files from local disk and produce Source Ledger entries."""

    connector_name: str = "file"

    def read(self, path: Path, *, organization_id: Any = None, workspace_id: Any = None) -> FileSnapshot:
        """Read a file and return a FileSnapshot with content digest."""
        raw = path.read_bytes()
        digest = _digest(raw)
        uri = f"file://{path.resolve()}"
        return FileSnapshot(
            path=str(path),
            locator=Locator(connector=self.connector_name, uri=uri),
            content=raw,
            content_digest=digest,
            size_bytes=len(raw),
        )

    def to_source_object(
        self,
        snapshot: FileSnapshot,
        *,
        organization_id: Any,
        workspace_id: Any,
        version_seq: int = 1,
    ) -> SourceObject:
        """Create a SourceObject from a FileSnapshot."""
        return SourceObject(
            id=uuid4(),
            organization_id=organization_id,
            workspace_id=workspace_id,
            source_type="file",
        )

    def to_source_version(
        self,
        snapshot: FileSnapshot,
        *,
        source_object_id: Any,
        version_seq: int = 1,
        **overrides: Any,
    ) -> SourceVersion:
        """Create a SourceVersion from a FileSnapshot."""
        from datetime import UTC, datetime

        now = datetime.now(tz=UTC)
        return SourceVersion(
            id=uuid4(),
            source_object_id=source_object_id,
            version_seq=version_seq,
            locator=snapshot.locator,
            content_digest=snapshot.content_digest,
            observed_at=now,
            valid_at=now,
            **overrides,
        )


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"
