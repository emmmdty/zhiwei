"""S2 runtime: core handler definitions for all 13 task primitives。

事实源：design doc §4.3、S2-T2 plan。

Core handlers are placeholder implementations that define the interface for each primitive.
S3-S7 will register real handlers via the same registry.
"""

from __future__ import annotations

from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput


class IntakeHandler(TaskHandler):
    """Handler for the Intake primitive."""

    @property
    def primitive_type(self) -> str:
        return "Intake"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class PlanHandler(TaskHandler):
    """Handler for the Plan primitive."""

    @property
    def primitive_type(self) -> str:
        return "Plan"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class ClarifyHandler(TaskHandler):
    """Handler for the Clarify primitive."""

    @property
    def primitive_type(self) -> str:
        return "Clarify"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class RetrieveHandler(TaskHandler):
    """Handler for the Retrieve primitive."""

    @property
    def primitive_type(self) -> str:
        return "Retrieve"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class AnalyzeHandler(TaskHandler):
    """Handler for the Analyze primitive."""

    @property
    def primitive_type(self) -> str:
        return "Analyze"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class InvokeToolHandler(TaskHandler):
    """Handler for the InvokeTool primitive."""

    @property
    def primitive_type(self) -> str:
        return "InvokeTool"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class DelegateHandler(TaskHandler):
    """Handler for the Delegate primitive."""

    @property
    def primitive_type(self) -> str:
        return "Delegate"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class VerifyHandler(TaskHandler):
    """Handler for the Verify primitive."""

    @property
    def primitive_type(self) -> str:
        return "Verify"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class RequestApprovalHandler(TaskHandler):
    """Handler for the RequestApproval primitive."""

    @property
    def primitive_type(self) -> str:
        return "RequestApproval"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class SynthesizeHandler(TaskHandler):
    """Handler for the Synthesize primitive."""

    @property
    def primitive_type(self) -> str:
        return "Synthesize"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class EmitArtifactHandler(TaskHandler):
    """Handler for the EmitArtifact primitive."""

    @property
    def primitive_type(self) -> str:
        return "EmitArtifact"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class WriteMemoryCandidateHandler(TaskHandler):
    """Handler for the WriteMemoryCandidate primitive."""

    @property
    def primitive_type(self) -> str:
        return "WriteMemoryCandidate"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


class FinishHandler(TaskHandler):
    """Handler for the Finish primitive."""

    @property
    def primitive_type(self) -> str:
        return "Finish"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values=input.input_values)


ALL_CORE_HANDLERS: list[TaskHandler] = [
    IntakeHandler(),
    PlanHandler(),
    ClarifyHandler(),
    RetrieveHandler(),
    AnalyzeHandler(),
    InvokeToolHandler(),
    DelegateHandler(),
    VerifyHandler(),
    RequestApprovalHandler(),
    SynthesizeHandler(),
    EmitArtifactHandler(),
    WriteMemoryCandidateHandler(),
    FinishHandler(),
]
