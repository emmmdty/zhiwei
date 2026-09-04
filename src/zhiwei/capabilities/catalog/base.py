"""Base catalog types and abstract interface for capability discovery and quarantine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now


class CatalogError(Exception):
    """Raised when a catalog operation fails."""


class CatalogOutageError(CatalogError):
    """Raised when an upstream catalog is unreachable or returns an error."""


class SourceType(StrEnum):
    MCP_REGISTRY = "mcp_registry"
    GIT = "git"
    URL = "url"


class CatalogEntry(BaseModel):
    """A single capability discovered from an upstream catalog source."""

    model_config = {"frozen": True}

    id: str = Field(default_factory=lambda: str(new_id()))
    source_type: SourceType
    source_url: str
    publisher: str = ""
    name: str
    description: str = ""
    version: str = ""
    license: str | None = None
    content_digest: str = ""
    size_bytes: int = 0
    fetched_at: datetime = Field(default_factory=utc_now)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_source_url_not_empty(self) -> CatalogEntry:
        if not self.source_url:
            raise ValueError("source_url must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_name_not_empty(self) -> CatalogEntry:
        if not self.name:
            raise ValueError("name must not be empty")
        return self

    @property
    def entry_digest(self) -> str:
        content = {
            "source_type": self.source_type,
            "source_url": self.source_url,
            "publisher": self.publisher,
            "name": self.name,
            "version": self.version,
            "content_digest": self.content_digest,
        }
        return digest_bytes(canonical_json(content))


class QuarantineRecord(BaseModel):
    """An immutable record of a capability imported into quarantine.

    Quarantine means: stored, content-addressed, no credentials attached,
    no execution permitted. Admission must explicitly approve before any
    binding or invocation.
    """

    model_config = {"frozen": True}

    id: str = Field(default_factory=lambda: str(new_id()))
    entry: CatalogEntry
    quarantined_at: datetime = Field(default_factory=utc_now)
    quarantine_digest: str = ""

    @model_validator(mode="after")
    def _compute_quarantine_digest(self) -> QuarantineRecord:
        if not self.quarantine_digest:
            object.__setattr__(
                self,
                "quarantine_digest",
                digest_bytes(
                    canonical_json(
                        {
                            "entry_id": self.entry.id,
                            "entry_digest": self.entry.entry_digest,
                        }
                    )
                ),
            )
        return self


class CatalogSource(ABC):
    """Abstract base for catalog discovery backends."""

    @abstractmethod
    def discover(self, *, page: int = 1, page_size: int = 50) -> list[CatalogEntry]:
        """Discover capabilities from the upstream source with pagination."""
        ...

    @abstractmethod
    def fetch(self, entry: CatalogEntry) -> QuarantineRecord:
        """Fetch the capability content and place it into immutable quarantine."""
        ...
