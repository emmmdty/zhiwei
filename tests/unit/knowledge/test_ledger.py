"""S5-T1 RED: Source Ledger and synchronization tests.

Tests cover:
- Immutable SourceVersion with observed/valid time
- Locator identity
- Watermark lifecycle
- Parent/tombstone relationships
- Stale transition
- Tenant/ACL/classification fields
- ObjectStore manifests (no raw content in index-only tables)
- Sync intent/outbox, duplicate/out-of-order webhook handling
- Reconciliation contract
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceObject,
    SourceVersion,
    SourceVersionState,
    SyncWatermark,
)
from zhiwei.knowledge.freshness import (
    FreshnessPolicy,
    FreshnessState,
    evaluate_freshness,
)
from zhiwei.knowledge.ledger import (
    DuplicateVersionError,
    ObjectNotFoundError,
    SourceLedger,
)
from zhiwei.knowledge.sync import (
    DuplicateWebhookError,
    OutOfOrderWebhookError,
    SyncEventType,
    SyncManager,
    WebhookEvent,
)
from zhiwei.knowledge.watermarks import WatermarkManager, WatermarkNotFoundError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _make_locator(**overrides: str | None) -> Locator:
    defaults: dict[str, str | None] = {"connector": "files", "uri": "/docs/spec.md"}
    defaults.update(overrides)
    non_none = {k: v for k, v in defaults.items() if v is not None}
    return Locator(**non_none)


def _make_acl() -> ACLSnapshot:
    return ACLSnapshot(
        allowed_principals=("user:alice",),
        denied_principals=(),
        allowed_groups=("group:engineering",),
    )


def _make_source_object(**overrides: object) -> SourceObject:
    defaults = {
        "id": new_id(),
        "organization_id": new_id(),
        "workspace_id": new_id(),
        "source_type": "document",
        "acl": _make_acl(),
        "classification": Classification.INTERNAL,
    }
    defaults.update(overrides)
    return SourceObject(**defaults)


def _content_digest(content: bytes = b"test content") -> str:
    return digest_bytes(content)


# ---------------------------------------------------------------------------
# Locator tests
# ---------------------------------------------------------------------------

class TestLocator:
    def test_locator_identity(self) -> None:
        """Locator is identified by connector+uri."""
        loc = _make_locator()
        assert loc.connector == "files"
        assert loc.uri == "/docs/spec.md"

    def test_locator_frozen(self) -> None:
        loc = _make_locator()
        with pytest.raises(ValidationError):
            loc.uri = "/changed"  # type: ignore[misc]

    def test_locator_rejects_blank(self) -> None:
        with pytest.raises(ValidationError):
            Locator(connector="", uri="/test")

    def test_locator_strips_whitespace(self) -> None:
        loc = Locator(connector="  files  ", uri="/docs/spec.md")
        assert loc.connector == "files"
        assert loc.uri == "/docs/spec.md"

    def test_locator_version_hint_optional(self) -> None:
        loc = Locator(connector="github", uri="/repo/commit/abc", version_hint="v1.0")
        assert loc.version_hint == "v1.0"


# ---------------------------------------------------------------------------
# SourceObject tests
# ---------------------------------------------------------------------------

class TestSourceObject:
    def test_creation(self) -> None:
        obj = _make_source_object()
        assert obj.source_type == "document"
        assert obj.classification == Classification.INTERNAL

    def test_frozen(self) -> None:
        obj = _make_source_object()
        with pytest.raises(ValidationError):
            obj.source_type = "changed"  # type: ignore[misc]

    def test_acl_snapshot(self) -> None:
        acl = _make_acl()
        assert "user:alice" in acl.allowed_principals
        assert "group:engineering" in acl.allowed_groups

    def test_classification_levels(self) -> None:
        for level in Classification:
            obj = _make_source_object(classification=level)
            assert obj.classification == level


# ---------------------------------------------------------------------------
# SourceVersion immutability tests
# ---------------------------------------------------------------------------

class TestSourceVersion:
    def test_immutable_version(self) -> None:
        """SourceVersion is immutable once created."""
        version = SourceVersion(
            id=new_id(),
            source_object_id=new_id(),
            version_seq=1,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        assert version.state == SourceVersionState.ACTIVE
        with pytest.raises(ValidationError):
            version.version_seq = 2  # type: ignore[misc]

    def test_observed_and_valid_time(self) -> None:
        """observed_at and valid_at capture distinct temporal semantics."""
        observed = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
        valid = datetime(2026, 9, 1, 11, 0, 0, tzinfo=UTC)
        version = SourceVersion(
            id=new_id(),
            source_object_id=new_id(),
            version_seq=1,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=observed,
            valid_at=valid,
        )
        assert version.observed_at == observed
        assert version.valid_at == valid
        assert version.valid_at > version.observed_at

    def test_content_digest_format(self) -> None:
        """Content digest must be sha256: prefixed."""
        version = SourceVersion(
            id=new_id(),
            source_object_id=new_id(),
            version_seq=1,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        assert version.content_digest.startswith("sha256:")
        assert len(version.content_digest) == 71

    def test_invalid_digest_rejected(self) -> None:
        with pytest.raises(ValidationError, match="sha256"):
            SourceVersion(
                id=new_id(),
                source_object_id=new_id(),
                version_seq=1,
                locator=_make_locator(),
                content_digest="md5:abc123",
                observed_at=NOW,
                valid_at=NOW,
            )

    def test_zero_version_rejected(self) -> None:
        with pytest.raises(ValidationError, match="version_seq"):
            SourceVersion(
                id=new_id(),
                source_object_id=new_id(),
                version_seq=0,
                locator=_make_locator(),
                content_digest=_content_digest(),
                observed_at=NOW,
                valid_at=NOW,
            )


# ---------------------------------------------------------------------------
# Parent/tombstone tests
# ---------------------------------------------------------------------------

class TestParentTombstone:
    def test_parent_version_reference(self) -> None:
        """A version can reference its parent."""
        parent_id = new_id()
        version = SourceVersion(
            id=new_id(),
            source_object_id=new_id(),
            version_seq=2,
            locator=_make_locator(),
            content_digest=_content_digest(b"v2 content"),
            observed_at=NOW,
            valid_at=NOW,
            parent_version_id=parent_id,
        )
        assert version.parent_version_id == parent_id

    def test_tombstone_flag(self) -> None:
        version = SourceVersion(
            id=new_id(),
            source_object_id=new_id(),
            version_seq=1,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
            tombstone=True,
        )
        assert version.tombstone is True


# ---------------------------------------------------------------------------
# Ledger tests
# ---------------------------------------------------------------------------

class TestSourceLedger:
    def test_register_and_get_object(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)
        assert ledger.get_object(obj.id) == obj

    def test_get_missing_object_raises(self) -> None:
        ledger = SourceLedger()
        with pytest.raises(ObjectNotFoundError):
            ledger.get_object(new_id())

    def test_create_version(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        version = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        assert version.version_seq == 1
        assert version.source_object_id == obj.id
        assert version.state == SourceVersionState.ACTIVE

    def test_incremental_version_seq(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(b"v1"),
            observed_at=NOW,
            valid_at=NOW,
        )
        v2 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(b"v2"),
            observed_at=NOW,
            valid_at=NOW,
        )
        assert v1.version_seq == 1
        assert v2.version_seq == 2

    def test_duplicate_digest_rejected(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        digest_val = _content_digest()
        ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=digest_val,
            observed_at=NOW,
            valid_at=NOW,
        )
        with pytest.raises(DuplicateVersionError):
            ledger.create_version(
                obj.id,
                locator=_make_locator(),
                content_digest=digest_val,
                observed_at=NOW,
                valid_at=NOW,
            )

    def test_acl_inherited_from_object(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        version = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        assert version.acl == obj.acl

    def test_classification_inherited_from_object(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object(classification=Classification.CONFIDENTIAL)
        ledger.register_object(obj)

        version = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        assert version.classification == Classification.CONFIDENTIAL

    def test_mark_stale(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(b"v1"),
            observed_at=NOW,
            valid_at=NOW,
        )
        updated = ledger.mark_stale(v1.id)
        assert updated.state == SourceVersionState.STALE

    def test_mark_stale_on_revoked_raises(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        ledger.revoke_version(v1.id)
        with pytest.raises(ValueError, match="Cannot mark a revoked version as stale"):
            ledger.mark_stale(v1.id)

    def test_revoke_version(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        revoked = ledger.revoke_version(v1.id)
        assert revoked.state == SourceVersionState.REVOKED
        assert revoked.tombstone is True

    def test_mark_stale_on_tombstone_raises(self) -> None:
        """Tombstone versions cannot be marked stale (revoked check fires first)."""
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        ledger.revoke_version(v1.id)
        with pytest.raises(ValueError, match="Cannot mark"):
            ledger.mark_stale(v1.id)

    def test_latest_version_returns_active(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(b"v1"),
            observed_at=NOW,
            valid_at=NOW,
        )
        v2 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(b"v2"),
            observed_at=NOW,
            valid_at=NOW,
        )
        ledger.mark_stale(v1.id)

        latest = ledger.latest_version(obj.id)
        assert latest is not None
        assert latest.id == v2.id

    def test_latest_version_returns_none_when_all_stale(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        ledger.mark_stale(v1.id)

        assert ledger.latest_version(obj.id) is None

    def test_list_versions_order(self) -> None:
        ledger = SourceLedger()
        obj = _make_source_object()
        ledger.register_object(obj)

        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(b"v1"),
            observed_at=NOW,
            valid_at=NOW,
        )
        v2 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(b"v2"),
            observed_at=NOW,
            valid_at=NOW,
        )

        versions = ledger.list_versions(obj.id)
        assert len(versions) == 2
        assert versions[0].id == v1.id
        assert versions[1].id == v2.id


# ---------------------------------------------------------------------------
# Watermark tests
# ---------------------------------------------------------------------------

class TestWatermarkManager:
    def test_get_or_create(self) -> None:
        wm = WatermarkManager()
        org_id = new_id()
        ws_id = new_id()

        watermark = wm.get_or_create("files", org_id, ws_id)
        assert watermark.connector == "files"
        assert watermark.sync_count == 0

    def test_get_or_create_idempotent(self) -> None:
        wm = WatermarkManager()
        org_id = new_id()
        ws_id = new_id()

        w1 = wm.get_or_create("files", org_id, ws_id)
        w2 = wm.get_or_create("files", org_id, ws_id)
        assert w1.id == w2.id

    def test_advance_watermark(self) -> None:
        wm = WatermarkManager()
        org_id = new_id()
        ws_id = new_id()

        wm.get_or_create("files", org_id, ws_id)
        updated = wm.advance(
            "files", org_id, ws_id,
            new_watermark="2026-09-01T12:00:00Z",
            last_event_id="evt-001",
        )
        assert updated.watermark == "2026-09-01T12:00:00Z"
        assert updated.sync_count == 1
        assert updated.last_event_id == "evt-001"

    def test_advance_nonexistent_raises(self) -> None:
        wm = WatermarkManager()
        with pytest.raises(WatermarkNotFoundError):
            wm.advance("files", new_id(), new_id(), new_watermark="next")

    def test_list_all_for_organization(self) -> None:
        wm = WatermarkManager()
        org_id = new_id()

        wm.get_or_create("files", org_id, new_id())
        wm.get_or_create("github", org_id, new_id())

        results = wm.list_all(org_id)
        assert len(results) == 2

    def test_watermark_frozen(self) -> None:
        wm = WatermarkManager()
        org_id = new_id()
        ws_id = new_id()

        watermark = wm.get_or_create("files", org_id, ws_id)
        with pytest.raises(ValidationError):
            watermark.sync_count = 5  # type: ignore[misc]

    def test_watermark_must_be_nonblank(self) -> None:
        with pytest.raises(ValidationError, match="watermark"):
            SyncWatermark(
                id=new_id(),
                connector="files",
                organization_id=new_id(),
                workspace_id=new_id(),
                watermark="  ",
                last_synced_at=NOW,
                sync_count=0,
            )


# ---------------------------------------------------------------------------
# Freshness tests
# ---------------------------------------------------------------------------

class TestFreshness:
    def _make_version(self, observed_at: datetime) -> SourceVersion:
        return SourceVersion(
            id=new_id(),
            source_object_id=new_id(),
            version_seq=1,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=observed_at,
            valid_at=observed_at,
        )

    def test_fresh_version(self) -> None:
        version = self._make_version(observed_at=NOW)
        result = evaluate_freshness(version, reference_time=NOW)
        assert result.state == FreshnessState.FRESH

    def test_aging_version(self) -> None:
        version = self._make_version(observed_at=NOW - timedelta(days=10))
        result = evaluate_freshness(version, reference_time=NOW)
        assert result.state == FreshnessState.AGING

    def test_stale_version(self) -> None:
        version = self._make_version(observed_at=NOW - timedelta(days=40))
        result = evaluate_freshness(version, reference_time=NOW)
        assert result.state == FreshnessState.STALE

    def test_expired_version(self) -> None:
        policy = FreshnessPolicy(
            connector="files",
            max_age=timedelta(days=30),
            expire_after=timedelta(days=60),
        )
        version = self._make_version(observed_at=NOW - timedelta(days=90))
        result = evaluate_freshness(version, policy, reference_time=NOW)
        assert result.state == FreshnessState.EXPIRED

    def test_revoked_version_is_stale(self) -> None:
        version = self._make_version(observed_at=NOW)
        revoked = version.model_copy(
            update={"state": SourceVersionState.REVOKED}
        )
        result = evaluate_freshness(revoked, reference_time=NOW)
        assert result.state == FreshnessState.STALE

    def test_stale_state_version_is_stale(self) -> None:
        version = self._make_version(observed_at=NOW)
        stale = version.model_copy(update={"state": SourceVersionState.STALE})
        result = evaluate_freshness(stale, reference_time=NOW)
        assert result.state == FreshnessState.STALE

    def test_custom_policy(self) -> None:
        policy = FreshnessPolicy(
            connector="github",
            max_age=timedelta(days=1),
            aging_threshold=timedelta(hours=12),
        )
        version = self._make_version(observed_at=NOW - timedelta(hours=18))
        result = evaluate_freshness(version, policy, reference_time=NOW)
        assert result.state == FreshnessState.AGING


# ---------------------------------------------------------------------------
# Sync / Webhook tests
# ---------------------------------------------------------------------------

class TestSyncManager:
    def test_receive_webhook_creates_intent(self) -> None:
        sm = SyncManager()
        event = WebhookEvent(
            id="evt-001",
            connector="github",
            source_object_id=new_id(),
            event_type=SyncEventType.CREATE,
        )
        intent = sm.receive_webhook(event)
        assert intent.connector == "github"
        assert intent.event_id == "evt-001"
        assert intent.processed is False

    def test_duplicate_event_raises(self) -> None:
        sm = SyncManager()
        event = WebhookEvent(
            id="evt-001",
            connector="github",
            source_object_id=new_id(),
            event_type=SyncEventType.CREATE,
        )
        sm.receive_webhook(event)
        with pytest.raises(DuplicateWebhookError):
            sm.receive_webhook(event)

    def test_out_of_order_event_raises(self) -> None:
        sm = SyncManager()
        e1 = WebhookEvent(
            id="evt-002",
            connector="github",
            source_object_id=new_id(),
            event_type=SyncEventType.CREATE,
        )
        e2 = WebhookEvent(
            id="evt-001",
            connector="github",
            source_object_id=new_id(),
            event_type=SyncEventType.CREATE,
        )
        sm.receive_webhook(e1)
        with pytest.raises(OutOfOrderWebhookError):
            sm.receive_webhook(e2)

    def test_independent_connectors_no_ordering_conflict(self) -> None:
        sm = SyncManager()
        e1 = WebhookEvent(
            id="evt-001",
            connector="github",
            source_object_id=new_id(),
            event_type=SyncEventType.CREATE,
        )
        e2 = WebhookEvent(
            id="evt-001",
            connector="files",
            source_object_id=new_id(),
            event_type=SyncEventType.CREATE,
        )
        sm.receive_webhook(e1)
        sm.receive_webhook(e2)  # same id but different connector

    def test_get_pending_intents(self) -> None:
        sm = SyncManager()
        event = WebhookEvent(
            id="evt-001",
            connector="github",
            source_object_id=new_id(),
            event_type=SyncEventType.CREATE,
        )
        intent = sm.receive_webhook(event)
        pending = sm.get_pending_intents()
        assert len(pending) == 1
        assert pending[0].id == intent.id

    def test_mark_processed(self) -> None:
        sm = SyncManager()
        event = WebhookEvent(
            id="evt-001",
            connector="github",
            source_object_id=new_id(),
            event_type=SyncEventType.CREATE,
        )
        intent = sm.receive_webhook(event)
        sm.mark_processed(intent.id)
        assert sm.get_pending_intents() == []

    def test_reconcile_finds_missing(self) -> None:
        sm = SyncManager()
        expected = ["evt-001", "evt-002", "evt-003"]
        known = {"evt-001"}

        report = sm.reconcile("github", known, expected)
        assert report.missing == ["evt-002", "evt-003"]
        assert report.checked == 3
        assert report.reconciled == 1

    def test_reconcile_empty_missing(self) -> None:
        sm = SyncManager()
        expected = ["evt-001", "evt-002"]
        known = {"evt-001", "evt-002"}

        report = sm.reconcile("github", known, expected)
        assert report.missing == []
        assert report.reconciled == 2


# ---------------------------------------------------------------------------
# No raw content in index-only tables (structural test)
# ---------------------------------------------------------------------------

class TestNoRawContentInIndex:
    """SourceVersion contains only digests and metadata, never raw content.

    The ObjectStore manifest holds content; the ledger/index stores only
    the digest for content-addressed retrieval.
    """

    def test_version_has_no_content_field(self) -> None:
        SourceVersion(
            id=new_id(),
            source_object_id=new_id(),
            version_seq=1,
            locator=_make_locator(),
            content_digest=_content_digest(),
            observed_at=NOW,
            valid_at=NOW,
        )
        # SourceVersion should not have a 'content' or 'raw_content' field
        field_names = set(SourceVersion.model_fields.keys())
        assert "content" not in field_names
        assert "raw_content" not in field_names
        assert "body" not in field_names
