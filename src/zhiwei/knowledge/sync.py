"""Synchronization: webhook incremental, reconciliation, duplicate/out-of-order handling.

Delete/revoke priority; updates create new version, old Evidence stale.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now


class DuplicateWebhookError(Exception):
    """Raised when a duplicate webhook event is detected."""


class OutOfOrderWebhookError(Exception):
    """Raised when a webhook event arrives out of order."""
    def __init__(self, message: str, *, expected: str, received: str) -> None:
        super().__init__(message)
        self.expected = expected
        self.received = received


class SyncEventType(StrEnum):
    """Types of sync events."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    REVOKE = "revoke"


class SyncIntent(BaseModel):
    """A pending sync action to be processed.

    SyncIntents are written to an outbox before processing to ensure
    at-least-once delivery and idempotency.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=new_id)
    event_type: SyncEventType
    connector: str = Field(min_length=1)
    source_object_id: UUID
    event_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    processed: bool = False


class WebhookEvent(BaseModel):
    """An incoming webhook event from an external source system.

    Events carry the source-native event identifier for deduplication
    and ordering checks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    source_object_id: UUID
    event_type: SyncEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=utc_now)


class ReconciliationReport(BaseModel):
    """Result of a reconciliation pass between webhook events and ledger state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector: str
    checked: int = Field(ge=0)
    reconciled: int = Field(ge=0)
    missing: list[str] = Field(default_factory=list)
    duplicates_skipped: int = Field(ge=0)
    completed_at: datetime = Field(default_factory=utc_now)


class SyncManager:
    """Manages webhook sync: deduplication, ordering, and reconciliation.

    Duplicate detection uses event_id. Ordering uses a monotonically
    increasing sequence per connector. Reconciliation compares the set
    of known event IDs against expected sequence to find gaps.
    """

    def __init__(self) -> None:
        self._processed_events: dict[str, str] = {}  # event_id -> connector
        self._event_sequence: dict[str, list[str]] = {}  # connector -> [event_ids]
        self._intents: dict[UUID, SyncIntent] = {}

    def receive_webhook(self, event: WebhookEvent) -> SyncIntent:
        """Process an incoming webhook event.

        - Duplicate event_id raises DuplicateWebhookError
        - Out-of-order event raises OutOfOrderWebhookError
        - Returns a SyncIntent for the ledger to process
        """
        event_key = f"{event.connector}:{event.id}"

        # Deduplication check
        if event_key in self._processed_events:
            raise DuplicateWebhookError(
                f"Event {event.id} for connector {event.connector} already processed"
            )

        # Ordering check: events must arrive in order per connector
        sequence = self._event_sequence.setdefault(event.connector, [])
        if sequence:
            last_event_id = sequence[-1]
            if event.id <= last_event_id:
                raise OutOfOrderWebhookError(
                    f"Event {event.id} arrived out of order for connector {event.connector}",
                    expected=f">{last_event_id}",
                    received=event.id,
                )

        sequence.append(event.id)
        self._processed_events[event_key] = event.connector

        intent = SyncIntent(
            event_type=event.event_type,
            connector=event.connector,
            source_object_id=event.source_object_id,
            event_id=event.id,
            payload=event.payload,
            idempotency_key=event_key,
        )
        self._intents[intent.id] = intent
        return intent

    def get_pending_intents(self) -> list[SyncIntent]:
        """Return all unprocessed sync intents."""
        return [i for i in self._intents.values() if not i.processed]

    def mark_processed(self, intent_id: UUID) -> None:
        """Mark a sync intent as processed."""
        if intent_id not in self._intents:
            raise ValueError(f"SyncIntent {intent_id} not found")
        intent = self._intents[intent_id]
        self._intents[intent_id] = intent.model_copy(update={"processed": True})

    def reconcile(
        self,
        connector: str,
        known_event_ids: set[str],
        expected_sequence: list[str],
    ) -> ReconciliationReport:
        """Reconcile expected sequence against known events.

        Compares expected_sequence against known_event_ids to find
        missing events. Returns a report for the caller to act on.
        """
        known_for_connector = {
            eid for eid, conn in self._processed_events.items() if conn == connector
        }

        all_known = known_for_connector | known_event_ids
        missing = [eid for eid in expected_sequence if eid not in all_known]

        duplicates = len(known_for_connector & known_event_ids)

        return ReconciliationReport(
            connector=connector,
            checked=len(expected_sequence),
            reconciled=len(expected_sequence) - len(missing),
            missing=missing,
            duplicates_skipped=duplicates,
        )
