"""S2 runtime: canonical event persistence binding for RuntimeEvent。

事实源：design doc §4.3、S2-T3/T4 plan、specs/s2-agent-runtime.md §3（PG event 为真相）。

RuntimeEvent 经 EventCommand 走 S0 的 CanonicalUnitOfWork 落入 canonical_events：
digest 链、projection、audit、outbox 与既有 eval 事件共用同一条写入路径（不建第二套
事件存储）。schema 以事件类本身注册——append 用 strict 校验（python-mode dump 保持
UUID/datetime 实例），读取用 lax 反序列化从 JSONB 还原。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.envelope import SchemaRegistry
from zhiwei.persistence.events import EventCommand
from zhiwei.persistence.models import CanonicalEvent
from zhiwei.persistence.tenant import TenantContext
from zhiwei.persistence.unit_of_work import CanonicalUnitOfWork
from zhiwei.runtime import events as runtime_events
from zhiwei.runtime.events import (
    RunCancelled,
    RunCompleted,
    RunCreated,
    RunFailed,
    RunPaused,
    RunResumed,
    RunStarted,
    RuntimeEvent,
    TaskCompleted,
    TaskFailed,
    TaskScheduled,
    TaskSkipped,
    TaskStarted,
)
from zhiwei.runtime.reducer import RunState, reduce

_EVENT_SCHEMA_VERSION = 1

_EVENT_TYPES: dict[type[RuntimeEvent], str] = {
    RunCreated: "runtime.run.created",
    RunStarted: "runtime.run.started",
    RunCompleted: "runtime.run.completed",
    RunFailed: "runtime.run.failed",
    RunCancelled: "runtime.run.cancelled",
    RunPaused: "runtime.run.paused",
    RunResumed: "runtime.run.resumed",
    TaskScheduled: "runtime.task.scheduled",
    TaskStarted: "runtime.task.started",
    TaskCompleted: "runtime.task.completed",
    TaskFailed: "runtime.task.failed",
    TaskSkipped: "runtime.task.skipped",
    runtime_events.AttemptCreated: "runtime.attempt.created",
    runtime_events.AttemptCommitted: "runtime.attempt.committed",
    runtime_events.AttemptAborted: "runtime.attempt.aborted",
    runtime_events.ConflictDetected: "runtime.conflict.detected",
}

_BY_EVENT_TYPE: dict[str, type[RuntimeEvent]] = {v: k for k, v in _EVENT_TYPES.items()}


class RuntimeEventSchemaError(LookupError):
    """Raised when an event type has no registered canonical schema."""


def runtime_schema_registry() -> SchemaRegistry:
    """Registry mapping runtime event types to canonical payload schemas."""
    registry = SchemaRegistry()
    for event_cls, event_type in _EVENT_TYPES.items():
        registry.register(event_type, _EVENT_SCHEMA_VERSION, event_cls)
    return registry


def event_type_for(event: RuntimeEvent) -> str:
    """Canonical event_type string for a RuntimeEvent instance."""
    for event_cls, event_type in _EVENT_TYPES.items():
        if isinstance(event, event_cls):
            return event_type
    raise RuntimeEventSchemaError(f"unregistered runtime event: {type(event).__name__}")


def runtime_event_to_command(
    event: RuntimeEvent, *, actor_ref: str, idempotency_key: str
) -> EventCommand:
    """Convert a RuntimeEvent into a canonical EventCommand.

    payload 用 python-mode dump：UUID/datetime 保持实例，strict schema 校验通过后由
    validate_event_command 统一转 JSON 存储。task_id 是 string 节点 id，canonical_events
    的 task_id 列是 UUID，故节点 id 只随 payload 落库；attempt_id 列可索引则填充。
    """
    return EventCommand(
        run_id=event.run_id,
        event_type=event_type_for(event),
        payload_schema_version=_EVENT_SCHEMA_VERSION,
        payload=event.model_dump(mode="python"),
        actor_ref=actor_ref,
        idempotency_key=idempotency_key,
        attempt_id=getattr(event, "attempt_id", None),
    )


class RuntimeEventStore:
    """Append/load runtime events on the S0 canonical event chain."""

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        self._session = session
        self._context = context
        self._uow = CanonicalUnitOfWork(
            session, context, schema_registry=runtime_schema_registry()
        )

    async def append(
        self, event: RuntimeEvent, *, actor_ref: str, idempotency_key: str
    ) -> bool:
        """Append one runtime event; return True if created, False if already present.

        幂等键已存在且内容一致 → 0 行写入（activity 重试语义）；内容不一致由
        CanonicalUnitOfWork 以 EventIdempotencyConflict 拒绝（fail closed）。
        """
        command = runtime_event_to_command(
            event, actor_ref=actor_ref, idempotency_key=idempotency_key
        )
        result = await self._uow.append_event(command)
        return result.created

    async def has_event(self, run_id, idempotency_key: str) -> bool:
        """Check whether a logical event (by idempotency key) is already committed."""
        existing = await self._session.scalar(
            select(CanonicalEvent).where(
                CanonicalEvent.organization_id == self._context.organization_id,
                CanonicalEvent.workspace_id == self._context.workspace_id,
                CanonicalEvent.run_id == run_id,
                CanonicalEvent.idempotency_key == idempotency_key,
            )
        )
        return existing is not None

    async def load_events(self, run_id) -> list[RuntimeEvent]:
        """Load and decode all committed runtime events for a run, in sequence order."""
        rows = (
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
            )
            .all()
        )
        decoded: list[RuntimeEvent] = []
        for row in rows:
            decoded.append(_decode_event(row.event_type, row.payload))
        return decoded

    async def reduce_state(self, run_id) -> RunState:
        """Replay committed events into the canonical Run projection."""
        return reduce(await self.load_events(run_id))


def _decode_event(event_type: str, payload: dict[str, Any]) -> RuntimeEvent:
    try:
        event_cls = _BY_EVENT_TYPE[event_type]
    except KeyError as exc:
        raise RuntimeEventSchemaError(
            f"unknown canonical event type: {event_type!r}"
        ) from exc
    return event_cls.model_validate(payload)
