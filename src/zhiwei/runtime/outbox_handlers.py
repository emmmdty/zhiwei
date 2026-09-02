"""S2 runtime: outbox handlers that bridge outbox messages to workflow signals.

Handlers process OutboxDelivery messages, parse them into typed commands,
and dispatch via a WorkflowSignalSender port. No Temporal SDK dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from zhiwei.persistence.outbox import OutboxDelivery
from zhiwei.runtime.commands import (
    CancelRun,
    CommandBase,
    CommandKind,
    CommandUnion,
    PauseRun,
    ResumeRun,
    SignalRun,
    StartRun,
)


class CommandParseError(ValueError):
    """Raised when an outbox payload cannot be parsed into a typed command."""


class OutboxHandlerError(RuntimeError):
    """Raised when an outbox handler fails to process a message."""


class WorkflowNotFoundError(OutboxHandlerError):
    """Raised when the target workflow has not started yet."""


class WorkflowSignalSender(Protocol):
    """Port for sending signals to workflow instances."""

    def start_workflow(
        self,
        workflow_id: str,
        workflow_type: str,
        input: dict[str, Any],
    ) -> None: ...

    def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any,
    ) -> None: ...


@dataclass(frozen=True)
class HandleResult:
    """Result of processing an outbox delivery."""

    command: CommandBase | None = None
    status: str = "delivered"
    error: str | None = None


_COMMAND_KIND_MAP: dict[str, type[CommandUnion]] = {
    CommandKind.START_RUN.value: StartRun,
    CommandKind.CANCEL_RUN.value: CancelRun,
    CommandKind.SIGNAL_RUN.value: SignalRun,
    CommandKind.PAUSE_RUN.value: PauseRun,
    CommandKind.RESUME_RUN.value: ResumeRun,
}

_SIGNAL_NAME_MAP: dict[CommandKind, str] = {
    CommandKind.CANCEL_RUN: "cancel",
    CommandKind.SIGNAL_RUN: "",  # uses signal_name from the command
    CommandKind.PAUSE_RUN: "pause",
    CommandKind.RESUME_RUN: "resume",
}


class OutboxSignalHandler:
    """Processes outbox deliveries and dispatches to the workflow signal sender.

    Handles:
    - Parsing outbox payloads into typed commands
    - Idempotent dispatch (duplicate deliveries are no-ops)
    - Signal-before-worker scenarios (workflow not yet started)
    - Poison message detection (unparseable payloads)
    """

    def __init__(self, sender: WorkflowSignalSender) -> None:
        self._sender = sender
        self._delivered: set[UUID] = set()

    @staticmethod
    def parse_command(event_key: str, payload: dict[str, Any]) -> CommandUnion:
        """Parse an outbox payload into a typed command."""
        command_cls = _COMMAND_KIND_MAP.get(event_key)
        if command_cls is None:
            raise CommandParseError(f"unknown event_key: {event_key!r}")
        try:
            return command_cls.model_validate(payload)
        except Exception as exc:
            raise CommandParseError(f"invalid payload for {event_key}: {exc}") from exc

    def handle(self, delivery: OutboxDelivery) -> HandleResult:
        """Process a single outbox delivery.

        Returns HandleResult with status:
        - 'delivered': command dispatched successfully
        - 'duplicate': delivery already processed (idempotent no-op)
        - 'signal_before_worker': workflow not started yet, signal dropped
        - 'poison': unparseable payload, should be dead-lettered
        """
        if delivery.id in self._delivered:
            return HandleResult(status="duplicate")

        try:
            command = self.parse_command(delivery.event_key, delivery.payload)
        except CommandParseError as exc:
            return HandleResult(status="poison", error=str(exc))

        try:
            self._dispatch(command)
        except WorkflowNotFoundError:
            return HandleResult(command=command, status="signal_before_worker")
        except OutboxHandlerError:
            raise
        except Exception as exc:
            raise OutboxHandlerError(str(exc)) from exc

        self._delivered.add(delivery.id)
        return HandleResult(command=command, status="delivered")

    def _dispatch(self, command: CommandBase) -> None:
        """Dispatch a parsed command to the appropriate workflow signal.

        Raises WorkflowNotFoundError if the workflow is not running.
        Raises OutboxHandlerError on other failures.
        """
        if isinstance(command, StartRun):
            try:
                self._sender.start_workflow(
                    workflow_id=command.workflow_id,
                    workflow_type="AgentRun",
                    input=command.model_dump(mode="json"),
                )
            except WorkflowNotFoundError:
                raise
            except Exception as exc:
                raise OutboxHandlerError(f"failed to start workflow: {exc}") from exc
            return

        signal_name = _SIGNAL_NAME_MAP.get(command.kind)
        if signal_name is None:
            raise OutboxHandlerError(f"no signal mapping for {command.kind}")

        if isinstance(command, SignalRun):
            signal_name = command.signal_name

        payload: dict[str, Any] = {}
        if isinstance(command, CancelRun):
            payload = {"reason": command.reason} if command.reason else {}
        elif isinstance(command, SignalRun):
            payload = command.payload

        try:
            self._sender.signal_workflow(
                workflow_id=command.workflow_id,
                signal_name=signal_name,
                payload=payload,
            )
        except WorkflowNotFoundError:
            raise
        except Exception as exc:
            raise OutboxHandlerError(f"failed to signal workflow: {exc}") from exc
