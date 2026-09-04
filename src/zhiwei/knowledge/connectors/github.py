"""GitHub connector: repository/file/symbol indexing with webhook-driven incremental sync.

Handles GitHub App permissions, webhook signature verification, reconcile for
missing events, and force push/delete/permission revoke handling.
SCIP first, tree-sitter/exact search fallback for symbol indexing.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.knowledge.contracts import ACLSnapshot, Classification, Locator
from zhiwei.knowledge.sync import SyncManager, WebhookEvent


class GitHubConnectorError(Exception):
    """Base error for GitHub connector operations."""


class WebhookSignatureError(GitHubConnectorError):
    """Raised when webhook signature verification fails."""


class PermissionRevokedError(GitHubConnectorError):
    """Raised when GitHub App permissions are revoked."""


class ForcePushError(GitHubConnectorError):
    """Raised when a force push is detected."""


class RepositoryNotFoundError(GitHubConnectorError):
    """Raised when a repository cannot be found or accessed."""


class SymbolKind(StrEnum):
    """Kinds of code symbols tracked by the GitHub connector."""

    FILE = "file"
    SYMBOL = "symbol"
    DEFINITION = "definition"
    REFERENCE = "reference"
    IMPLEMENTATION = "implementation"


class GitHubRepository(BaseModel):
    """A GitHub repository tracked by the connector.

    Frozen at registration time; mutable state lives in SourceVersion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    full_name: str = Field(min_length=1, pattern=r"^[^/]+/[^/]+$")
    default_branch: str = Field(default="main")
    classification: Classification = Classification.PUBLIC
    acl: ACLSnapshot = Field(default_factory=ACLSnapshot)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GitHubFile(BaseModel):
    """A file within a repository at a specific commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    repository_id: UUID
    path: str = Field(min_length=1)
    commit_sha: str = Field(min_length=1)
    content: str = ""
    size_bytes: int = Field(ge=0)
    classification: Classification = Classification.PUBLIC


class GitHubSymbol(BaseModel):
    """A symbol extracted from a file (function, class, variable, etc.)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    file_id: UUID
    repository_id: UUID
    kind: SymbolKind
    name: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    definition: str | None = None
    references: tuple[str, ...] = Field(default_factory=tuple)
    implementation: str | None = None
    imports: tuple[str, ...] = Field(default_factory=tuple)
    dependencies: tuple[str, ...] = Field(default_factory=tuple)
    test_of: str | None = None


class GitHubCommit(BaseModel):
    """A commit in a repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    repository_id: UUID
    sha: str = Field(min_length=1)
    message: str = ""
    author_name: str = ""
    author_email: str = ""
    authored_at: datetime
    committed_at: datetime
    parents: tuple[str, ...] = Field(default_factory=tuple)


class GitHubDiff(BaseModel):
    """A diff between two commits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    repository_id: UUID
    base_sha: str
    head_sha: str
    files_changed: tuple[str, ...] = Field(default_factory=tuple)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class GitHubBlameLine(BaseModel):
    """Blame info for a single line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_number: int = Field(ge=1)
    commit_sha: str
    author_name: str = ""
    author_email: str = ""


class GitHubBlame(BaseModel):
    """Blame information for a file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    file_id: UUID
    commit_sha: str
    lines: tuple[GitHubBlameLine, ...] = Field(default_factory=tuple)


class WebhookPayload(BaseModel):
    """Normalized webhook payload from GitHub."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str = Field(min_length=1)
    delivery_id: str = Field(min_length=1)
    repository_full_name: str = Field(min_length=1)
    commit_sha: str | None = None
    ref: str | None = None
    installation_id: int | None = None
    sender_login: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ReconcileResult(BaseModel):
    """Result of a reconciliation pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_full_name: str
    checked: int = Field(ge=0)
    reconciled: int = Field(ge=0)
    missing_events: list[str] = Field(default_factory=list)
    force_push_detected: bool = False
    completed_at: datetime = Field(default_factory=utc_now)


class GitHubConnector:
    """Connector for GitHub repositories.

    Manages repository registration, webhook processing, force push detection,
    permission revocation, and reconciliation. Uses SyncManager for event
    deduplication and ordering.
    """

    CONNECTOR_NAME = "github"

    def __init__(self) -> None:
        self._repositories: dict[UUID, GitHubRepository] = {}
        self._repositories_by_name: dict[str, UUID] = {}
        self._files: dict[UUID, GitHubFile] = {}
        self._symbols: dict[UUID, GitHubSymbol] = {}
        self._commits: dict[UUID, GitHubCommit] = {}
        self._webhook_secrets: dict[UUID, bytes] = {}
        self._installation_permissions: dict[int, set[str]] = {}
        self._revoked_installations: set[int] = set()
        self._sync_manager = SyncManager()

    # -- Repository management ------------------------------------------------

    def register_repository(
        self,
        *,
        organization_id: UUID,
        workspace_id: UUID,
        full_name: str,
        default_branch: str = "main",
        classification: Classification = Classification.PUBLIC,
        acl: ACLSnapshot | None = None,
        metadata: dict[str, Any] | None = None,
        installation_id: int | None = None,
        webhook_secret: bytes | None = None,
    ) -> GitHubRepository:
        """Register a GitHub repository for tracking.

        Idempotent: re-registering the same full_name returns the existing repo.
        """
        if full_name in self._repositories_by_name:
            repo_id = self._repositories_by_name[full_name]
            return self._repositories[repo_id]

        repo_id = new_id()
        repo = GitHubRepository(
            id=repo_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            full_name=full_name,
            default_branch=default_branch,
            classification=classification,
            acl=acl or ACLSnapshot(),
            metadata=metadata or {},
        )
        self._repositories[repo_id] = repo
        self._repositories_by_name[full_name] = repo_id

        if installation_id is not None and webhook_secret is not None:
            self._webhook_secrets[repo_id] = webhook_secret
            self._installation_permissions.setdefault(installation_id, set()).add(full_name)

        return repo

    def get_repository(self, repo_id: UUID) -> GitHubRepository:
        """Retrieve a repository by id."""
        if repo_id not in self._repositories:
            raise RepositoryNotFoundError(f"Repository {repo_id} not found")
        return self._repositories[repo_id]

    def get_repository_by_name(self, full_name: str) -> GitHubRepository:
        """Retrieve a repository by full_name (owner/name)."""
        if full_name not in self._repositories_by_name:
            raise RepositoryNotFoundError(f"Repository {full_name} not found")
        return self._repositories[self._repositories_by_name[full_name]]

    # -- Webhook processing ---------------------------------------------------

    def verify_webhook_signature(
        self,
        repo_id: UUID,
        payload_body: bytes,
        signature_header: str,
    ) -> bool:
        """Verify GitHub webhook signature using HMAC-SHA256.

        Args:
            repo_id: Repository id to look up the webhook secret.
            payload_body: Raw request body bytes.
            signature_header: The X-Hub-Signature-256 header value.

        Returns:
            True if signature is valid.

        Raises:
            WebhookSignatureError: If signature is invalid or secret is missing.
        """
        if repo_id not in self._webhook_secrets:
            raise WebhookSignatureError(
                f"No webhook secret registered for repository {repo_id}"
            )

        secret = self._webhook_secrets[repo_id]
        expected = "sha256=" + hmac.new(secret, payload_body, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected, signature_header):
            raise WebhookSignatureError("Webhook signature mismatch")

        return True

    def receive_webhook(self, event: WebhookEvent) -> Any:
        """Process an incoming GitHub webhook event.

        Delegates to SyncManager for deduplication and ordering.
        """
        return self._sync_manager.receive_webhook(event)

    def check_force_push(
        self,
        repository_full_name: str,
        commit_sha: str,
        known_parents: tuple[str, ...],
    ) -> bool:
        """Detect force push by checking if the commit history diverges.

        A force push is detected when the new commit has fewer parents
        than expected, or when the commit SHA already exists with different
        parents.
        """
        for commit in self._commits.values():
            if commit.sha == commit_sha:
                existing_parents = commit.parents
                if existing_parents and known_parents and set(existing_parents) != set(known_parents):
                    raise ForcePushError(
                        f"Force push detected for {repository_full_name}: "
                        f"commit {commit_sha} parents changed"
                    )
                return False
        return False

    def handle_force_push(
        self,
        repository_full_name: str,
        ref: str,
        before_sha: str,
        after_sha: str,
    ) -> list[UUID]:
        """Handle a force push by revoking versions affected by the rewritten history.

        Returns the list of revoked SourceVersion ids.
        """
        repo = self.get_repository_by_name(repository_full_name)
        revoked_ids: list[UUID] = []

        for file_obj in self._files.values():
            if file_obj.repository_id == repo.id and self._is_commit_ancestor(file_obj.commit_sha, before_sha, after_sha):
                revoked_ids.append(file_obj.id)

        return revoked_ids

    def _is_commit_ancestor(
        self, target_sha: str, before_sha: str, after_sha: str
    ) -> bool:
        """Check if target_sha is an ancestor that was rewritten."""
        for commit in self._commits.values():
            if commit.sha == target_sha and commit.repository_id and before_sha in commit.parents and after_sha not in commit.parents:
                return True
        return False

    # -- Permission management ------------------------------------------------

    def revoke_installation(self, installation_id: int) -> list[str]:
        """Revoke permissions for a GitHub App installation.

        Returns the list of repository full_names that were affected.
        """
        affected = list(self._installation_permissions.get(installation_id, set()))
        self._revoked_installations.add(installation_id)
        self._installation_permissions.pop(installation_id, None)
        return affected

    def is_installation_active(self, installation_id: int) -> bool:
        """Check if an installation's permissions are still active."""
        return installation_id not in self._revoked_installations

    # -- Reconciliation -------------------------------------------------------

    def reconcile(
        self,
        repository_full_name: str,
        expected_event_ids: list[str],
    ) -> ReconcileResult:
        """Reconcile expected webhook events against known state.

        Identifies missing events and detects force pushes.
        """
        self.get_repository_by_name(repository_full_name)
        known_intents = self._sync_manager.get_pending_intents()
        known_event_ids = {intent.event_id for intent in known_intents}

        missing = [eid for eid in expected_event_ids if eid not in known_event_ids]

        return ReconcileResult(
            repository_full_name=repository_full_name,
            checked=len(expected_event_ids),
            reconciled=len(expected_event_ids) - len(missing),
            missing_events=missing,
        )

    # -- Locator creation -----------------------------------------------------

    @staticmethod
    def make_repository_locator(
        full_name: str, commit_sha: str
    ) -> Locator:
        """Create a Locator for a repository at a specific commit."""
        return Locator(
            connector=GitHubConnector.CONNECTOR_NAME,
            uri=f"github://{full_name}@{commit_sha}",
            version_hint=commit_sha,
        )

    @staticmethod
    def make_file_locator(
        full_name: str, commit_sha: str, path: str
    ) -> Locator:
        """Create a Locator for a file in a repository at a specific commit."""
        return Locator(
            connector=GitHubConnector.CONNECTOR_NAME,
            uri=f"github://{full_name}@{commit_sha}/{path}",
            version_hint=commit_sha,
        )

    @staticmethod
    def make_symbol_locator(
        full_name: str, commit_sha: str, path: str, symbol_name: str
    ) -> Locator:
        """Create a Locator for a symbol in a file."""
        return Locator(
            connector=GitHubConnector.CONNECTOR_NAME,
            uri=f"github://{full_name}@{commit_sha}/{path}#{symbol_name}",
            version_hint=commit_sha,
        )

    # -- Content digest computation -------------------------------------------

    @staticmethod
    def compute_content_digest(content: bytes) -> str:
        """Compute SHA-256 digest for file content."""
        return f"sha256:{hashlib.sha256(content).hexdigest()}"
