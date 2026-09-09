"""S2 runtime: typed commands that trigger workflow actions.

Commands are pure Pydantic models — no DB/Temporal imports.
Each command carries a run_id and derives a deterministic workflow_id for Temporal dispatch.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.identifiers import new_id


class CommandKind(StrEnum):
    START_RUN = "start_run"
    CANCEL_RUN = "cancel_run"
    SIGNAL_RUN = "signal_run"
    PAUSE_RUN = "pause_run"
    RESUME_RUN = "resume_run"


class CommandBase(BaseModel):
    """Base class for all outbox commands.

    Each command is frozen, carries a unique event_id for idempotency,
    and derives a deterministic workflow_id from its run_id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    kind: CommandKind
    event_id: UUID = Field(default_factory=new_id)

    @property
    def workflow_id(self) -> str:
        return f"run-{self.run_id}"


class StartRun(CommandBase):
    """Command to start a new agent run workflow."""

    kind: CommandKind = CommandKind.START_RUN
    task_queue: str
    max_attempts: int = Field(default=3, ge=1)
    graph: dict[str, Any] | None = None
    # workflow 编排参数随命令走（场景驱动的 CAN 阈值/超时必须能从命令侧传达，
    # 否则 eval 场景声称的契约在生产路径上被默认值静默替换）
    continue_as_new_after: int = Field(default=1000, ge=1)
    activity_timeout_seconds: int = Field(default=60, ge=1)
    # 触发 run 的 human principal（SoD 事实源：审批 requester 必须是真实主体，
    # 不得在 activity 内退化为常量——ADR-012 反例 1）。空值仅在 eval/内部路径允许。
    requested_by: str = ""
    # ADR-008 第②层：委托链（ Delegate 与 agent-as-tool 共用计数）。发布期
    # 环检测之外的运行时硬上界，命令提交侧 fail closed。
    delegation_chain: tuple[str, ...] = ()


class CancelRun(CommandBase):
    """Command to cancel a running workflow."""

    kind: CommandKind = CommandKind.CANCEL_RUN
    reason: str | None = None


class SignalRun(CommandBase):
    """Command to send an arbitrary signal to a running workflow."""

    kind: CommandKind = CommandKind.SIGNAL_RUN
    signal_name: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class PauseRun(CommandBase):
    """Command to pause a running workflow."""

    kind: CommandKind = CommandKind.PAUSE_RUN
    reason: str | None = None


class ResumeRun(CommandBase):
    """Command to resume a paused workflow."""

    kind: CommandKind = CommandKind.RESUME_RUN


CommandUnion = StartRun | CancelRun | SignalRun | PauseRun | ResumeRun
