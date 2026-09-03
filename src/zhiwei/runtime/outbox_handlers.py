"""S2 runtime: outbox handlers that bridge outbox messages to workflow signals。

事实源：specs/s2-agent-runtime.md §4、S2-T4 plan、ADR（outbox 桥接）。

Handlers process OutboxDelivery messages, parse them into typed commands,
and dispatch via an async WorkflowSignalSender port（真实绑定见
zhiwei/workers/temporal_sender.py；本模块保持 domain 纯度，不导入 Temporal SDK）。

幂等来自 deterministic workflow id + 同 command_event_id 的信号去重，不依赖进程内
内存状态（dispatcher 重启后重复投递仍然安全）。
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


class WorkflowDuplicateStart(OutboxHandlerError):
    """Raised when a start command targets an already-running workflow."""


class WorkflowSignalSender(Protocol):
    """Port for starting/signaling workflows (async: binds a real Temporal client)."""

    async def start_workflow(
        self,
        *,
        workflow_id: str,
        workflow_type: str,
        input: dict[str, Any],
        organization_id: UUID,
        workspace_id: UUID,
    ) -> None: ...

    async def signal_workflow(
        self,
        *,
        workflow_id: str,
        signal_name: str,
        payload: dict[str, Any],
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
    CommandKind.PAUSE_RUN: "pause",
    CommandKind.RESUME_RUN: "resume",
}


class OutboxSignalHandler:
    """Processes outbox deliveries and dispatches to the workflow signal sender.

    Handles:
    - Parsing outbox payloads into typed commands
    - Duplicate start (already-running workflow) is a delivered no-op
    - Signal-before-worker scenarios (workflow not started yet → retryable)
    - Poison message detection (unparseable payloads → dead-letter)
    """

    def __init__(self, sender: WorkflowSignalSender) -> None:
        self._sender = sender

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

    async def handle(self, delivery: OutboxDelivery) -> HandleResult:
        """Process a single outbox delivery.

        Returns HandleResult with status:
        - 'delivered': command dispatched successfully (or duplicate start)
        - 'signal_before_worker': workflow not started yet, retry later
        - 'poison': unparseable payload, should be dead-lettered
        """
        try:
            command = self.parse_command(delivery.event_key, delivery.payload)
        except CommandParseError as exc:
            return HandleResult(status="poison", error=str(exc))

        try:
            await self._dispatch(delivery, command)
        except WorkflowNotFoundError:
            return HandleResult(command=command, status="signal_before_worker")
        except WorkflowDuplicateStart:
            # deterministic workflow id 下的重复 start = 已投递（幂等 no-op）
            return HandleResult(command=command, status="delivered")
        except OutboxHandlerError:
            raise
        except Exception as exc:
            raise OutboxHandlerError(str(exc)) from exc

        return HandleResult(command=command, status="delivered")

    async def _dispatch(self, delivery: OutboxDelivery, command: CommandBase) -> None:
        """Dispatch a parsed command to the appropriate workflow action."""
        if delivery.organization_id is None or delivery.workspace_id is None:
            raise OutboxHandlerError("runtime command delivery requires tenant scope")

        if isinstance(command, StartRun):
            if not command.graph:
                raise OutboxHandlerError("start_run command requires a graph payload")
            try:
                await self._sender.start_workflow(
                    workflow_id=command.workflow_id,
                    workflow_type="agent-run",
                    input={
                        "graph": command.graph,
                        "task_queue": command.task_queue,
                        "max_task_attempts": command.max_attempts,
                    },
                    organization_id=delivery.organization_id,
                    workspace_id=delivery.workspace_id,
                )
            except WorkflowNotFoundError:
                raise
            except WorkflowDuplicateStart:
                raise
            except Exception as exc:
                raise OutboxHandlerError(f"failed to start workflow: {exc}") from exc
            return

        if isinstance(command, SignalRun):
            signal_name = command.signal_name
        else:
            signal_name = _SIGNAL_NAME_MAP.get(command.kind)
            if signal_name is None:
                raise OutboxHandlerError(f"no signal mapping for {command.kind}")

        payload: dict[str, Any] = {"command_event_id": str(command.event_id)}
        if isinstance(command, SignalRun):
            payload.update(command.payload)
        elif isinstance(command, (CancelRun, PauseRun)) and command.reason:
            payload["reason"] = command.reason

        try:
            await self._sender.signal_workflow(
                workflow_id=command.workflow_id,
                signal_name=signal_name,
                payload=payload,
            )
        except WorkflowNotFoundError:
            raise
        except Exception as exc:
            raise OutboxHandlerError(f"failed to signal workflow: {exc}") from exc
