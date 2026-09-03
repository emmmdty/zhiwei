"""Schema registry for task primitives.

Defines and validates the structure of tasks within agent task graphs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskPrimitiveSchema(BaseModel):
    """Schema definition for a task primitive type.

    Each primitive has a name and a mapping of field names to expected types.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value


class SchemaNotFoundError(RuntimeError):
    """Requested schema does not exist in the registry."""


class SchemaValidationError(RuntimeError):
    """Task does not conform to the registered schema."""


class SchemaRegistry:
    """Registry of task primitive schemas.

    Validates that tasks conform to their declared schema before execution.
    """

    def __init__(self) -> None:
        self._schemas: dict[str, TaskPrimitiveSchema] = {}

    def register(self, schema: TaskPrimitiveSchema) -> None:
        """Register a task primitive schema."""
        if schema.name in self._schemas:
            raise SchemaValidationError(
                f"Schema '{schema.name}' already registered"
            )
        self._schemas[schema.name] = schema

    def get(self, name: str) -> TaskPrimitiveSchema:
        """Get a schema by name."""
        if name not in self._schemas:
            raise SchemaNotFoundError(f"Schema '{name}' not found")
        return self._schemas[name]

    def validate_task(self, task: dict[str, Any]) -> bool:
        """Validate a task against its declared schema.

        Returns True if valid, raises SchemaValidationError otherwise.
        """
        task_type = task.get("type")
        if not task_type:
            raise SchemaValidationError("Task missing 'type' field")

        schema = self.get(task_type)

        missing_fields = set(schema.fields.keys()) - set(task.keys())
        if missing_fields:
            raise SchemaValidationError(
                f"Task of type '{task_type}' missing fields: {sorted(missing_fields)}"
            )

        return True
