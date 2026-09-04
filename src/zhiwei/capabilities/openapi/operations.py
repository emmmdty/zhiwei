"""OpenAPI operation selection, validation and typed parameter handling.

Constraints from S4 spec §4:
- Only select operations from the spec (no schema-only extraction).
- Typed params: parameters must have explicit types.
- Write operation idempotency/reconcile: POST/PUT/PATCH require
  idempotency key or reconcile strategy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.capabilities.openapi.importer import ImportedOperation


class HttpMethod(StrEnum):
    GET = "GET"
    PUT = "PUT"
    POST = "POST"
    DELETE = "DELETE"
    OPTIONS = "OPTIONS"
    HEAD = "HEAD"
    PATCH = "PATCH"
    TRACE = "TRACE"


class ParameterLocation(StrEnum):
    QUERY = "query"
    HEADER = "header"
    PATH = "path"
    COOKIE = "cookie"


class TypedParameter(BaseModel):
    """A validated parameter with explicit type information."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    location: ParameterLocation
    schema_type: str = Field(min_length=1)
    required: bool = False
    description: str = ""
    deprecated: bool = False
    enum_values: tuple[str, ...] = ()
    default: Any = None


class OperationFilter(BaseModel):
    """Filter criteria for selecting operations from an OpenAPI spec."""

    model_config = ConfigDict(frozen=True)

    methods: frozenset[str] = frozenset()
    path_prefix: str = ""
    include_deprecated: bool = False
    require_idempotent: bool = False
    tags: frozenset[str] = frozenset()


class IdempotencyRequirement(BaseModel):
    """Idempotency requirement for a write operation."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    method: str
    requires_key: bool = True
    key_header: str = "Idempotency-Key"
    reconcile_strategy: str = "reject"


class ValidatedOperation(BaseModel):
    """An operation after validation of typed parameters and idempotency."""

    model_config = ConfigDict(frozen=True)

    operation: ImportedOperation
    typed_parameters: tuple[TypedParameter, ...] = ()
    is_write: bool = False
    idempotency: IdempotencyRequirement | None = None
    has_typed_request_body: bool = False
    validation_errors: tuple[str, ...] = ()


class OperationSelector:
    """Selects and validates operations from OpenAPI import results."""

    _WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
    _READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def select_operations(
        self,
        operations: tuple[ImportedOperation, ...],
        *,
        filter: OperationFilter | None = None,
    ) -> list[ValidatedOperation]:
        """Select and validate operations matching the given filter."""
        results: list[ValidatedOperation] = []
        for op in operations:
            if filter and not self._matches_filter(op, filter):
                continue
            validated = self._validate_operation(op)
            results.append(validated)
        return results

    def _matches_filter(self, op: ImportedOperation, filter: OperationFilter) -> bool:
        if filter.methods and op.method.upper() not in filter.methods:
            return False
        if filter.path_prefix and not op.path.startswith(filter.path_prefix):
            return False
        if not filter.include_deprecated and op.deprecated:
            return False
        return not (filter.require_idempotent and op.method.upper() in self._WRITE_METHODS)

    def _validate_operation(self, op: ImportedOperation) -> ValidatedOperation:
        typed_params = self._validate_parameters(op)
        is_write = op.method.upper() in self._WRITE_METHODS
        idempotency = self._check_idempotency(op) if is_write else None
        errors = self._collect_validation_errors(op, typed_params)
        has_body = op.request_body is not None

        return ValidatedOperation(
            operation=op,
            typed_parameters=tuple(typed_params),
            is_write=is_write,
            idempotency=idempotency,
            has_typed_request_body=has_body,
            validation_errors=tuple(errors),
        )

    def _validate_parameters(self, op: ImportedOperation) -> list[TypedParameter]:
        typed: list[TypedParameter] = []
        for p in op.parameters:
            name = p.get("name", "")
            location = p.get("in", "")
            schema = p.get("schema", {})
            schema_type = schema.get("type", "string") if isinstance(schema, dict) else "string"
            enum_vals = schema.get("enum", []) if isinstance(schema, dict) else []
            typed.append(
                TypedParameter(
                    name=name,
                    location=ParameterLocation(location),
                    schema_type=schema_type,
                    required=p.get("required", False),
                    description=p.get("description", ""),
                    deprecated=p.get("deprecated", False),
                    enum_values=tuple(str(v) for v in enum_vals) if isinstance(enum_vals, list) else (),
                    default=schema.get("default") if isinstance(schema, dict) else None,
                )
            )
        return typed

    def _check_idempotency(self, op: ImportedOperation) -> IdempotencyRequirement | None:
        if op.method.upper() not in self._WRITE_METHODS:
            return None
        return IdempotencyRequirement(
            operation_id=op.operation_id,
            method=op.method,
            requires_key=True,
        )

    def _collect_validation_errors(
        self,
        op: ImportedOperation,
        typed_params: list[TypedParameter],
    ) -> list[str]:
        errors: list[str] = []
        for p in op.parameters:
            schema = p.get("schema", {})
            if not isinstance(schema, dict) or "type" not in schema:
                errors.append(
                    f"Parameter '{p.get('name', '?')}' lacks explicit type in schema"
                )
        path_params = {p.name for p in typed_params if p.location == ParameterLocation.PATH}
        import re

        path_vars = set(re.findall(r"\{(\w+)\}", op.path))
        missing = path_vars - path_params
        for mv in sorted(missing):
            errors.append(f"Path variable '{{{mv}}}' not declared in parameters")
        return errors
