"""S3-T6 Plan/Analyze/Synthesize model action handlers.

Registered in the TaskHandlerRegistry for task primitives that
require model I/O through the Activity port pattern.
"""

from __future__ import annotations

from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput


class PlanModelHandler(TaskHandler):
    """Handler for Plan tasks that execute model I/O for planning."""

    @property
    def primitive_type(self) -> str:
        return "Plan"

    @property
    def handler_version(self) -> int:
        return 2

    def execute(self, input: TaskInput) -> TaskOutput:
        """Execute plan generation via model I/O.

        The actual model call goes through the Activity port;
        this handler orchestrates the request/response flow.
        """
        plan = input.input_values.get("plan", [])
        constraints = input.input_values.get("constraints", [])
        return TaskOutput(
            output_values={
                "plan": plan,
                "constraints": constraints,
                "status": "planned",
                "model_action": "plan",
            }
        )


class AnalyzeModelHandler(TaskHandler):
    """Handler for Analyze tasks that execute model I/O for analysis."""

    @property
    def primitive_type(self) -> str:
        return "Analyze"

    @property
    def handler_version(self) -> int:
        return 2

    def execute(self, input: TaskInput) -> TaskOutput:
        """Execute analysis via model I/O.

        The actual model call goes through the Activity port;
        this handler orchestrates the request/response flow.
        """
        analysis = input.input_values.get("analysis", {})
        evidence = input.input_values.get("evidence", [])
        return TaskOutput(
            output_values={
                "analysis": analysis,
                "evidence": evidence,
                "status": "analyzed",
                "model_action": "analyze",
            }
        )


class SynthesizeModelHandler(TaskHandler):
    """Handler for Synthesize tasks that execute model I/O for synthesis."""

    @property
    def primitive_type(self) -> str:
        return "Synthesize"

    @property
    def handler_version(self) -> int:
        return 2

    def execute(self, input: TaskInput) -> TaskOutput:
        """Execute synthesis via model I/O.

        The actual model call goes through the Activity port;
        this handler orchestrates the request/response flow.

        Note: Synthesize downgrade gate is handled at the Activity layer
        (RuntimeActivities.execute_task), not here. If unresolved conflicts
        exist, the Activity will short-circuit before calling this handler.
        """
        result = input.input_values.get("result", {})
        artifacts = input.input_values.get("artifacts", [])
        return TaskOutput(
            output_values={
                "result": result,
                "artifacts": artifacts,
                "status": "synthesized",
                "model_action": "synthesize",
            }
        )


ALL_MODEL_ACTION_HANDLERS: list[TaskHandler] = [
    PlanModelHandler(),
    AnalyzeModelHandler(),
    SynthesizeModelHandler(),
]
