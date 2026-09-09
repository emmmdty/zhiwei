"""S4 InvokeTool runtime handler.

Runtime handler that delegates to the Tool Gateway Activity.
Intent/result/receipt submitted through canonical events (S4 spec §7).

事实源：S4 spec §5、§7 (Runtime：InvokeTool handler 只调用 Tool Gateway Activity)。
"""

from __future__ import annotations

from uuid import UUID

from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput


class InvokeToolHandler(TaskHandler):
    """Handler for InvokeTool task type.

    Routes tool invocations through the Tool Gateway. The actual execution
    is delegated to the Gateway Activity which handles the full pipeline:
    intent → policy → approval → credential → sandbox → execute → receipt.
    """

    @property
    def primitive_type(self) -> str:
        return "InvokeTool"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        """Execute a tool invocation.

        The handler validates input and returns structured output. Actual
        execution is delegated to the Tool Gateway Activity via the
        Temporal workflow.
        """
        tool_name = input.input_values.get("tool_name", "")
        if not tool_name:
            return TaskOutput(
                output_values={
                    "status": "failed",
                    "error": "tool_name is required",
                }
            )

        tool_version_id = input.input_values.get("tool_version_id")
        if not tool_version_id:
            return TaskOutput(
                output_values={
                    "status": "failed",
                    "error": "tool_version_id is required",
                }
            )

        connection_id = input.input_values.get("connection_id")
        if not connection_id:
            return TaskOutput(
                output_values={
                    "status": "failed",
                    "error": "connection_id is required",
                }
            )

        credential_binding_id = input.input_values.get("credential_binding_id")
        if not credential_binding_id:
            return TaskOutput(
                output_values={
                    "status": "failed",
                    "error": "credential_binding_id is required",
                }
            )

        principal_id = input.input_values.get("principal_id")
        if not principal_id:
            return TaskOutput(
                output_values={
                    "status": "failed",
                    "error": "principal_id is required",
                }
            )

        # Validate input args exist
        input_args = input.input_values.get("input_args", {})
        if not isinstance(input_args, dict):
            return TaskOutput(
                output_values={
                    "status": "failed",
                    "error": "input_args must be a dictionary",
                }
            )

        # In the real implementation, this handler would be registered with
        # the Temporal workflow and would delegate to the Tool Gateway Activity.
        # For now, we return a structured output that indicates the handler
        # has validated the input and is ready for the Activity to execute.
        return TaskOutput(
            output_values={
                "status": "ready_for_execution",
                "tool_name": tool_name,
                "tool_version_id": str(tool_version_id),
                "connection_id": str(connection_id),
                "credential_binding_id": str(credential_binding_id),
                "principal_id": str(principal_id),
                "input_args": input_args,
                "invocation_id": str(UUID(input.task_id.replace("task:", "").replace("-", "")[:32].ljust(32, "0"))),
            }
        )
