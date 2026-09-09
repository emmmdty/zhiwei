"""Official MCP Registry discoverer with pagination and identity."""

from __future__ import annotations

from pydantic import BaseModel, model_validator

from zhiwei.capabilities.catalog.base import (
    CatalogEntry,
    CatalogOutageError,
    CatalogSource,
    QuarantineRecord,
    SourceType,
)
from zhiwei.contracts.canonical import canonical_json, digest_bytes


class McpRegistryEntry(BaseModel):
    """Raw entry schema from the official MCP Registry API."""

    model_config = {"frozen": True}

    name: str
    description: str = ""
    publisher: str = ""
    version: str = ""
    url: str
    license: str | None = None
    homepage: str | None = None

    @model_validator(mode="after")
    def _validate_url_not_empty(self) -> McpRegistryEntry:
        if not self.url:
            raise ValueError("url must not be empty")
        return self


class McpRegistryPage(BaseModel):
    """A single page of results from the MCP Registry."""

    model_config = {"frozen": True}

    entries: tuple[McpRegistryEntry, ...] = ()
    total: int = 0
    page: int = 1
    page_size: int = 50
    has_next: bool = False


class McpRegistryClient:
    """HTTP client port for the official MCP Registry.

    Implementations provide the actual HTTP transport; this port
    defines the contract for discoverability and fetching.
    """

    def fetch_page(self, *, page: int, page_size: int) -> McpRegistryPage:
        raise NotImplementedError("subclasses must implement fetch_page")

    def fetch_raw(self, url: str) -> bytes:
        raise NotImplementedError("subclasses must implement fetch_raw")


class McpRegistryDiscoverer(CatalogSource):
    """Discover capabilities from the official MCP Registry."""

    def __init__(self, client: McpRegistryClient) -> None:
        self._client = client

    def discover(self, *, page: int = 1, page_size: int = 50) -> list[CatalogEntry]:
        try:
            result = self._client.fetch_page(page=page, page_size=page_size)
        except Exception as exc:
            raise CatalogOutageError(f"MCP Registry unavailable: {exc}") from exc

        entries: list[CatalogEntry] = []
        for raw in result.entries:
            content = digest_bytes(canonical_json({"url": raw.url, "name": raw.name}))
            entries.append(
                CatalogEntry(
                    source_type=SourceType.MCP_REGISTRY,
                    source_url=raw.url,
                    publisher=raw.publisher,
                    name=raw.name,
                    description=raw.description,
                    version=raw.version,
                    license=raw.license,
                    content_digest=content,
                    raw_metadata={"homepage": raw.homepage},
                )
            )
        return entries

    def fetch(self, entry: CatalogEntry) -> QuarantineRecord:
        try:
            raw_content = self._client.fetch_raw(entry.source_url)
        except Exception as exc:
            raise CatalogOutageError(
                f"Failed to fetch {entry.source_url}: {exc}"
            ) from exc

        content_digest = digest_bytes(raw_content)
        updated_entry = entry.model_copy(
            update={"content_digest": content_digest, "size_bytes": len(raw_content)}
        )
        return QuarantineRecord(entry=updated_entry)
