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
    def test_src_zhiwei_agents_does_not_import_app_modules(self) -> None:
        """src/zhiwei must not import from apps/ solution-packs modules."""
        import importlib
        import pkgutil

        import zhiwei.agents

        package_path = zhiwei.agents.__path__
        forbidden_prefixes = ("zhiwei.apps",)

        for _importer, modname, _ispkg in pkgutil.walk_packages(
            package_path, prefix="zhiwei.agents."
        ):
            try:
                mod = importlib.import_module(modname)
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name, None)
                    if attr is None:
                        continue
                    if hasattr(attr, "__module__") and attr.__module__:
                        for prefix in forbidden_prefixes:
                            if attr.__module__.startswith(prefix):
                                pytest.fail(
                                    f"{modname} imports from {attr.__module__} "
                                    f"which starts with forbidden prefix {prefix}"
                                )
            except ImportError:
                pass
