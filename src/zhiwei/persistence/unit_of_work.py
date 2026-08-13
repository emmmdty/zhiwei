"""Atomic canonical event, projection, audit and outbox transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.envelope import SchemaRegistry
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.events import (
    AuditEventData,
    EventChainError,
    EventCommand,
    audit_data_from_row,
    build_audit_digest,
    build_event_digest,
    event_data_from_row,
    reduce_projection,
    validate_event_command,
    verify_audit_chain,
    verify_event_chain,
)
from zhiwei.persistence.models import (
    AuditEvent,
    CanonicalEvent,
    CanonicalProjection,
    OutboxMessage,
    Run,
)
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired


class EventIdempotencyConflict(RuntimeError):
    """Raised when an event idempotency key is reused for a different command."""


class RunNotFound(LookupError):
    """Raised when the target Run is absent from the explicit tenant scope."""


class AuditChainError(RuntimeError):
    """Raised when the existing tenant audit chain fails integrity verification."""


class ProjectionMismatch(RuntimeError):
    """Raised when the projection cache disagrees with canonical committed events."""


class EventAppendResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID
    sequence_no: int
    event_digest: str
    created: bool


class ProjectionRebuildResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    sequence_no: int
    head_event_digest: str | None
    state: dict[str, Any]


class CanonicalUnitOfWork:
    """Commit the canonical event write path inside an existing tenant transaction."""

    def __init__(
        self,
        session: AsyncSession,
        context: TenantContext | None,
        *,
        schema_registry: SchemaRegistry,
    ) -> None:
        if context is None:
            raise TenantContextRequired("organization context is required")
        if context.workspace_id is None:
            raise TenantContextRequired("canonical events require workspace context")
        self._session = session
        self._context = context
        self._workspace_id = context.workspace_id
        self._schema_registry = schema_registry

    async def append_event(self, command: EventCommand) -> EventAppendResult:
        command = validate_event_command(command, self._schema_registry)
        await self._lock(command.run_id, namespace=0x45564E54)
        run = await self._run(command.run_id)

        existing = await self._session.scalar(
            select(CanonicalEvent).where(
                CanonicalEvent.organization_id == self._context.organization_id,
                CanonicalEvent.workspace_id == self._context.workspace_id,
                CanonicalEvent.run_id == command.run_id,
                CanonicalEvent.idempotency_key == command.idempotency_key,
            )
        )
        if existing is not None:
            expected_digest = build_event_digest(
                organization_id=self._context.organization_id,
                workspace_id=self._workspace_id,
                command=command,
                sequence_no=existing.sequence_no,
                previous_event_digest=existing.previous_event_digest,
            )
            if expected_digest != existing.event_digest:
                raise EventIdempotencyConflict(
                    "event idempotency key was already used for a different command"
                )
            return EventAppendResult(
                event_id=existing.id,
                sequence_no=existing.sequence_no,
                event_digest=existing.event_digest,
                created=False,
            )

        projection = await self._session.scalar(
            select(CanonicalProjection)
            .where(
                CanonicalProjection.organization_id == self._context.organization_id,
                CanonicalProjection.workspace_id == self._context.workspace_id,
                CanonicalProjection.run_id == command.run_id,
            )
            .with_for_update()
        )
        committed_events = list(
            (
                await self._session.scalars(
                    select(CanonicalEvent)
                    .where(
                        CanonicalEvent.organization_id == self._context.organization_id,
                        CanonicalEvent.workspace_id == self._workspace_id,
                        CanonicalEvent.run_id == command.run_id,
                    )
                    .order_by(CanonicalEvent.sequence_no)
                )
            ).all()
        )
        committed_event_data = [event_data_from_row(event) for event in committed_events]
        verify_event_chain(committed_event_data)
        canonical_state: dict[str, Any] = {}
        for committed_event in committed_event_data:
            canonical_state = reduce_projection(canonical_state, committed_event)
        canonical_sequence = len(committed_events)
        canonical_head = None if not committed_events else committed_events[-1].event_digest
        if projection is None:
            if committed_events:
                raise ProjectionMismatch("projection is missing for committed canonical events")
        elif (
            projection.sequence_no != canonical_sequence
            or projection.head_event_digest != canonical_head
            or projection.state != canonical_state
        ):
            raise ProjectionMismatch("projection must be rebuilt from canonical events before append")

        sequence_no = canonical_sequence + 1
        previous_event_digest = canonical_head
        event_digest = build_event_digest(
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            command=command,
            sequence_no=sequence_no,
            previous_event_digest=previous_event_digest,
        )
        event = CanonicalEvent(
            id=uuid4(),
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            run_id=command.run_id,
            sequence_no=sequence_no,
            event_type=command.event_type,
            payload_schema_version=command.payload_schema_version,
            payload=command.payload,
            actor_ref=command.actor_ref,
            task_id=command.task_id,
            attempt_id=command.attempt_id,
            epoch_id=command.epoch_id,
            idempotency_key=command.idempotency_key,
            previous_event_digest=previous_event_digest,
            event_digest=event_digest,
        )
        state = reduce_projection(canonical_state, event_data_from_row(event))
        now = utc_now()
        if projection is None:
            projection = CanonicalProjection(
                run_id=command.run_id,
                organization_id=self._context.organization_id,
                workspace_id=self._workspace_id,
                sequence_no=sequence_no,
                head_event_digest=event_digest,
                state=state,
                schema_version=1,
                updated_at=now,
            )
            self._session.add(projection)
        else:
            projection.sequence_no = sequence_no
            projection.head_event_digest = event_digest
            projection.state = state
            projection.updated_at = now

        self._session.add(event)
        run.updated_at = now
        await self._append_audit_and_outbox(event, outbox_available_at=now)
        await self._session.flush()
        return EventAppendResult(
            event_id=event.id,
            sequence_no=event.sequence_no,
            event_digest=event.event_digest,
            created=True,
        )

    async def rebuild_projection(self, run_id: UUID) -> ProjectionRebuildResult:
        await self._lock(run_id, namespace=0x45564E54)
        await self._run(run_id)
        events = list(
            (
                await self._session.scalars(
                    select(CanonicalEvent)
                    .where(
                        CanonicalEvent.organization_id == self._context.organization_id,
                        CanonicalEvent.workspace_id == self._context.workspace_id,
                        CanonicalEvent.run_id == run_id,
                    )
                    .order_by(CanonicalEvent.sequence_no)
                )
            ).all()
        )
        event_data = [event_data_from_row(event) for event in events]
        verify_event_chain(event_data)
        state: dict[str, Any] = {}
        for event in event_data:
            state = reduce_projection(state, event)
        projection = await self._session.scalar(
            select(CanonicalProjection)
            .where(
                CanonicalProjection.organization_id == self._context.organization_id,
                CanonicalProjection.workspace_id == self._context.workspace_id,
                CanonicalProjection.run_id == run_id,
            )
            .with_for_update()
        )
        sequence_no = len(events)
        head_event_digest = None if not events else events[-1].event_digest
        if projection is None:
            projection = CanonicalProjection(
                run_id=run_id,
                organization_id=self._context.organization_id,
                workspace_id=self._context.workspace_id,
                sequence_no=sequence_no,
                head_event_digest=head_event_digest,
                state=state,
                schema_version=1,
                updated_at=utc_now(),
            )
            self._session.add(projection)
        else:
            projection.sequence_no = sequence_no
            projection.head_event_digest = head_event_digest
            projection.state = state
            projection.updated_at = utc_now()
        await self._session.flush()
        return ProjectionRebuildResult(
            run_id=run_id,
            sequence_no=sequence_no,
            head_event_digest=head_event_digest,
            state=state,
        )

    async def _run(self, run_id: UUID) -> Run:
        run = await self._session.scalar(
            select(Run).where(
                Run.id == run_id,
                Run.organization_id == self._context.organization_id,
                Run.workspace_id == self._context.workspace_id,
            )
        )
        if run is None:
            raise RunNotFound("Run is missing from tenant scope")
        return run

    async def _append_audit_and_outbox(
        self, event: CanonicalEvent, *, outbox_available_at: datetime
    ) -> None:
        await self._lock(self._workspace_id, namespace=0x41554454)
        audit_rows = list(
            (
                await self._session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.organization_id == self._context.organization_id,
                        AuditEvent.workspace_id == self._workspace_id,
                    )
                )
            ).all()
        )
        try:
            previous_audit_digest = verify_audit_chain(
                audit_data_from_row(row) for row in audit_rows
            )
        except EventChainError as exc:
            raise AuditChainError(str(exc)) from exc
        audit_data = AuditEventData(
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            action="canonical_event.append",
            resource_type="run",
            resource_id=event.run_id,
            actor_ref=event.actor_ref,
            payload_digest=event.event_digest,
            previous_event_digest=previous_audit_digest,
            event_digest="",
        )
        audit_digest = build_audit_digest(audit_data)
        audit_committed_at = utc_now()
        self._session.add(
            AuditEvent(
                id=uuid4(),
                organization_id=self._context.organization_id,
                workspace_id=self._workspace_id,
                action="canonical_event.append",
                resource_type="run",
                resource_id=event.run_id,
                actor_ref=event.actor_ref,
                payload_digest=event.event_digest,
                previous_event_digest=previous_audit_digest,
                event_digest=audit_digest,
                schema_version=1,
                created_at=audit_committed_at,
            )
        )
        self._session.add(
            OutboxMessage(
                id=uuid4(),
                organization_id=self._context.organization_id,
                workspace_id=self._workspace_id,
                topic="canonical.event.committed",
                event_key=str(event.id),
                payload={
                    "event_id": str(event.id),
                    "run_id": str(event.run_id),
                    "sequence_no": event.sequence_no,
                    "event_digest": event.event_digest,
                },
                status="pending",
                attempts=0,
                available_at=outbox_available_at,
                schema_version=1,
                created_at=outbox_available_at,
            )
        )

    async def _lock(self, value: UUID, *, namespace: int) -> None:
        raw = int.from_bytes(value.bytes[:8], byteorder="big") ^ namespace
        lock_key = raw if raw < 2**63 else raw - 2**64
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
        )
