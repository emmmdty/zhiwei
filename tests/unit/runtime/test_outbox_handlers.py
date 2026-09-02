"""S2-T4 RED: Outbox handlers — bridge outbox messages to workflow signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.persistence.outbox import OutboxDelivery
from zhiwei.runtime.commands import (
    CancelRun,
    PauseRun,
    ResumeRun,
    SignalRun,
    StartRun,
)
from zhiwei.runtime.outbox_handlers import (
    CommandParseError,
    OutboxHandlerError,
    OutboxSignalHandler,
    WorkflowNotFoundError,
)
from zhiwei.workers.outbox_dispatcher import (
    DispatcherMetrics,
    DispatcherState,
    OutboxDispatcher,
    OutboxDispatcherConfig,
)


class StubWorkflowSignalSender:
    """Records signals sent via the WorkflowSignalSender port."""

    def __init__(self) -> None:
        self.signals: list[dict[str, Any]] = []
        self.failures: dict[tuple[str, str], Exception] = {}

    def send_signal(
        self,
        workflow_id: str,
        signal_name: str,
        payload: dict[str, Any],
    ) -> None:
        key = (workflow_id, signal_name)
        if key in self.failures:
            raise self.failures[key]
        self.signals.append({
            "workflow_id": workflow_id,
            "signal_name": signal_name,
            "payload": payload,
        })

    def start_workflow(
        self,
        workflow_id: str,
        workflow_type: str,
        input: dict[str, Any],
    ) -> None:
        key = (workflow_id, "start_workflow")
        if key in self.failures:
            raise self.failures[key]
        self.signals.append({
            "workflow_id": workflow_id,
            "signal_name": "start_workflow",
            "payload": input,
        })

    def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any,
    ) -> None:
        self.send_signal(workflow_id, signal_name, payload if isinstance(payload, dict) else {})


class StubOutboxMessageTracker:
    """Tracks outbox message state transitions."""

    def __init__(self) -> None:
        self.delivered: list[UUID] = []
        self.failed: list[tuple[UUID, str]] = []
        self.dead_letters: list[UUID] = []

    def mark_delivered(self, message_id: UUID) -> None:
        self.delivered.append(message_id)

    def mark_failed(self, message_id: UUID, error: str) -> None:
        self.failed.append((message_id, error))

    def mark_dead_letter(self, message_id: UUID) -> None:
        self.dead_letters.append(message_id)


def _make_delivery(
    *,
    topic: str = "runtime.commands",
    event_key: str = "start_run",
    payload: dict[str, Any] | None = None,
    message_id: UUID | None = None,
    attempts: int = 0,
) -> OutboxDelivery:
    now = datetime.now(tz=UTC)
    return OutboxDelivery(
        id=message_id or new_id(),
        organization_id=new_id(),
        workspace_id=None,
        topic=topic,
        event_key=event_key,
        payload=payload or {"run_id": str(new_id()), "kind": "start_run", "task_queue": "q"},
        status="processing",
        attempts=attempts,
        available_at=now,
        claimed_by="dispatcher-1",
        claim_token=new_id(),
        lease_expires_at=now + timedelta(seconds=30),
    )


def _start_run_payload(*, run_id: UUID | None = None) -> dict[str, Any]:
    return {
        "run_id": str(run_id or new_id()),
        "kind": "start_run",
        "task_queue": "q",
    }


def _cancel_run_payload(*, run_id: UUID | None = None) -> dict[str, Any]:
    return {
        "run_id": str(run_id or new_id()),
        "kind": "cancel_run",
        "reason": "test",
    }


def _signal_run_payload(*, run_id: UUID | None = None) -> dict[str, Any]:
    return {
        "run_id": str(run_id or new_id()),
        "kind": "signal_run",
        "signal_name": "heartbeat",
        "payload": {"ts": "now"},
    }


def _pause_run_payload(*, run_id: UUID | None = None) -> dict[str, Any]:
    return {
        "run_id": str(run_id or new_id()),
        "kind": "pause_run",
    }


def _resume_run_payload(*, run_id: UUID | None = None) -> dict[str, Any]:
    return {
        "run_id": str(run_id or new_id()),
        "kind": "resume_run",
    }


class TestCommandParsing:
    def test_parse_start_run(self) -> None:
        run_id = new_id()
        payload = _start_run_payload(run_id=run_id)
        cmd = OutboxSignalHandler.parse_command("start_run", payload)
        assert isinstance(cmd, StartRun)
        assert cmd.run_id == run_id

    def test_parse_cancel_run(self) -> None:
        run_id = new_id()
        payload = _cancel_run_payload(run_id=run_id)
        cmd = OutboxSignalHandler.parse_command("cancel_run", payload)
        assert isinstance(cmd, CancelRun)
        assert cmd.run_id == run_id

    def test_parse_signal_run(self) -> None:
        run_id = new_id()
        payload = _signal_run_payload(run_id=run_id)
        cmd = OutboxSignalHandler.parse_command("signal_run", payload)
        assert isinstance(cmd, SignalRun)
        assert cmd.run_id == run_id

    def test_parse_pause_run(self) -> None:
        run_id = new_id()
        payload = _pause_run_payload(run_id=run_id)
        cmd = OutboxSignalHandler.parse_command("pause_run", payload)
        assert isinstance(cmd, PauseRun)
        assert cmd.run_id == run_id

    def test_parse_resume_run(self) -> None:
        run_id = new_id()
        payload = _resume_run_payload(run_id=run_id)
        cmd = OutboxSignalHandler.parse_command("resume_run", payload)
        assert isinstance(cmd, ResumeRun)
        assert cmd.run_id == run_id

    def test_parse_unknown_event_key_raises(self) -> None:
        with pytest.raises(CommandParseError, match="unknown"):
            OutboxSignalHandler.parse_command("unknown_type", {})

    def test_parse_malformed_payload_raises(self) -> None:
        with pytest.raises(CommandParseError):
            OutboxSignalHandler.parse_command("start_run", {"not_a_uuid": True})


class TestHandleStartRun:
    def test_start_run_sends_start_workflow(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="start_run",
            payload=_start_run_payload(run_id=run_id),
        )
        handler.handle(delivery)
        assert len(sender.signals) == 1
        sig = sender.signals[0]
        assert sig["workflow_id"] == f"run-{run_id}"
        assert sig["signal_name"] == "start_workflow"

    def test_start_run_workflow_failure_raises(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        sender.failures[(f"run-{run_id}", "start_workflow")] = RuntimeError("temporal down")
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="start_run",
            payload=_start_run_payload(run_id=run_id),
        )
        with pytest.raises(OutboxHandlerError, match="temporal down"):
            handler.handle(delivery)


class TestHandleCancelRun:
    def test_cancel_run_sends_signal(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="cancel_run",
            payload=_cancel_run_payload(run_id=run_id),
        )
        handler.handle(delivery)
        assert len(sender.signals) == 1
        sig = sender.signals[0]
        assert sig["workflow_id"] == f"run-{run_id}"
        assert sig["signal_name"] == "cancel"


class TestHandleSignalRun:
    def test_signal_run_sends_signal_with_payload(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="signal_run",
            payload=_signal_run_payload(run_id=run_id),
        )
        handler.handle(delivery)
        assert len(sender.signals) == 1
        sig = sender.signals[0]
        assert sig["workflow_id"] == f"run-{run_id}"
        assert sig["signal_name"] == "heartbeat"


class TestHandlePauseResume:
    def test_pause_run_sends_signal(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="pause_run",
            payload=_pause_run_payload(run_id=run_id),
        )
        handler.handle(delivery)
        assert len(sender.signals) == 1
        assert sender.signals[0]["signal_name"] == "pause"

    def test_resume_run_sends_signal(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="resume_run",
            payload=_resume_run_payload(run_id=run_id),
        )
        handler.handle(delivery)
        assert len(sender.signals) == 1
        assert sender.signals[0]["signal_name"] == "resume"


class TestDuplicateDispatch:
    def test_duplicate_start_run_is_idempotent(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="start_run",
            payload=_start_run_payload(run_id=run_id),
        )
        handler.handle(delivery)
        handler.handle(delivery)
        assert len(sender.signals) == 1

    def test_duplicate_cancel_run_is_idempotent(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="cancel_run",
            payload=_cancel_run_payload(run_id=run_id),
        )
        handler.handle(delivery)
        handler.handle(delivery)
        assert len(sender.signals) == 1


class TestSignalBeforeWorker:
    def test_signal_before_start_returns_pending(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        sender.failures[(f"run-{run_id}", "cancel")] = WorkflowNotFoundError(
            f"workflow not found: run-{run_id}"
        )
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="cancel_run",
            payload=_cancel_run_payload(run_id=run_id),
        )
        result = handler.handle(delivery)
        assert result.status == "signal_before_worker"


class TestPoisonMessage:
    def test_unparseable_payload_is_poison(self) -> None:
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        delivery = _make_delivery(
            event_key="start_run",
            payload={"garbage": True},
        )
        result = handler.handle(delivery)
        assert result.status == "poison"
        assert result.error is not None


class TestWorkflowIdDeterminism:
    def test_all_commands_use_run_id_based_workflow_id(self) -> None:
        run_id = new_id()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)

        for key, payload_fn in [
            ("start_run", _start_run_payload),
            ("cancel_run", _cancel_run_payload),
            ("signal_run", _signal_run_payload),
            ("pause_run", _pause_run_payload),
            ("resume_run", _resume_run_payload),
        ]:
            delivery = _make_delivery(event_key=key, payload=payload_fn(run_id=run_id))
            handler.handle(delivery)
            expected_workflow_id = f"run-{run_id}"
            assert sender.signals[-1]["workflow_id"] == expected_workflow_id


# ---------------------------------------------------------------------------
# Dispatcher tests
# ---------------------------------------------------------------------------


class StubOutboxRepository:
    """In-memory outbox repository for testing the dispatcher."""

    def __init__(self) -> None:
        self._messages: list[OutboxDelivery] = []
        self.delivered: list[UUID] = []
        self.failed: list[tuple[UUID, str]] = []
        self.dead_letters: list[UUID] = []

    def enqueue(self, *messages: OutboxDelivery) -> None:
        self._messages.extend(messages)

    async def claim_batch(
        self,
        *,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> list[OutboxDelivery]:
        claimed = self._messages[:limit]
        self._messages = self._messages[limit:]
        return claimed

    async def mark_delivered(self, message: OutboxDelivery) -> None:
        self.delivered.append(message.id)

    async def mark_failed(
        self,
        message: OutboxDelivery,
        *,
        error: str,
        now: datetime,
        max_attempts: int,
        base_delay: timedelta,
    ) -> OutboxDelivery:
        new_attempts = message.attempts + 1
        pending_at = now + base_delay * (2 ** (new_attempts - 1))
        if new_attempts >= max_attempts:
            self.dead_letters.append(message.id)
            return OutboxDelivery(
                id=message.id,
                organization_id=message.organization_id,
                workspace_id=message.workspace_id,
                topic=message.topic,
                event_key=message.event_key,
                payload=message.payload,
                status="dead_letter",
                attempts=new_attempts,
                available_at=pending_at,
                claimed_by="dispatcher",
                claim_token=new_id(),
                lease_expires_at=pending_at,
                dead_lettered_at=now,
            )
        self.failed.append((message.id, error))
        return OutboxDelivery(
            id=message.id,
            organization_id=message.organization_id,
            workspace_id=message.workspace_id,
            topic=message.topic,
            event_key=message.event_key,
            payload=message.payload,
            status="pending",
            attempts=new_attempts,
            available_at=pending_at,
            claimed_by="dispatcher",
            claim_token=new_id(),
            lease_expires_at=pending_at,
        )


def _dispatcher_config(**kwargs: Any) -> OutboxDispatcherConfig:
    defaults = {
        "worker_id": "test-dispatcher",
        "poll_interval": timedelta(milliseconds=10),
        "batch_limit": 10,
        "max_attempts": 3,
        "base_delay": timedelta(seconds=1),
        "lease_duration": timedelta(seconds=30),
    }
    defaults.update(kwargs)
    return OutboxDispatcherConfig(**defaults)


@pytest.mark.asyncio
class TestDispatcherCrashRecovery:
    async def test_claim_batch_returns_leased_messages(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        config = _dispatcher_config()
        dispatcher = OutboxDispatcher(repository=repo, handler=handler, config=config)

        run_id = new_id()
        msg = _make_delivery(
            event_key="start_run",
            payload=_start_run_payload(run_id=run_id),
        )
        repo.enqueue(msg)
        results = await dispatcher.poll_once()
        assert len(results) == 1
        assert results[0].status == "delivered"
        assert msg.id in repo.delivered

    async def test_empty_poll_returns_no_results(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        config = _dispatcher_config()
        dispatcher = OutboxDispatcher(repository=repo, handler=handler, config=config)

        results = await dispatcher.poll_once()
        assert results == []


@pytest.mark.asyncio
class TestDispatcherPoisonMessage:
    async def test_poison_message_dead_letters(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        config = _dispatcher_config(max_attempts=1)
        dispatcher = OutboxDispatcher(repository=repo, handler=handler, config=config)

        msg = _make_delivery(
            event_key="start_run",
            payload={"garbage": True},
        )
        repo.enqueue(msg)
        results = await dispatcher.poll_once()
        assert len(results) == 1
        assert results[0].status == "poison"
        assert msg.id in repo.dead_letters

    async def test_poison_increments_poison_metric(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        config = _dispatcher_config(max_attempts=3)
        dispatcher = OutboxDispatcher(repository=repo, handler=handler, config=config)

        msg = _make_delivery(
            event_key="start_run",
            payload={"garbage": True},
        )
        repo.enqueue(msg)
        await dispatcher.poll_once()
        assert dispatcher.metrics.poison == 1


@pytest.mark.asyncio
class TestDispatcherMetrics:
    async def test_dispatched_increments_on_success(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        config = _dispatcher_config()
        dispatcher = OutboxDispatcher(repository=repo, handler=handler, config=config)

        run_id = new_id()
        msg = _make_delivery(
            event_key="start_run",
            payload=_start_run_payload(run_id=run_id),
        )
        repo.enqueue(msg)
        await dispatcher.poll_once()
        assert dispatcher.metrics.dispatched == 1

    async def test_handler_error_increments_error_metric(self) -> None:
        run_id = new_id()
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        sender.failures[(f"run-{run_id}", "start_workflow")] = RuntimeError("temporal down")
        handler = OutboxSignalHandler(sender=sender)
        config = _dispatcher_config()
        dispatcher = OutboxDispatcher(repository=repo, handler=handler, config=config)

        msg = _make_delivery(
            event_key="start_run",
            payload=_start_run_payload(run_id=run_id),
        )
        repo.enqueue(msg)
        results = await dispatcher.poll_once()
        assert len(results) == 1
        assert results[0].status == "error"

    async def test_metrics_as_dict(self) -> None:
        metrics = DispatcherMetrics(dispatched=5, failed=2)
        d = metrics.as_dict()
        assert d["dispatched"] == 5
        assert d["failed"] == 2


class TestDispatcherState:
    def test_initial_state_is_idle(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        dispatcher = OutboxDispatcher(repository=repo, handler=handler)
        assert dispatcher.state == DispatcherState.IDLE

    def test_start_transitions_to_running(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        dispatcher = OutboxDispatcher(repository=repo, handler=handler)
        dispatcher.start()
        assert dispatcher.state == DispatcherState.RUNNING

    def test_stop_transitions_to_stopped(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        dispatcher = OutboxDispatcher(repository=repo, handler=handler)
        dispatcher.start()
        dispatcher.stop()
        assert dispatcher.state == DispatcherState.STOPPED


@pytest.mark.asyncio
class TestDispatcherBoundedRetry:
    async def test_failed_message_requeued(self) -> None:
        run_id = new_id()
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        sender.failures[(f"run-{run_id}", "cancel")] = WorkflowNotFoundError("not found")
        handler = OutboxSignalHandler(sender=sender)
        config = _dispatcher_config(max_attempts=3)
        dispatcher = OutboxDispatcher(repository=repo, handler=handler, config=config)

        msg = _make_delivery(
            event_key="cancel_run",
            payload=_cancel_run_payload(run_id=run_id),
        )
        repo.enqueue(msg)
        results = await dispatcher.poll_once()
        assert len(results) == 1
        assert results[0].status == "signal_before_worker"
        assert (msg.id, "signal before worker") in repo.failed

    async def test_max_attempts_dead_letters(self) -> None:
        repo = StubOutboxRepository()
        sender = StubWorkflowSignalSender()
        handler = OutboxSignalHandler(sender=sender)
        config = _dispatcher_config(max_attempts=1)
        dispatcher = OutboxDispatcher(repository=repo, handler=handler, config=config)

        msg = _make_delivery(
            event_key="start_run",
            payload={"garbage": True},
        )
        repo.enqueue(msg)
        await dispatcher.poll_once()
        assert msg.id in repo.dead_letters
