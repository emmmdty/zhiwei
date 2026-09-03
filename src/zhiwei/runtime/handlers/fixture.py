"""S2 runtime: fixture handler for testing。

事实源：design doc §4.3、S2-T2 plan。

FixtureHandler is a test double that echoes input as output.
Used for testing the runtime without real LLM or tool integrations.
"""

from __future__ import annotations

from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput


class FixtureHandler(TaskHandler):
    """Test double handler that echoes input values as output.

    Used for integration and fixture-based testing of the runtime.
    """

    def __init__(self, primitive_type: str = "Fixture") -> None:
        self._primitive_type = primitive_type

    @property
    def primitive_type(self) -> str:
        return self._primitive_type

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        """Echo input values as output for deterministic testing."""
        return TaskOutput(output_values=input.input_values)
