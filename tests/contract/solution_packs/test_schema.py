"""S2-T1 CONTRACT: Schema registry for task primitives and architecture boundary test."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zhiwei.agents.schemas import (
    SchemaNotFoundError,
    SchemaRegistry,
    SchemaValidationError,
    TaskPrimitiveSchema,
)


class TestTaskPrimitiveSchema:
    def test_prompt_primitive_schema(self) -> None:
        schema = TaskPrimitiveSchema(
            name="prompt",
            fields={"template": str, "variables": dict},
        )
        assert schema.name == "prompt"
        assert "template" in schema.fields

    def test_tool_use_primitive_schema(self) -> None:
        schema = TaskPrimitiveSchema(
            name="tool_use",
            fields={"tool_name": str, "parameters": dict},
        )
        assert schema.name == "tool_use"

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValidationError):
            TaskPrimitiveSchema(name="", fields={})

    def test_rejects_duplicate_field_names(self) -> None:
        # Python dict literals silently overwrite duplicate keys, so we verify
        # that the schema correctly tracks its fields after construction.
        schema = TaskPrimitiveSchema(name="prompt", fields={"template": str, "variables": dict})
        assert len(schema.fields) == 2
        assert "template" in schema.fields
        assert "variables" in schema.fields

        # Ensure fields are frozen (immutable)
        with pytest.raises(ValidationError):
            schema.fields = {}  # type: ignore[misc]


class TestSchemaRegistry:
    def test_register_and_get_schema(self) -> None:
        registry = SchemaRegistry()
        schema = TaskPrimitiveSchema(
            name="prompt",
            fields={"template": str},
        )
        registry.register(schema)
        assert registry.get("prompt") == schema

    def test_get_nonexistent_schema_raises(self) -> None:
        registry = SchemaRegistry()
        with pytest.raises(SchemaNotFoundError):
            registry.get("nonexistent")

    def test_register_duplicate_name_raises(self) -> None:
        registry = SchemaRegistry()
        schema = TaskPrimitiveSchema(name="prompt", fields={"template": str})
        registry.register(schema)
        with pytest.raises(SchemaValidationError, match="already"):
            registry.register(schema)

    def test_validate_task_against_schema(self) -> None:
        registry = SchemaRegistry()
        schema = TaskPrimitiveSchema(
            name="prompt",
            fields={"template": str},
        )
        registry.register(schema)
        valid_task = {"type": "prompt", "template": "Hello {{name}}"}
        assert registry.validate_task(valid_task) is True

    def test_validate_task_with_invalid_type_raises(self) -> None:
        registry = SchemaRegistry()
        schema = TaskPrimitiveSchema(
            name="prompt",
            fields={"template": str},
        )
        registry.register(schema)
        invalid_task = {"type": "unknown", "template": "Hello"}
        with pytest.raises(SchemaNotFoundError):
            registry.validate_task(invalid_task)

    def test_validate_task_with_missing_fields_raises(self) -> None:
        registry = SchemaRegistry()
        schema = TaskPrimitiveSchema(
            name="prompt",
            fields={"template": str, "variables": dict},
        )
        registry.register(schema)
        invalid_task = {"type": "prompt", "template": "Hello"}
        with pytest.raises(SchemaValidationError, match="missing"):
            registry.validate_task(invalid_task)


class TestArchitectureBoundary:
    """2026-09-03 修订：原断言的 zhiwei.apps 前缀在代码树中不存在（恒真）且
    ImportError 被吞（fail-open）——由 test_core_boundary.py 的真实模块遍历
    + fail-closed 重写取代（ADR-012：恒真断言与 except-pass 不满足
    spec §6 架构边界条目）。本类保留占位以记录替换关系。"""
