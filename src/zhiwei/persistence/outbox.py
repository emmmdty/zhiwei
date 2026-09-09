"""Leased transactional outbox with fenced, at-least-once delivery."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.time import ensure_utc
from zhiwei.persistence.models import OutboxMessage
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired


class OutboxStateError(RuntimeError):
    """Raised when an outbox claim or transition is stale, forged or out of scope."""


class OutboxDelivery(BaseModel):
    """Detached delivery data bound to one tenant and fenced worker claim."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    workspace_id: UUID | None
    topic: str
    event_key: str
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: datetime
    claimed_by: str
    claim_token: UUID
    lease_expires_at: datetime
    dead_lettered_at: datetime | None = None


class OutboxSink(Protocol):
    async def publish(self, message: OutboxDelivery) -> None:
        """Deliver at least once, deduplicating retries by the stable message id."""


class MemoryOutboxSink:
    """Idempotent in-process sink used only by tests and local smoke checks."""

    def __init__(self) -> None:
        self.deliveries: list[OutboxDelivery] = []
        self._by_id: dict[UUID, OutboxDelivery] = {}

    async def publish(self, message: OutboxDelivery) -> None:
        existing = self._by_id.get(message.id)
        if existing is not None:
            if (
                existing.topic,
                existing.event_key,
                existing.payload,
            ) != (message.topic, message.event_key, message.payload):
                raise OutboxStateError("stable outbox id was reused with different content")
            return
        detached = message.model_copy(deep=True)
        self._by_id[message.id] = detached
        self.deliveries.append(detached)


class OutboxRepository:
    """Tenant-explicit lease and retry transitions for transactional outbox rows."""

    def __init__(self, session: AsyncSession, context: TenantContext | None) -> None:
        if context is None:
            raise TenantContextRequired("organization context is required")
        self._session = session
        self._context = context

    async def claim_batch(
        self,
        *,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> list[OutboxDelivery]:
        if not worker_id or limit <= 0 or lease_duration <= timedelta(0):
            raise ValueError("worker_id, positive claim limit and lease duration are required")
        claimed_at = ensure_utc(now)
        statement = (
            select(OutboxMessage)
            .where(
                OutboxMessage.organization_id == self._context.organization_id,
                OutboxMessage.workspace_id == self._context.workspace_id,
                or_(
                    and_(
                        OutboxMessage.status == "pending",
                        OutboxMessage.available_at <= claimed_at,
                    ),
                    and_(
                        OutboxMessage.status == "processing",
                        OutboxMessage.lease_expires_at <= claimed_at,
                    ),
                ),
            )
            .order_by(OutboxMessage.available_at, OutboxMessage.created_at, OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.scalars(statement)).all())
        deliveries: list[OutboxDelivery] = []
        for row in rows:
            row.status = "processing"
            row.claimed_by = worker_id
            row.claim_token = uuid4()
            row.claimed_at = claimed_at
            row.lease_expires_at = claimed_at + lease_duration
            deliveries.append(_delivery(row))
        await self._session.flush()
        return deliveries

    async def validate_claim(self, message: OutboxDelivery) -> OutboxDelivery:
        """Lock a fenced claim and return canonical content read from the database."""
        return _delivery(await self._locked_claim(message))

    async def mark_delivered(self, message: OutboxDelivery) -> OutboxDelivery:
        row = await self._locked_claim(message)
        row.status = "delivered"
        row.claimed_by = None
        row.claim_token = None
        row.claimed_at = None
        row.lease_expires_at = None
        await self._session.flush()
        return _delivery(row, claim=message)

    async def mark_failed(
        self,
        message: OutboxDelivery,
        *,
        error: str,
        now: datetime,
        max_attempts: int,
        base_delay: timedelta,
    ) -> OutboxDelivery:
        if not error or max_attempts <= 0 or base_delay < timedelta(0):
            raise ValueError("error, positive max_attempts and non-negative delay are required")
        failed_at = ensure_utc(now)
        row = await self._locked_claim(message)
        row.attempts += 1
        row.last_error = error
        row.claimed_by = None
        row.claim_token = None
        row.claimed_at = None
        row.lease_expires_at = None
        if row.attempts >= max_attempts:
            row.status = "dead_letter"
            row.dead_lettered_at = failed_at
        else:
            row.status = "pending"
            row.available_at = failed_at + base_delay * (2 ** (row.attempts - 1))
        await self._session.flush()
        return _delivery(row, claim=message)

    async def _locked_claim(self, message: OutboxDelivery) -> OutboxMessage:
        if (
            message.organization_id != self._context.organization_id
            or message.workspace_id != self._context.workspace_id
        ):
            raise OutboxStateError("outbox claim is outside repository tenant scope")
        row = await self._session.scalar(
            select(OutboxMessage)
            .where(
                OutboxMessage.id == message.id,
                OutboxMessage.organization_id == message.organization_id,
                OutboxMessage.workspace_id == message.workspace_id,
                OutboxMessage.status == "processing",
                OutboxMessage.claimed_by == message.claimed_by,
                OutboxMessage.claim_token == message.claim_token,
            )
            .with_for_update()
        )
        if row is None:
            raise OutboxStateError("outbox claim is stale, missing or owned by another worker")
        return row


async def dispatch_claimed(
    repository: OutboxRepository,
    messages: Sequence[OutboxDelivery],
    sink: OutboxSink,
    *,
    now: datetime,
    max_attempts: int = 5,
    base_delay: timedelta = timedelta(seconds=1),
) -> None:
    """Dispatch a fenced batch using at-least-once, stable-id sink semantics."""
    dispatch_time = ensure_utc(now)
    for message in messages:
        canonical_message = await repository.validate_claim(message)
        try:
            await sink.publish(canonical_message)
        except Exception as exc:
            await repository.mark_failed(
                canonical_message,
                error=str(exc),
                now=dispatch_time,
                max_attempts=max_attempts,
                base_delay=base_delay,
            )
        else:
            await repository.mark_delivered(canonical_message)


def _delivery(
    row: OutboxMessage, *, claim: OutboxDelivery | None = None
) -> OutboxDelivery:
    claimed_by = row.claimed_by if row.claimed_by is not None else claim.claimed_by if claim else None
    claim_token = (
        row.claim_token if row.claim_token is not None else claim.claim_token if claim else None
    )
    lease_expires_at = (
        row.lease_expires_at
        if row.lease_expires_at is not None
        else claim.lease_expires_at
        if claim
        else None
    )
    if claimed_by is None or claim_token is None or lease_expires_at is None:
        raise OutboxStateError("outbox delivery requires a fenced claim")
    return OutboxDelivery(
        id=row.id,
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        topic=row.topic,
        event_key=row.event_key,
        payload=deepcopy(row.payload),
        status=row.status,
        attempts=row.attempts,
        available_at=row.available_at,
        claimed_by=claimed_by,
        claim_token=claim_token,
        lease_expires_at=lease_expires_at,
        dead_lettered_at=row.dead_lettered_at,
    )
