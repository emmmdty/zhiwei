"""S4-T2 contract: catalog discovery and quarantine tests."""

from __future__ import annotations

from typing import Any

import pytest

from zhiwei.capabilities.catalog.base import (
    CatalogEntry,
    CatalogError,
    CatalogOutageError,
    CatalogSource,
    QuarantineRecord,
    SourceType,
)
from zhiwei.capabilities.catalog.git import GitClient, GitImporter, GitRef
from zhiwei.capabilities.catalog.imports import (
    FetchResult,
    ImportRequest,
    UrlClient,
    UrlImporter,
)
from zhiwei.capabilities.catalog.mcp_registry import (
    McpRegistryClient,
    McpRegistryDiscoverer,
    McpRegistryEntry,
    McpRegistryPage,
)

# ---------------------------------------------------------------------------
# Fakes / fixtures (no live network)
# ---------------------------------------------------------------------------


_DEFAULT_PAGE = McpRegistryPage(
    entries=(
        McpRegistryEntry(
            name="example-tool",
            description="An example MCP tool",
            publisher="example-org",
            version="1.0.0",
            url="https://registry.example.com/tools/example-tool",
            license="MIT",
        ),
    ),
    total=1,
    page=1,
    page_size=50,
    has_next=False,
)


class FakeMcpRegistryClient(McpRegistryClient):
    def __init__(
        self,
        pages: list[McpRegistryPage] | None = None,
        *,
        fail_on_page: int | None = None,
        fail_on_fetch: str | None = None,
    ) -> None:
        self._pages = list(pages) if pages is not None else [_DEFAULT_PAGE]
        self._fail_on_page = fail_on_page
        self._fail_on_fetch = fail_on_fetch

    def fetch_page(self, *, page: int, page_size: int) -> McpRegistryPage:
        if self._fail_on_page is not None and page == self._fail_on_page:
            raise ConnectionError("registry unreachable")
        idx = page - 1
        if idx >= len(self._pages):
            return McpRegistryPage(entries=(), total=0, page=page, page_size=page_size, has_next=False)
        return self._pages[idx]

    def fetch_raw(self, url: str) -> bytes:
        if self._fail_on_fetch is not None and self._fail_on_fetch in url:
            raise ConnectionError("fetch failed")
        return b'{"name": "example-tool", "version": "1.0.0"}'


class FakeGitClient(GitClient):
    def __init__(
        self,
        *,
        fail: bool = False,
        content: bytes | None = None,
    ) -> None:
        self._fail = fail
        self._content = content or b"git-archive-content"

    def clone_archive(self, git_ref: GitRef) -> Any:
        if self._fail:
            raise ConnectionError("git clone failed")
        from zhiwei.capabilities.catalog.git import GitCloneResult

        return GitCloneResult(
            content=self._content,
            commit_sha="abc123def456",
            ref=git_ref.ref,
        )


class FakeUrlClient(UrlClient):
    def __init__(
        self,
        *,
        fail: bool = False,
        content: bytes | None = None,
        content_type: str = "application/zip",
    ) -> None:
        self._fail = fail
        self._content = content or b"zip-content-bytes"
        self._content_type = content_type

    def fetch(self, request: ImportRequest) -> FetchResult:
        if self._fail:
            raise ConnectionError("url fetch failed")
        return FetchResult(
            content=self._content,
            content_type=self._content_type,
            etag='"abc123"',
            last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
        )


# ---------------------------------------------------------------------------
# CatalogEntry tests
# ---------------------------------------------------------------------------


class TestCatalogEntry:
    def test_entry_creation(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.MCP_REGISTRY,
            source_url="https://example.com/tool",
            name="my-tool",
            version="1.0.0",
        )
        assert entry.source_type == SourceType.MCP_REGISTRY
        assert entry.name == "my-tool"
        assert entry.source_url == "https://example.com/tool"

    def test_entry_digest_deterministic(self) -> None:
        a = CatalogEntry(
            source_type=SourceType.MCP_REGISTRY,
            source_url="https://example.com/tool",
            name="my-tool",
            version="1.0.0",
        )
        b = CatalogEntry(
            source_type=SourceType.MCP_REGISTRY,
            source_url="https://example.com/tool",
            name="my-tool",
            version="1.0.0",
        )
        assert a.entry_digest == b.entry_digest

    def test_entry_digest_changes_with_content(self) -> None:
        a = CatalogEntry(
            source_type=SourceType.MCP_REGISTRY,
            source_url="https://example.com/tool",
            name="my-tool",
            version="1.0.0",
        )
        b = CatalogEntry(
            source_type=SourceType.MCP_REGISTRY,
            source_url="https://example.com/tool",
            name="my-tool",
            version="2.0.0",
        )
        assert a.entry_digest != b.entry_digest

    def test_entry_rejects_empty_source_url(self) -> None:
        with pytest.raises(ValueError, match="source_url"):
            CatalogEntry(source_type=SourceType.URL, source_url="", name="x")

    def test_entry_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            CatalogEntry(source_type=SourceType.URL, source_url="https://x.com", name="")

    def test_entry_is_frozen(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.GIT,
            source_url="https://github.com/org/repo",
            name="repo",
        )
        with pytest.raises(Exception, match="frozen"):
            entry.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# QuarantineRecord tests
# ---------------------------------------------------------------------------


class TestQuarantineRecord:
    def test_quarantine_computes_digest(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.URL,
            source_url="https://example.com/archive.zip",
            name="my-archive",
        )
        record = QuarantineRecord(entry=entry)
        assert record.quarantine_digest.startswith("sha256:")

    def test_quarantine_digest_deterministic(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.URL,
            source_url="https://example.com/archive.zip",
            name="my-archive",
        )
        r1 = QuarantineRecord(entry=entry)
        r2 = QuarantineRecord(entry=entry)
        assert r1.quarantine_digest == r2.quarantine_digest

    def test_quarantine_is_frozen(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.URL,
            source_url="https://example.com/archive.zip",
            name="my-archive",
        )
        record = QuarantineRecord(entry=entry)
        with pytest.raises(Exception, match="frozen"):
            record.id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# McpRegistryDiscoverer tests
# ---------------------------------------------------------------------------


class TestMcpRegistryDiscoverer:
    def test_discover_single_page(self) -> None:
        client = FakeMcpRegistryClient()
        discoverer = McpRegistryDiscoverer(client)
        entries = discoverer.discover(page=1, page_size=50)
        assert len(entries) == 1
        assert entries[0].name == "example-tool"
        assert entries[0].source_type == SourceType.MCP_REGISTRY

    def test_discover_pagination(self) -> None:
        page1 = McpRegistryPage(
            entries=(
                McpRegistryEntry(name="tool-a", url="https://a.example.com", publisher="a"),
                McpRegistryEntry(name="tool-b", url="https://b.example.com", publisher="b"),
            ),
            total=3,
            page=1,
            page_size=2,
            has_next=True,
        )
        page2 = McpRegistryPage(
            entries=(
                McpRegistryEntry(name="tool-c", url="https://c.example.com", publisher="c"),
            ),
            total=3,
            page=2,
            page_size=2,
            has_next=False,
        )
        client = FakeMcpRegistryClient(pages=[page1, page2])
        discoverer = McpRegistryDiscoverer(client)

        entries_p1 = discoverer.discover(page=1, page_size=2)
        assert len(entries_p1) == 2
        assert entries_p1[0].name == "tool-a"
        assert entries_p1[1].name == "tool-b"

        entries_p2 = discoverer.discover(page=2, page_size=2)
        assert len(entries_p2) == 1
        assert entries_p2[0].name == "tool-c"

    def test_discover_empty_page(self) -> None:
        client = FakeMcpRegistryClient(pages=[])
        discoverer = McpRegistryDiscoverer(client)
        entries = discoverer.discover(page=1, page_size=50)
        assert entries == []

    def test_discover_outage_raises(self) -> None:
        client = FakeMcpRegistryClient(fail_on_page=1)
        discoverer = McpRegistryDiscoverer(client)
        with pytest.raises(CatalogOutageError, match="MCP Registry unavailable"):
            discoverer.discover(page=1, page_size=50)

    def test_fetch_places_into_quarantine(self) -> None:
        client = FakeMcpRegistryClient()
        discoverer = McpRegistryDiscoverer(client)
        entries = discoverer.discover()
        record = discoverer.fetch(entries[0])

        assert isinstance(record, QuarantineRecord)
        assert record.entry.content_digest.startswith("sha256:")
        assert record.entry.size_bytes > 0
        assert record.quarantine_digest.startswith("sha256:")

    def test_fetch_outage_raises(self) -> None:
        client = FakeMcpRegistryClient(fail_on_fetch="example-tool")
        discoverer = McpRegistryDiscoverer(client)
        entry = CatalogEntry(
            source_type=SourceType.MCP_REGISTRY,
            source_url="https://registry.example.com/tools/example-tool",
            name="example-tool",
        )
        with pytest.raises(CatalogOutageError, match="Failed to fetch"):
            discoverer.fetch(entry)

    def test_registry_entry_preserves_publisher(self) -> None:
        client = FakeMcpRegistryClient()
        discoverer = McpRegistryDiscoverer(client)
        entries = discoverer.discover()
        assert entries[0].publisher == "example-org"

    def test_registry_entry_preserves_license(self) -> None:
        client = FakeMcpRegistryClient()
        discoverer = McpRegistryDiscoverer(client)
        entries = discoverer.discover()
        assert entries[0].license == "MIT"


# ---------------------------------------------------------------------------
# GitImporter tests
# ---------------------------------------------------------------------------


class TestGitImporter:
    def test_discover_not_supported(self) -> None:
        client = FakeGitClient()
        importer = GitImporter(client)
        with pytest.raises(CatalogError, match="do not support enumeration"):
            importer.discover()

    def test_fetch_places_into_quarantine(self) -> None:
        client = FakeGitClient()
        importer = GitImporter(client)
        entry = CatalogEntry(
            source_type=SourceType.GIT,
            source_url="https://github.com/org/repo",
            name="my-repo",
            version="v1.0.0",
        )
        record = importer.fetch(entry)

        assert isinstance(record, QuarantineRecord)
        assert record.entry.content_digest.startswith("sha256:")
        assert record.entry.size_bytes == len(b"git-archive-content")
        assert record.entry.raw_metadata["commit_sha"] == "abc123def456"

    def test_fetch_outage_on_clone_failure(self) -> None:
        client = FakeGitClient(fail=True)
        importer = GitImporter(client)
        entry = CatalogEntry(
            source_type=SourceType.GIT,
            source_url="https://github.com/org/repo",
            name="my-repo",
        )
        with pytest.raises(CatalogOutageError, match="Git clone failed"):
            importer.fetch(entry)

    def test_fetch_custom_content(self) -> None:
        custom = b"custom-git-archive"
        client = FakeGitClient(content=custom)
        importer = GitImporter(client)
        entry = CatalogEntry(
            source_type=SourceType.GIT,
            source_url="https://github.com/org/repo",
            name="my-repo",
        )
        record = importer.fetch(entry)
        assert record.entry.size_bytes == len(custom)


# ---------------------------------------------------------------------------
# UrlImporter tests
# ---------------------------------------------------------------------------


class TestUrlImporter:
    def test_discover_not_supported(self) -> None:
        client = FakeUrlClient()
        importer = UrlImporter(client)
        with pytest.raises(CatalogError, match="do not support enumeration"):
            importer.discover()

    def test_fetch_places_into_quarantine(self) -> None:
        client = FakeUrlClient()
        importer = UrlImporter(client)
        entry = CatalogEntry(
            source_type=SourceType.URL,
            source_url="https://example.com/capability.zip",
            name="my-capability",
        )
        record = importer.fetch(entry)

        assert isinstance(record, QuarantineRecord)
        assert record.entry.content_digest.startswith("sha256:")
        assert record.entry.size_bytes == len(b"zip-content-bytes")
        assert record.entry.raw_metadata["etag"] == '"abc123"'
        assert record.entry.raw_metadata["last_modified"] == "Mon, 01 Jan 2026 00:00:00 GMT"

    def test_fetch_outage_on_url_failure(self) -> None:
        client = FakeUrlClient(fail=True)
        importer = UrlImporter(client)
        entry = CatalogEntry(
            source_type=SourceType.URL,
            source_url="https://example.com/capability.zip",
            name="my-capability",
        )
        with pytest.raises(CatalogOutageError, match="URL fetch failed"):
            importer.fetch(entry)

    def test_fetch_no_etag(self) -> None:
        class NoEtagClient(UrlClient):
            def fetch(self, request: ImportRequest) -> FetchResult:
                return FetchResult(content=b"data", content_type="application/zip")

        importer = UrlImporter(NoEtagClient())
        entry = CatalogEntry(
            source_type=SourceType.URL,
            source_url="https://example.com/file.zip",
            name="file",
        )
        record = importer.fetch(entry)
        assert "etag" not in record.entry.raw_metadata


# ---------------------------------------------------------------------------
# No-credentials / no-execution invariant
# ---------------------------------------------------------------------------


class TestQuarantineInvariants:
    def test_quarantine_record_has_no_credentials(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.MCP_REGISTRY,
            source_url="https://example.com/tool",
            name="tool",
        )
        record = QuarantineRecord(entry=entry)
        dumped = record.model_dump()
        for key in dumped:
            assert "credential" not in key.lower()
            assert "secret" not in key.lower()
            assert "token" not in key.lower()
            assert "key" not in key.lower()

    def test_quarantine_record_has_no_execution_flag(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.URL,
            source_url="https://example.com/tool.zip",
            name="tool",
        )
        record = QuarantineRecord(entry=entry)
        dumped = record.model_dump()
        for key in dumped:
            assert "exec" not in key.lower()
            assert "run" not in key.lower()

    def test_catalog_entry_records_source_metadata(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.MCP_REGISTRY,
            source_url="https://example.com/tool",
            name="tool",
            publisher="acme-corp",
            version="1.0.0",
        )
        assert entry.publisher == "acme-corp"
        assert entry.version == "1.0.0"
        assert entry.fetched_at is not None

    def test_quarantine_record_preserves_full_audit_trail(self) -> None:
        entry = CatalogEntry(
            source_type=SourceType.GIT,
            source_url="https://github.com/org/repo",
            name="repo",
            publisher="org",
        )
        record = QuarantineRecord(entry=entry)
        assert record.entry.source_url == "https://github.com/org/repo"
        assert record.entry.publisher == "org"
        assert record.quarantined_at is not None
        assert record.quarantine_digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# Abstract interface contract
# ---------------------------------------------------------------------------


class TestCatalogSourceInterface:
    def test_mcp_registry_is_catalog_source(self) -> None:
        assert issubclass(McpRegistryDiscoverer, CatalogSource)

    def test_git_importer_is_catalog_source(self) -> None:
        assert issubclass(GitImporter, CatalogSource)

    def test_url_importer_is_catalog_source(self) -> None:
        assert issubclass(UrlImporter, CatalogSource)
