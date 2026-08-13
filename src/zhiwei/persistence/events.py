"""Canonical event identity, chain verification and the S0 projection reducer."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.canonical import digest
from zhiwei.contracts.envelope import SchemaRegistry
from zhiwei.persistence.models import AuditEvent, CanonicalEvent


class EventChainError(ValueError):
    """Raised when committed events do not form one intact sequence/digest chain."""


class EventCommand(BaseModel):
    """One append request whose stable idempotency key is scoped to a Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    event_type: str = Field(min_length=1, max_length=128)
    payload_schema_version: int = Field(gt=0)
    payload: dict[str, Any]
    actor_ref: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    task_id: UUID | None = None
    attempt_id: UUID | None = None
    epoch_id: UUID | None = None


class CanonicalEventData(EventCommand):
    """Digest-relevant fields of a committed canonical event."""

    organization_id: UUID
    workspace_id: UUID
    sequence_no: int = Field(gt=0)
    previous_event_digest: str | None
    event_digest: str


class AuditEventData(BaseModel):
    """Digest-relevant fields of one immutable tenant audit event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: UUID
    workspace_id: UUID | None
    action: str
    resource_type: str
    resource_id: UUID
    actor_ref: str
    payload_digest: str
    previous_event_digest: str | None
    event_digest: str


def validate_event_command(
    command: EventCommand, schema_registry: SchemaRegistry
) -> EventCommand:
    """Resolve and strictly validate an event payload before any async boundary."""
    command_snapshot = command.model_copy(deep=True)
    model = schema_registry.resolve(
        command_snapshot.event_type, command_snapshot.payload_schema_version
    )
    payload = model.model_validate(command_snapshot.payload, strict=True, extra="forbid")
    validated_payload = payload.model_dump(mode="json", by_alias=True)
    if not isinstance(validated_payload, dict):
        raise ValueError("canonical event payload schema must produce a JSON object")
    return command_snapshot.model_copy(update={"payload": deepcopy(validated_payload)})


def build_event_digest(
    *,
    organization_id: UUID,
    workspace_id: UUID,
    command: EventCommand,
    sequence_no: int,
    previous_event_digest: str | None,
) -> str:
    """Digest every semantic append field and its exact chain position."""
    if sequence_no <= 0:
        raise ValueError("event sequence must be positive")
    return digest(
        {
            "organization_id": str(organization_id),
            "workspace_id": str(workspace_id),
            "run_id": str(command.run_id),
            "sequence_no": sequence_no,
            "event_type": command.event_type,
            "payload_schema_version": command.payload_schema_version,
            "payload": command.payload,
            "actor_ref": command.actor_ref,
            "task_id": None if command.task_id is None else str(command.task_id),
            "attempt_id": None if command.attempt_id is None else str(command.attempt_id),
            "epoch_id": None if command.epoch_id is None else str(command.epoch_id),
            "idempotency_key": command.idempotency_key,
            "previous_event_digest": previous_event_digest,
        }
    )


def build_audit_digest(event: AuditEventData) -> str:
    """Recompute one audit event digest from its immutable semantic fields."""
    return digest(
        {
            "organization_id": str(event.organization_id),
            "workspace_id": None if event.workspace_id is None else str(event.workspace_id),
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": str(event.resource_id),
            "actor_ref": event.actor_ref,
            "payload_digest": event.payload_digest,
            "previous_event_digest": event.previous_event_digest,
        }
    )


def verify_event_chain(events: Iterable[CanonicalEventData]) -> None:
    """Fail closed unless events start at one and exactly reproduce their digest chain."""
    previous_event_digest: str | None = None
    chain_scope: tuple[UUID, UUID, UUID] | None = None
    for expected_sequence, event in enumerate(events, start=1):
        event_scope = (event.organization_id, event.workspace_id, event.run_id)
        if chain_scope is None:
            chain_scope = event_scope
        elif event_scope != chain_scope:
            raise EventChainError("event chain crosses tenant or Run scope")
        if event.sequence_no != expected_sequence:
            raise EventChainError(
                f"event sequence is not contiguous at {event.sequence_no}; expected {expected_sequence}"
            )
        if event.previous_event_digest != previous_event_digest:
            raise EventChainError(f"previous event digest mismatch at sequence {event.sequence_no}")
        command = _command_from_event(event)
        expected_digest = build_event_digest(
            organization_id=event.organization_id,
            workspace_id=event.workspace_id,
            command=command,
            sequence_no=event.sequence_no,
            previous_event_digest=previous_event_digest,
        )
        if event.event_digest != expected_digest:
            raise EventChainError(f"event digest mismatch at sequence {event.sequence_no}")
        previous_event_digest = event.event_digest


def reduce_projection(
    state: Mapping[str, Any], event: CanonicalEventData
) -> dict[str, Any]:
    """Append one canonical summary to the deterministic S0 projection cache."""
    reduced = deepcopy(dict(state))
    raw_events = reduced.setdefault("events", [])
    if not isinstance(raw_events, list):
        raise EventChainError("projection events must be a list")
    expected_sequence = len(raw_events) + 1
    if event.sequence_no != expected_sequence:
        raise EventChainError(
            f"projection sequence is not contiguous; expected {expected_sequence}"
        )
    raw_events.append(
        {
            "sequence_no": event.sequence_no,
            "event_type": event.event_type,
            "payload_schema_version": event.payload_schema_version,
            "payload": deepcopy(dict(event.payload)),
            "event_digest": event.event_digest,
        }
    )
    return reduced


def verify_audit_chain(events: Iterable[AuditEventData]) -> str | None:
    """Verify digest integrity and return the single head of an unordered audit chain."""
    materialized = list(events)
    if not materialized:
        return None
    by_digest: dict[str, AuditEventData] = {}
    successor_by_previous: dict[str | None, AuditEventData] = {}
    scope: tuple[UUID, UUID | None] | None = None
    for event in materialized:
        event_scope = (event.organization_id, event.workspace_id)
        if scope is None:
            scope = event_scope
        elif event_scope != scope:
            raise EventChainError("audit chain crosses tenant scope")
        if build_audit_digest(event) != event.event_digest:
            raise EventChainError("audit event digest mismatch")
        if event.event_digest in by_digest:
            raise EventChainError("audit event digest is duplicated")
        if event.previous_event_digest in successor_by_previous:
            raise EventChainError("audit chain branches from one predecessor")
        by_digest[event.event_digest] = event
        successor_by_previous[event.previous_event_digest] = event

    root = successor_by_previous.get(None)
    if root is None:
        raise EventChainError("audit chain has no root")
    visited: set[str] = set()
    current = root
    while True:
        if current.event_digest in visited:
            raise EventChainError("audit chain contains a cycle")
        visited.add(current.event_digest)
        successor = successor_by_previous.get(current.event_digest)
        if successor is None:
            break
        current = successor
    if visited != set(by_digest):
        raise EventChainError("audit chain is disconnected or has a missing predecessor")
    return current.event_digest


def event_data_from_row(row: CanonicalEvent) -> CanonicalEventData:
    """Detach the digest-relevant values from a persisted ORM event row."""
    return CanonicalEventData(
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        run_id=row.run_id,
        sequence_no=row.sequence_no,
        event_type=row.event_type,
        payload_schema_version=row.payload_schema_version,
        payload=row.payload,
        actor_ref=row.actor_ref,
        task_id=row.task_id,
        attempt_id=row.attempt_id,
        epoch_id=row.epoch_id,
        idempotency_key=row.idempotency_key,
        previous_event_digest=row.previous_event_digest,
        event_digest=row.event_digest,
    )


def audit_data_from_row(row: AuditEvent) -> AuditEventData:
    """Detach the digest-relevant values from a persisted ORM audit row."""
    return AuditEventData(
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        actor_ref=row.actor_ref,
        payload_digest=row.payload_digest,
        previous_event_digest=row.previous_event_digest,
        event_digest=row.event_digest,
    )


def _command_from_event(event: CanonicalEventData) -> EventCommand:
    return EventCommand(
        run_id=event.run_id,
        event_type=event.event_type,
        payload_schema_version=event.payload_schema_version,
        payload=dict(event.payload),
        actor_ref=event.actor_ref,
        task_id=event.task_id,
        attempt_id=event.attempt_id,
        epoch_id=event.epoch_id,
        idempotency_key=event.idempotency_key,
    )
