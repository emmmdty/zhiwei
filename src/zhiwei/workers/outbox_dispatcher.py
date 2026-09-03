"""S2 workers: outbox dispatcher that polls and dispatches to Temporal/streams.

Concrete implementation that polls an outbox repository and dispatches
messages by topic:
- `runtime.command` → WorkflowSignalSender（Temporal durable shell）
- 其他 topic（canonical.event.committed 等）→ OutboxSink（Redis/SSE 增量通道；
  丢失只影响增量，REST projection + cursor 可恢复）

Handles crash recovery, bounded retry with dead-letter, and observable state
transitions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.persistence.outbox import OutboxDelivery, OutboxSink
from zhiwei.persistence.outbox import OutboxRepository as PGOutboxRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.outbox_handlers import (
    HandleResult,
    OutboxSignalHandler,
)
from zhiwei.runtime.run_commands import RUNTIME_COMMAND_TOPIC

logger = logging.getLogger(__name__)


class DispatcherState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class OutboxRepository(Protocol):
    """Port for claiming and transitioning outbox messages."""

    async def claim_batch(
        self,
        *,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> list[OutboxDelivery]: ...

    async def mark_delivered(self, message: OutboxDelivery) -> None: ...

    async def mark_failed(
        self,
        message: OutboxDelivery,
        *,
        error: str,
        now: datetime,
        max_attempts: int,
        base_delay: timedelta,
    ) -> OutboxDelivery: ...


class SessionOutboxRepository:
    """PG-backed OutboxRepository opening one tenant transaction per operation.

    dispatcher 是长驻进程：每个操作独立事务（claim 的 fencing 由 claim_token 承担），
    不跨 poll 持有会话。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        context: TenantContext,
    ) -> None:
        self._sessions = session_factory
        self._context = context

    async def claim_batch(
        self,
        *,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> list[OutboxDelivery]:
        async with tenant_session(self._sessions, self._context) as session:
            repository = PGOutboxRepository(session, self._context)
            return await repository.claim_batch(
                worker_id=worker_id, limit=limit, now=now, lease_duration=lease_duration
            )

    async def mark_delivered(self, message: OutboxDelivery) -> None:
        async with tenant_session(self._sessions, self._context) as session:
            repository = PGOutboxRepository(session, self._context)
            await repository.mark_delivered(message)

    async def mark_failed(
        self,
        message: OutboxDelivery,
        *,
        error: str,
        now: datetime,
        max_attempts: int,
        base_delay: timedelta,
    ) -> OutboxDelivery:
        async with tenant_session(self._sessions, self._context) as session:
            repository = PGOutboxRepository(session, self._context)
            return await repository.mark_failed(
                message,
                error=error,
                now=now,
                max_attempts=max_attempts,
                base_delay=base_delay,
            )


@dataclass
class DispatcherMetrics:
    """Observable metrics for the outbox dispatcher."""

    dispatched: int = 0
    failed: int = 0
    dead_lettered: int = 0
    duplicates: int = 0
    signal_before_worker: int = 0
    poison: int = 0
    events_published: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dispatched": self.dispatched,
            "failed": self.failed,
            "dead_lettered": self.dead_lettered,
            "duplicates": self.duplicates,
            "signal_before_worker": self.signal_before_worker,
            "poison": self.poison,
            "events_published": self.events_published,
            "errors": self.errors,
        }


@dataclass(frozen=True)
class OutboxDispatcherConfig:
    """Configuration for the outbox dispatcher."""

    worker_id: str = "outbox-dispatcher"
    poll_interval: timedelta = timedelta(seconds=1)
    batch_limit: int = 10
    max_attempts: int = 5
    base_delay: timedelta = timedelta(seconds=1)
    lease_duration: timedelta = timedelta(seconds=30)


class OutboxDispatcher:
    """Polls the outbox table, dispatches commands to Temporal and events to sinks.

    Handles:
    - Crash recovery (leased messages are re-claimed after lease expiry)
    - Bounded retry with exponential backoff
    - Dead-letter after max_attempts exceeded
    - Observable state transitions via metrics
    - Idempotent dispatch via deterministic workflow ids / stable message ids
    """

    def __init__(
        self,
        repository: OutboxRepository,
        handler: OutboxSignalHandler,
        config: OutboxDispatcherConfig | None = None,
        *,
        event_sink: OutboxSink | None = None,
    ) -> None:
        self._repository = repository
        self._handler = handler
        self._config = config or OutboxDispatcherConfig()
        self._event_sink = event_sink
        self._state = DispatcherState.IDLE
        self._metrics = DispatcherMetrics()

    @property
    def state(self) -> DispatcherState:
        return self._state

    @property
    def metrics(self) -> DispatcherMetrics:
        return self._metrics

    async def poll_once(self) -> list[HandleResult]:
        """Poll one batch from the outbox and dispatch each message.

        Returns the list of HandleResults for command-topic messages.
        On crash recovery, leased messages from a previous run are re-claimed.
        """
        now = datetime.now(tz=UTC)
        messages = await self._repository.claim_batch(
            worker_id=self._config.worker_id,
            limit=self._config.batch_limit,
            now=now,
            lease_duration=self._config.lease_duration,
        )
        results: list[HandleResult] = []
        for message in messages:
            if message.topic == RUNTIME_COMMAND_TOPIC:
                results.append(await self._dispatch_command(message))
            else:
                await self._dispatch_event(message)
        return results

    async def _dispatch_command(self, message: OutboxDelivery) -> HandleResult:
        """Dispatch a runtime command message, handling success/failure."""
        try:
            result = await self._handler.handle(message)
        except Exception as exc:
            self._metrics.errors += 1
            logger.exception("handler error for message %s", message.id)
            await self._mark_failed(message, str(exc))
            return HandleResult(status="error", error=str(exc))

        if result.status == "duplicate":
            self._metrics.duplicates += 1
            await self._repository.mark_delivered(message)
            return result

        if result.status == "poison":
            self._metrics.poison += 1
            await self._mark_failed(message, result.error or "poison message")
            return result

        if result.status == "signal_before_worker":
            self._metrics.signal_before_worker += 1
            await self._mark_failed(message, "signal before worker")
            return result

        if result.status == "delivered":
            self._metrics.dispatched += 1
            await self._repository.mark_delivered(message)
            return result

        self._metrics.errors += 1
        await self._mark_failed(message, f"unknown status: {result.status}")
        return result

    async def _dispatch_event(self, message: OutboxDelivery) -> None:
        """Deliver a non-command outbox message to the stream sink (best effort).

        失败走有界重试 → dead-letter：增量通道丢失不影响真相（REST projection 可恢复）。
        """
        if self._event_sink is None:
            # 未配置增量通道：标记 delivered，避免 pending 堆积（部署时按需接 Redis）
            await self._repository.mark_delivered(message)
            return
        try:
            await self._event_sink.publish(message)
        except Exception as exc:
            self._metrics.errors += 1
            await self._mark_failed(message, str(exc))
        else:
            self._metrics.events_published += 1
            await self._repository.mark_delivered(message)

    async def _mark_failed(self, message: OutboxDelivery, error: str) -> None:
        """Mark a message as failed with bounded retry / dead-letter."""
        now = datetime.now(tz=UTC)
        try:
            updated = await self._repository.mark_failed(
                message,
                error=error,
                now=now,
                max_attempts=self._config.max_attempts,
                base_delay=self._config.base_delay,
            )
            if updated.status == "dead_letter":
                self._metrics.dead_lettered += 1
            else:
                self._metrics.failed += 1
        except Exception:
            self._metrics.errors += 1
            logger.exception("failed to mark message %s as failed", message.id)

    def start(self) -> None:
        """Transition to RUNNING state."""
        self._state = DispatcherState.RUNNING

    def stop(self) -> None:
        """Transition to STOPPING then STOPPED."""
        self._state = DispatcherState.STOPPING
        self._state = DispatcherState.STOPPED
