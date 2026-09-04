"""Generic URL/ZIP import for capability discovery and quarantine."""

from __future__ import annotations

from pydantic import BaseModel, model_validator

from zhiwei.capabilities.catalog.base import (
    CatalogEntry,
    CatalogError,
    CatalogOutageError,
    CatalogSource,
    QuarantineRecord,
)
from zhiwei.contracts.canonical import digest_bytes


class ImportRequest(BaseModel):
    """Parameters for a URL or ZIP import."""

    model_config = {"frozen": True}

    url: str
    content_type: str = "application/octet-stream"

    @model_validator(mode="after")
    def _validate_url_not_empty(self) -> ImportRequest:
        if not self.url:
            raise ValueError("url must not be empty")
        return self


class FetchResult(BaseModel):
    """Result of an HTTP fetch operation."""

    model_config = {"frozen": True}

    content: bytes
    content_type: str = "application/octet-stream"
    etag: str | None = None
    last_modified: str | None = None

    @model_validator(mode="after")
    def _validate_content_not_empty(self) -> FetchResult:
        if not self.content:
            raise ValueError("content must not be empty")
        return self


class UrlClient:
    """Port for HTTP fetch operations. Implementations provide the actual transport."""

    def fetch(self, request: ImportRequest) -> FetchResult:
        raise NotImplementedError("subclasses must implement fetch")


class UrlImporter(CatalogSource):
    """Import capabilities from arbitrary URLs (HTTP, ZIP archives, etc.)."""

    def __init__(self, client: UrlClient) -> None:
        self._client = client

    def discover(self, *, page: int = 1, page_size: int = 50) -> list[CatalogEntry]:
        raise CatalogError(
            "URL sources do not support enumeration; use fetch with a known URL"
        )

    def fetch(self, entry: CatalogEntry) -> QuarantineRecord:
        request = ImportRequest(url=entry.source_url)
        try:
            result = self._client.fetch(request)
        except Exception as exc:
            raise CatalogOutageError(
                f"URL fetch failed for {entry.source_url}: {exc}"
            ) from exc

        content_digest = digest_bytes(result.content)
        metadata = dict(entry.raw_metadata)
        if result.etag:
            metadata["etag"] = result.etag
        if result.last_modified:
            metadata["last_modified"] = result.last_modified

        updated_entry = entry.model_copy(
            update={
                "content_digest": content_digest,
                "size_bytes": len(result.content),
                "raw_metadata": metadata,
            }
        )
        return QuarantineRecord(entry=updated_entry)
