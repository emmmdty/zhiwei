"""Git URL import for capability discovery and quarantine."""

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


class GitRef(BaseModel):
    """Identifies a specific Git reference (branch, tag, or commit)."""

    model_config = {"frozen": True}

    url: str
    ref: str = "main"

    @model_validator(mode="after")
    def _validate_url_not_empty(self) -> GitRef:
        if not self.url:
            raise ValueError("url must not be empty")
        return self


class GitCloneResult(BaseModel):
    """Result of a Git clone/archive operation."""

    model_config = {"frozen": True}

    content: bytes
    commit_sha: str = ""
    ref: str = "main"

    @model_validator(mode="after")
    def _validate_content_not_empty(self) -> GitCloneResult:
        if not self.content:
            raise ValueError("content must not be empty")
        return self


class GitClient:
    """Port for Git operations. Implementations provide the actual transport."""

    def clone_archive(self, git_ref: GitRef) -> GitCloneResult:
        raise NotImplementedError("subclasses must implement clone_archive")


class GitImporter(CatalogSource):
    """Import capabilities from Git repositories."""

    def __init__(self, client: GitClient) -> None:
        self._client = client

    def discover(self, *, page: int = 1, page_size: int = 50) -> list[CatalogEntry]:
        raise CatalogError(
            "Git sources do not support enumeration; use fetch with a known URL"
        )

    def fetch(self, entry: CatalogEntry) -> QuarantineRecord:
        git_ref = GitRef(url=entry.source_url, ref=entry.version or "main")
        try:
            result = self._client.clone_archive(git_ref)
        except Exception as exc:
            raise CatalogOutageError(
                f"Git clone failed for {entry.source_url}: {exc}"
            ) from exc

        content_digest = digest_bytes(result.content)
        metadata = dict(entry.raw_metadata)
        metadata["commit_sha"] = result.commit_sha

        updated_entry = entry.model_copy(
            update={
                "content_digest": content_digest,
                "size_bytes": len(result.content),
                "raw_metadata": metadata,
            }
        )
        return QuarantineRecord(entry=updated_entry)
