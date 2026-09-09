"""S2 runtime: abstract TaskHandler base class。

事实源：design doc §4.3、S2-T2 plan。

Task handlers implement the actual execution logic for each task primitive type.
They receive typed input and produce typed output, with all side effects going through Activity ports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TaskInput(BaseModel):
    """Input to a task handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    attempt_id: UUID
    input_values: dict[str, Any] = {}


class TaskOutput(BaseModel):
    """Output from a task handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_values: dict[str, Any] = {}
    artifacts: list[dict[str, Any]] = []


class TaskHandler(ABC):
    """Abstract base class for task handlers.

    Each task primitive type has a corresponding handler that implements
    the execute method. Handlers receive typed input and produce typed output.
    """

    @property
    @abstractmethod
    def primitive_type(self) -> str:
        """The task primitive type this handler supports."""

    @property
    @abstractmethod
    def handler_version(self) -> int:
        """Version of this handler implementation."""

    @abstractmethod
    def execute(self, input: TaskInput) -> TaskOutput:
        """Execute the task with the given input and return output.

        Side effects must go through Activity ports (not direct DB/provider calls).
        """

    def validate_input(self, input: TaskInput) -> None:  # noqa: B027
        """Validate task input before execution. Override for custom validation."""

    def validate_output(self, output: TaskOutput) -> None:  # noqa: B027
        """Validate task output after execution. Override for custom validation."""


class EffectUnknownError(RuntimeError):
    """副作用状态未知的失败（spec §4 2026-09-03 增补）。

    抛出本类型的 handler 声明：副作用可能已发生（写入已发出但结果未确认）。
    重试会造成重复副作用——workflow 的重试决策必须拒绝自动重试，run 以
    failed 终态落账后由人工/对账路径处置（ADR-008 终止界外的效果语义）。
    """
