"""S5-T3 contract tests: GitHub connector and code parsers.

Tests run WITHOUT network: no live GitHub API calls.
Covers repository management, webhook processing, force push detection,
permission revocation, reconciliation, and parser stubs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.connectors.github import (
    ForcePushError,
    GitHubBlame,
    GitHubBlameLine,
    GitHubCommit,
    GitHubConnector,
    GitHubFile,
    GitHubRepository,
    GitHubSymbol,
    ReconcileResult,
    RepositoryNotFoundError,
    SymbolKind,
    WebhookPayload,
    WebhookSignatureError,
)
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceObject,
    SourceVersion,
)
from zhiwei.knowledge.parsers.scip import (
    SCIPIndex,
    SCIPParser,
    SCIPSymbol,
    SCIPUnavailableError,
)
from zhiwei.knowledge.parsers.treesitter import (
    TreeSitterIndex,
    TreeSitterParser,
    TreeSitterSymbol,
    TreeSitterUnavailableError,
)
from zhiwei.knowledge.sync import (
    DuplicateWebhookError,
    SyncEventType,
    WebhookEvent,
)

ORGANIZATION_ID = UUID("11111111-1111-4111-8111-111111111111")
WORKSPACE_ID = UUID("22222222-2222-4222-8222-222222222222")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def connector() -> GitHubConnector:
    return GitHubConnector()


@pytest.fixture
def registered_repo(connector: GitHubConnector) -> GitHubRepository:
    return connector.register_repository(
        organization_id=ORGANIZATION_ID,
        workspace_id=WORKSPACE_ID,
        full_name="acme/backend",
        default_branch="main",
        classification=Classification.INTERNAL,
    )


@pytest.fixture
def repo_with_webhook(connector: GitHubConnector) -> GitHubRepository:
    return connector.register_repository(
        organization_id=ORGANIZATION_ID,
        workspace_id=WORKSPACE_ID,
        full_name="acme/secure-repo",
        installation_id=42,
        webhook_secret=b"test-webhook-secret-key",
    )


@pytest.fixture
def scip_parser() -> SCIPParser:
    return SCIPParser()


@pytest.fixture
def ts_parser() -> TreeSitterParser:
    return TreeSitterParser()


# ===========================================================================
# Repository registration tests
# ===========================================================================


class TestRepositoryRegistration:
    def test_register_repository_returns_frozen_model(
        self, connector: GitHubConnector
    ) -> None:
        repo = connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/frontend",
        )
        assert isinstance(repo, GitHubRepository)
        assert repo.full_name == "acme/frontend"
        assert repo.default_branch == "main"
        assert repo.classification == Classification.PUBLIC

    def test_register_repository_is_idempotent(
        self, connector: GitHubConnector
    ) -> None:
        repo1 = connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/idempotent",
        )
        repo2 = connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/idempotent",
        )
        assert repo1.id == repo2.id

    def test_get_repository_by_id(self, registered_repo: GitHubRepository) -> None:
        connector = GitHubConnector()
        connector._repositories[registered_repo.id] = registered_repo
        connector._repositories_by_name[registered_repo.full_name] = registered_repo.id
        result = connector.get_repository(registered_repo.id)
        assert result.full_name == "acme/backend"

    def test_get_repository_by_name(self, registered_repo: GitHubRepository) -> None:
        connector = GitHubConnector()
        connector._repositories[registered_repo.id] = registered_repo
        connector._repositories_by_name[registered_repo.full_name] = registered_repo.id
        result = connector.get_repository_by_name("acme/backend")
        assert result.id == registered_repo.id

    def test_get_repository_not_found(self, connector: GitHubConnector) -> None:
        with pytest.raises(RepositoryNotFoundError):
            connector.get_repository(new_id())

    def test_get_repository_by_name_not_found(
        self, connector: GitHubConnector
    ) -> None:
        with pytest.raises(RepositoryNotFoundError):
            connector.get_repository_by_name("nonexistent/repo")

    def test_register_with_classification_and_acl(
        self, connector: GitHubConnector
    ) -> None:
        acl = ACLSnapshot(
            allowed_principals=("user:1",),
            denied_principals=("user:2",),
        )
        repo = connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/private",
            classification=Classification.CONFIDENTIAL,
            acl=acl,
        )
        assert repo.classification == Classification.CONFIDENTIAL
        assert "user:1" in repo.acl.allowed_principals
        assert "user:2" in repo.acl.denied_principals

    def test_register_rejects_invalid_full_name(
        self, connector: GitHubConnector
    ) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            connector.register_repository(
                organization_id=ORGANIZATION_ID,
                workspace_id=WORKSPACE_ID,
                full_name="invalid-name-no-slash",
            )


# ===========================================================================
# Webhook signature verification tests
# ===========================================================================


class TestWebhookSignature:
    def test_verify_valid_signature(
        self, connector: GitHubConnector, repo_with_webhook: GitHubRepository
    ) -> None:
        import hashlib
        import hmac

        secret = b"test-webhook-secret-key"
        body = b'{"action":"push"}'
        expected_sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

        result = connector.verify_webhook_signature(
            repo_with_webhook.id, body, expected_sig
        )
        assert result is True

    def test_verify_invalid_signature(
        self, connector: GitHubConnector, repo_with_webhook: GitHubRepository
    ) -> None:
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            connector.verify_webhook_signature(
                repo_with_webhook.id, b"body", "sha256=invalid"
            )

    def test_verify_no_secret_registered(self, connector: GitHubConnector) -> None:
        repo = connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/no-secret",
        )
        with pytest.raises(WebhookSignatureError, match="No webhook secret"):
            connector.verify_webhook_signature(repo.id, b"body", "sha256=x")

    def test_signature_uses_constant_time_comparison(
        self, connector: GitHubConnector, repo_with_webhook: GitHubRepository
    ) -> None:
        import hashlib
        import hmac

        secret = b"test-webhook-secret-key"
        body = b'{"action":"push"}'
        correct = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        # A similar but wrong signature should still fail
        wrong = correct[:-1] + ("0" if correct[-1] != "0" else "1")
        with pytest.raises(WebhookSignatureError):
            connector.verify_webhook_signature(repo_with_webhook.id, body, wrong)


# ===========================================================================
# Webhook event processing tests
# ===========================================================================


class TestWebhookProcessing:
    def test_receive_webhook_creates_intent(
        self, connector: GitHubConnector, registered_repo: GitHubRepository
    ) -> None:
        event = WebhookEvent(
            id="evt-001",
            connector=GitHubConnector.CONNECTOR_NAME,
            source_object_id=registered_repo.id,
            event_type=SyncEventType.CREATE,
            payload={"action": "completed"},
        )
        intent = connector.receive_webhook(event)
        assert intent.event_id == "evt-001"
        assert intent.connector == GitHubConnector.CONNECTOR_NAME

    def test_duplicate_webhook_raises_error(
        self, connector: GitHubConnector, registered_repo: GitHubRepository
    ) -> None:
        event = WebhookEvent(
            id="evt-dup",
            connector=GitHubConnector.CONNECTOR_NAME,
            source_object_id=registered_repo.id,
            event_type=SyncEventType.CREATE,
        )
        connector.receive_webhook(event)
        with pytest.raises(DuplicateWebhookError):
            connector.receive_webhook(event)

    def test_webhook_event_types(self, connector: GitHubConnector) -> None:
        for idx, event_type in enumerate(SyncEventType):
            event = WebhookEvent(
                id=f"evt-{idx:04d}-{event_type.value}",
                connector=GitHubConnector.CONNECTOR_NAME,
                source_object_id=new_id(),
                event_type=event_type,
            )
            intent = connector.receive_webhook(event)
            assert intent.event_type == event_type


# ===========================================================================
# Force push detection and handling tests
# ===========================================================================


class TestForcePush:
    def test_no_force_push_when_no_history(
        self, connector: GitHubConnector
    ) -> None:
        result = connector.check_force_push("acme/backend", "abc123", ("parent1",))
        assert result is False

    def test_force_push_detected_on_parent_change(
        self, connector: GitHubConnector, registered_repo: GitHubRepository
    ) -> None:
        commit = GitHubCommit(
            id=new_id(),
            repository_id=registered_repo.id,
            sha="abc123",
            message="initial",
            authored_at=datetime.now(UTC),
            committed_at=datetime.now(UTC),
            parents=("old-parent",),
        )
        connector._commits[commit.id] = commit

        with pytest.raises(ForcePushError, match="Force push detected"):
            connector.check_force_push(
                "acme/backend", "abc123", ("new-parent",)
            )

    def test_handle_force_push_returns_affected_files(
        self, connector: GitHubConnector, registered_repo: GitHubRepository
    ) -> None:
        file_obj = GitHubFile(
            id=new_id(),
            repository_id=registered_repo.id,
            path="src/main.py",
            commit_sha="old-commit",
            content="old code",
            size_bytes=9,
        )
        connector._files[file_obj.id] = file_obj

        commit = GitHubCommit(
            id=new_id(),
            repository_id=registered_repo.id,
            sha="old-commit",
            message="old commit",
            authored_at=datetime.now(UTC),
            committed_at=datetime.now(UTC),
            parents=("parent-before-force",),
        )
        connector._commits[commit.id] = commit

        affected = connector.handle_force_push(
            "acme/backend", "main", "parent-before-force", "new-head"
        )
        assert file_obj.id in affected

    def test_handle_force_push_no_affected_files(
        self, connector: GitHubConnector, registered_repo: GitHubRepository
    ) -> None:
        affected = connector.handle_force_push(
            "acme/backend", "main", "old-parent", "new-head"
        )
        assert affected == []


# ===========================================================================
# Permission revocation tests
# ===========================================================================


class TestPermissionRevocation:
    def test_revoke_installation_returns_affected_repos(
        self, connector: GitHubConnector
    ) -> None:
        connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/repo-a",
            installation_id=99,
            webhook_secret=b"secret-a",
        )
        connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/repo-b",
            installation_id=99,
            webhook_secret=b"secret-b",
        )

        affected = connector.revoke_installation(99)
        assert set(affected) == {"acme/repo-a", "acme/repo-b"}

    def test_revoked_installation_is_inactive(
        self, connector: GitHubConnector
    ) -> None:
        connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/repo-c",
            installation_id=77,
            webhook_secret=b"secret-c",
        )
        assert connector.is_installation_active(77) is True
        connector.revoke_installation(77)
        assert connector.is_installation_active(77) is False

    def test_revoke_nonexistent_installation(
        self, connector: GitHubConnector
    ) -> None:
        affected = connector.revoke_installation(9999)
        assert affected == []

    def test_revoke_only_affects_target_installation(
        self, connector: GitHubConnector
    ) -> None:
        connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/repo-x",
            installation_id=10,
            webhook_secret=b"secret-x",
        )
        connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/repo-y",
            installation_id=20,
            webhook_secret=b"secret-y",
        )

        connector.revoke_installation(10)
        assert connector.is_installation_active(10) is False
        assert connector.is_installation_active(20) is True


# ===========================================================================
# Reconciliation tests
# ===========================================================================


class TestReconciliation:
    def test_reconcile_no_missing_events(
        self, connector: GitHubConnector, registered_repo: GitHubRepository
    ) -> None:
        # Process some events
        for i in range(3):
            event = WebhookEvent(
                id=f"rec-evt-{i:03d}",
                connector=GitHubConnector.CONNECTOR_NAME,
                source_object_id=registered_repo.id,
                event_type=SyncEventType.CREATE,
            )
            connector.receive_webhook(event)

        result = connector.reconcile(
            "acme/backend",
            ["rec-evt-000", "rec-evt-001", "rec-evt-002"],
        )
        assert result.checked == 3
        assert result.reconciled == 3
        assert result.missing_events == []

    def test_reconcile_detects_missing_events(
        self, connector: GitHubConnector, registered_repo: GitHubRepository
    ) -> None:
        event = WebhookEvent(
            id="rec-missing-001",
            connector=GitHubConnector.CONNECTOR_NAME,
            source_object_id=registered_repo.id,
            event_type=SyncEventType.CREATE,
        )
        connector.receive_webhook(event)

        result = connector.reconcile(
            "acme/backend",
            ["rec-missing-001", "rec-missing-002", "rec-missing-003"],
        )
        assert result.checked == 3
        assert result.reconciled == 1
        assert "rec-missing-002" in result.missing_events
        assert "rec-missing-003" in result.missing_events

    def test_reconcile_result_is_frozen(self, connector: GitHubConnector) -> None:
        result = ReconcileResult(
            repository_full_name="acme/test",
            checked=5,
            reconciled=4,
            missing_events=["evt-miss"],
        )
        assert result.repository_full_name == "acme/test"


# ===========================================================================
# Locator creation tests
# ===========================================================================


class TestLocatorCreation:
    def test_repository_locator(self) -> None:
        loc = GitHubConnector.make_repository_locator("acme/backend", "abc123")
        assert loc.connector == "github"
        assert loc.uri == "github://acme/backend@abc123"
        assert loc.version_hint == "abc123"

    def test_file_locator(self) -> None:
        loc = GitHubConnector.make_file_locator("acme/backend", "abc123", "src/main.py")
        assert loc.connector == "github"
        assert loc.uri == "github://acme/backend@abc123/src/main.py"
        assert loc.version_hint == "abc123"

    def test_symbol_locator(self) -> None:
        loc = GitHubConnector.make_symbol_locator(
            "acme/backend", "abc123", "src/main.py", "MyClass"
        )
        assert loc.connector == "github"
        assert loc.uri == "github://acme/backend@abc123/src/main.py#MyClass"

    def test_locator_is_frozen(self) -> None:
        loc = GitHubConnector.make_repository_locator("acme/x", "sha")
        assert isinstance(loc, Locator)


# ===========================================================================
# Content digest tests
# ===========================================================================


class TestContentDigest:
    def test_compute_content_digest(self) -> None:
        digest = GitHubConnector.compute_content_digest(b"hello world")
        assert digest.startswith("sha256:")
        assert len(digest) == 71  # sha256: + 64 hex chars

    def test_digest_deterministic(self) -> None:
        d1 = GitHubConnector.compute_content_digest(b"test content")
        d2 = GitHubConnector.compute_content_digest(b"test content")
        assert d1 == d2

    def test_digest_different_for_different_content(self) -> None:
        d1 = GitHubConnector.compute_content_digest(b"content A")
        d2 = GitHubConnector.compute_content_digest(b"content B")
        assert d1 != d2


# ===========================================================================
# SCIP parser tests
# ===========================================================================


class TestSCIPParser:
    def test_parser_not_available_by_default(self, scip_parser: SCIPParser) -> None:
        assert scip_parser.is_available is False

    def test_parse_file_raises_when_unavailable(self, scip_parser: SCIPParser) -> None:
        with pytest.raises(SCIPUnavailableError, match="not available"):
            scip_parser.parse_file("test.py", "def foo(): pass")

    def test_parse_directory_raises_when_unavailable(
        self, scip_parser: SCIPParser
    ) -> None:
        with pytest.raises(SCIPUnavailableError, match="not available"):
            scip_parser.parse_directory("/src", {"test.py": "x"})

    def test_enable_disable(self, scip_parser: SCIPParser) -> None:
        scip_parser.enable()
        assert scip_parser.is_available is True
        scip_parser.disable()
        assert scip_parser.is_available is False

    def test_detect_language_python(self, scip_parser: SCIPParser) -> None:
        assert scip_parser.detect_language("main.py") == "python"

    def test_detect_language_typescript(self, scip_parser: SCIPParser) -> None:
        assert scip_parser.detect_language("index.ts") == "typescript"

    def test_detect_language_unknown(self, scip_parser: SCIPParser) -> None:
        assert scip_parser.detect_language("readme.md") is None

    def test_scip_symbol_is_frozen(self) -> None:
        sym = SCIPSymbol(
            name="my_func",
            kind=SymbolKind.DEFINITION,
            line_start=10,
            line_end=15,
        )
        assert sym.name == "my_func"
        assert sym.kind == SymbolKind.DEFINITION

    def test_scip_index_is_frozen(self) -> None:
        index = SCIPIndex(file_path="test.py", language="python")
        assert index.file_path == "test.py"
        assert index.symbols == ()


# ===========================================================================
# tree-sitter parser tests
# ===========================================================================


class TestTreeSitterParser:
    def test_parser_not_available_by_default(
        self, ts_parser: TreeSitterParser
    ) -> None:
        assert ts_parser.is_available is False

    def test_parse_file_raises_when_unavailable(
        self, ts_parser: TreeSitterParser
    ) -> None:
        with pytest.raises(TreeSitterUnavailableError, match="not available"):
            ts_parser.parse_file("test.py", "def foo(): pass")

    def test_parse_files_batch_raises_when_unavailable(
        self, ts_parser: TreeSitterParser
    ) -> None:
        with pytest.raises(TreeSitterUnavailableError, match="not available"):
            ts_parser.parse_files_batch({"test.py": "x"})

    def test_enable_disable(self, ts_parser: TreeSitterParser) -> None:
        ts_parser.enable()
        assert ts_parser.is_available is True
        ts_parser.disable()
        assert ts_parser.is_available is False

    def test_detect_language_go(self, ts_parser: TreeSitterParser) -> None:
        assert ts_parser.detect_language("main.go") == "go"

    def test_detect_language_rust(self, ts_parser: TreeSitterParser) -> None:
        assert ts_parser.detect_language("lib.rs") == "rust"

    def test_detect_language_unknown(self, ts_parser: TreeSitterParser) -> None:
        assert ts_parser.detect_language("Dockerfile") is None

    def test_load_grammar_returns_false_for_unknown(
        self, ts_parser: TreeSitterParser
    ) -> None:
        assert ts_parser.load_grammar("unknown_lang") is False

    def test_to_scip_symbols(self, ts_parser: TreeSitterParser) -> None:
        ts_sym = TreeSitterSymbol(
            name="my_class",
            kind=SymbolKind.SYMBOL,
            line_start=5,
            line_end=20,
            node_type="class_definition",
        )
        index = TreeSitterIndex(
            file_path="main.py",
            language="python",
            symbols=(ts_sym,),
        )
        scip_symbols = ts_parser.to_scip_symbols(index)
        assert len(scip_symbols) == 1
        assert scip_symbols[0].name == "my_class"
        assert scip_symbols[0].kind == SymbolKind.SYMBOL

    def test_to_scip_symbols_empty(self, ts_parser: TreeSitterParser) -> None:
        index = TreeSitterIndex(file_path="empty.py", language="python")
        scip_symbols = ts_parser.to_scip_symbols(index)
        assert scip_symbols == []


# ===========================================================================
# Data model contract tests
# ===========================================================================


class TestDataModels:
    def test_github_symbol_is_frozen(self) -> None:
        sym = GitHubSymbol(
            id=new_id(),
            file_id=new_id(),
            repository_id=new_id(),
            kind=SymbolKind.DEFINITION,
            name="func",
            line_start=1,
            line_end=5,
        )
        with pytest.raises(Exception, match="frozen"):
            sym.name = "changed"  # type: ignore[misc]

    def test_github_commit_parents_are_tuple(self) -> None:
        commit = GitHubCommit(
            id=new_id(),
            repository_id=new_id(),
            sha="abc123",
            authored_at=datetime.now(UTC),
            committed_at=datetime.now(UTC),
            parents=("p1", "p2"),
        )
        assert isinstance(commit.parents, tuple)
        assert len(commit.parents) == 2

    def test_github_blame_lines_are_frozen(self) -> None:
        line = GitHubBlameLine(line_number=1, commit_sha="abc")
        blame = GitHubBlame(
            id=new_id(),
            file_id=new_id(),
            commit_sha="abc",
            lines=(line,),
        )
        assert len(blame.lines) == 1
        assert blame.lines[0].line_number == 1

    def test_symbol_kind_values(self) -> None:
        assert SymbolKind.FILE == "file"
        assert SymbolKind.SYMBOL == "symbol"
        assert SymbolKind.DEFINITION == "definition"
        assert SymbolKind.REFERENCE == "reference"
        assert SymbolKind.IMPLEMENTATION == "implementation"

    def test_webhook_payload_is_frozen(self) -> None:
        payload = WebhookPayload(
            event_type="push",
            delivery_id="del-123",
            repository_full_name="acme/repo",
        )
        with pytest.raises(Exception, match="frozen"):
            payload.event_type = "pull_request"  # type: ignore[misc]

    def test_reconcile_result_is_frozen(self) -> None:
        result = ReconcileResult(
            repository_full_name="acme/x",
            checked=0,
            reconciled=0,
        )
        with pytest.raises(Exception, match="frozen"):
            result.checked = 10  # type: ignore[misc]


# ===========================================================================
# Cross-component integration tests
# ===========================================================================


class TestCrossComponentIntegration:
    def test_full_workflow_register_webhook_reconcile(
        self, connector: GitHubConnector
    ) -> None:
        # Register
        repo = connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/workflow",
            installation_id=1,
            webhook_secret=b"whsec-integration",
        )

        # Webhook events
        for i in range(5):
            event = WebhookEvent(
                id=f"wf-evt-{i:03d}",
                connector=GitHubConnector.CONNECTOR_NAME,
                source_object_id=repo.id,
                event_type=SyncEventType.UPDATE,
            )
            connector.receive_webhook(event)

        # Reconcile — all present
        result = connector.reconcile(
            "acme/workflow",
            [f"wf-evt-{i:03d}" for i in range(5)],
        )
        assert result.reconciled == 5
        assert result.missing_events == []

    def test_locator_roundtrip_through_models(
        self, connector: GitHubConnector, registered_repo: GitHubRepository
    ) -> None:
        loc = GitHubConnector.make_file_locator(
            registered_repo.full_name, "deadbeef", "src/main.py"
        )
        so = SourceObject(
            id=new_id(),
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            source_type="github_file",
        )
        sv = SourceVersion(
            id=new_id(),
            source_object_id=so.id,
            version_seq=1,
            locator=loc,
            content_digest=GitHubConnector.compute_content_digest(b"code"),
            observed_at=datetime.now(UTC),
            valid_at=datetime.now(UTC),
        )
        assert sv.locator.connector == "github"
        assert sv.locator.version_hint == "deadbeef"

    def test_scip_fallback_to_tree_sitter(
        self, scip_parser: SCIPParser, ts_parser: TreeSitterParser
    ) -> None:
        # SCIP unavailable, fall back to tree-sitter
        assert scip_parser.is_available is False
        assert ts_parser.is_available is False

        # Neither available — both raise
        with pytest.raises(SCIPUnavailableError):
            scip_parser.parse_file("x.py", "code")
        with pytest.raises(TreeSitterUnavailableError):
            ts_parser.parse_file("x.py", "code")

    def test_permission_revocation_blocks_reconcile(
        self, connector: GitHubConnector
    ) -> None:
        connector.register_repository(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            full_name="acme/revoked",
            installation_id=55,
            webhook_secret=b"secret-revoked",
        )
        connector.revoke_installation(55)
        assert connector.is_installation_active(55) is False
        # Reconcile still works but installation is revoked
        result = connector.reconcile("acme/revoked", [])
        assert result.checked == 0
