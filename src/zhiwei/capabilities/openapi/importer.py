"""OpenAPI 3.1 importer with $ref resolution, cycle detection and digest pinning.

Constraints from S4 spec §4:
- Fixed source digest: content hash recorded at import time, immutable.
- Restrict $ref: max depth limit, reject cycles.
- Only operations: ignore paths without operation objects.
- Immutable host: server URL locked at import, never modifiable by model.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.canonical import canonical_json, digest_bytes

_MAX_REF_DEPTH = 32
_MAX_PATHS = 512


class OpenAPIImportError(RuntimeError):
    """Raised when OpenAPI import fails validation."""


class RefCycleError(OpenAPIImportError):
    """Raised when a $ref cycle is detected."""


class RefDepthExceededError(OpenAPIImportError):
    """Raised when $ref resolution exceeds maximum depth."""


class InvalidOpenAPISpecError(OpenAPIImportError):
    """Raised when the OpenAPI spec is structurally invalid."""


class HostOverrideError(OpenAPIImportError):
    """Raised when an attempt is made to modify the immutable host."""


class ImportedOperation(BaseModel):
    """A single operation extracted from an OpenAPI spec."""

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    summary: str = ""
    description: str = ""
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    request_body: dict[str, Any] | None = None
    responses: dict[str, Any] = Field(default_factory=dict)
    security: list[dict[str, list[str]]] | None = None
    deprecated: bool = False
    idempotent: bool = False
    source_digest: str = ""


class ImportedServer(BaseModel):
    """Immutable server configuration from the OpenAPI spec."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(min_length=1)
    description: str = ""
    variables: dict[str, dict[str, Any]] = Field(default_factory=dict)


class OpenAPIImportResult(BaseModel):
    """Result of importing an OpenAPI 3.1 specification."""

    model_config = ConfigDict(frozen=True)

    source_url: str = ""
    source_digest: str = Field(min_length=1)
    servers: tuple[ImportedServer, ...] = ()
    operations: tuple[ImportedOperation, ...] = ()
    security_schemes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    title: str = ""
    version: str = ""
    openapi_version: str = ""


class OpenAPIImporter:
    """Imports and validates OpenAPI 3.1 specifications.

    Produces an immutable import result with pinned source digest.
    """

    def __init__(self, *, max_ref_depth: int = _MAX_REF_DEPTH) -> None:
        self._max_ref_depth = max_ref_depth
        self._root_spec: Any = None

    def import_spec(
        self,
        spec: dict[str, Any],
        *,
        source_url: str = "",
    ) -> OpenAPIImportResult:
        """Import an OpenAPI 3.1 spec, resolving $refs and extracting operations."""
        self._validate_version(spec)
        self._root_spec = spec
        resolved = self._resolve_refs(spec, depth=0, seen=())

        servers = self._extract_servers(resolved)
        operations = self._extract_operations(resolved)
        security_schemes = self._extract_security_schemes(resolved)
        info = resolved.get("info", {})

        source_digest = digest_bytes(canonical_json(spec))

        return OpenAPIImportResult(
            source_url=source_url,
            source_digest=source_digest,
            servers=tuple(servers),
            operations=tuple(operations),
            security_schemes=security_schemes,
            title=info.get("title", ""),
            version=info.get("version", ""),
            openapi_version=resolved.get("openapi", ""),
        )

    def compute_source_digest(self, spec: dict[str, Any]) -> str:
        """Compute the fixed source digest for an OpenAPI spec."""
        return digest_bytes(canonical_json(spec))

    def _validate_version(self, spec: dict[str, Any]) -> None:
        openapi_version = spec.get("openapi", "")
        if not openapi_version.startswith("3.1"):
            raise InvalidOpenAPISpecError(
                f"Only OpenAPI 3.1.x is supported, got: {openapi_version!r}"
            )
        if "paths" not in spec:
            raise InvalidOpenAPISpecError("OpenAPI spec must contain 'paths'")

    def _resolve_refs(
        self,
        node: Any,
        *,
        depth: int,
        seen: tuple[str, ...],
    ) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_path = node["$ref"]
                if ref_path in seen:
                    raise RefCycleError(f"$ref cycle detected: {ref_path}")
                if depth >= self._max_ref_depth:
                    raise RefDepthExceededError(
                        f"$ref depth {depth} exceeds limit {self._max_ref_depth}"
                    )
                resolved = self._follow_ref(ref_path, node, spec=self._root_spec)
                return self._resolve_refs(resolved, depth=depth + 1, seen=(*seen, ref_path))
            return {
                k: self._resolve_refs(v, depth=depth, seen=seen)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [self._resolve_refs(item, depth=depth, seen=seen) for item in node]
        return node

    def _follow_ref(self, ref_path: str, current: dict[str, Any], *, spec: Any) -> Any:
        if not ref_path.startswith("#/"):
            raise InvalidOpenAPISpecError(f"External $ref not allowed: {ref_path}")
        parts = ref_path[2:].split("/")
        value: Any = spec
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise InvalidOpenAPISpecError(f"$ref target not found: {ref_path}")
        return copy.deepcopy(value)

    def _extract_servers(self, spec: dict[str, Any]) -> list[ImportedServer]:
        servers_data = spec.get("servers", [])
        servers: list[ImportedServer] = []
        for s in servers_data:
            if isinstance(s, dict) and "url" in s:
                servers.append(
                    ImportedServer(
                        url=s["url"],
                        description=s.get("description", ""),
                        variables=s.get("variables", {}),
                    )
                )
        return servers

    def _extract_operations(self, spec: dict[str, Any]) -> list[ImportedOperation]:
        paths = spec.get("paths", {})
        if len(paths) > _MAX_PATHS:
            raise InvalidOpenAPISpecError(
                f"Path count {len(paths)} exceeds limit {_MAX_PATHS}"
            )
        operations: list[ImportedOperation] = []
        http_methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method in http_methods:
                if method in path_item:
                    op = path_item[method]
                    if not isinstance(op, dict):
                        continue
                    op_id = op.get("operationId", "")
                    if not op_id:
                        op_id = f"{method}_{path.replace('/', '_').strip('_')}"
                    parameters = self._collect_parameters(path_item, op)
                    operations.append(
                        ImportedOperation(
                            operation_id=op_id,
                            method=method.upper(),
                            path=path,
                            summary=op.get("summary", ""),
                            description=op.get("description", ""),
                            parameters=parameters,
                            request_body=op.get("requestBody"),
                            responses=op.get("responses", {}),
                            security=op.get("security"),
                            deprecated=op.get("deprecated", False),
                            idempotent=method.upper() in {"GET", "HEAD", "OPTIONS"},
                        )
                    )
        return operations

    def _collect_parameters(
        self,
        path_item: dict[str, Any],
        operation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        path_params = path_item.get("parameters", [])
        op_params = operation.get("parameters", [])
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for p in path_params + op_params:
            if isinstance(p, dict) and "name" in p and "in" in p:
                key = (p["name"], p["in"])
                merged[key] = p
        return list(merged.values())

    def _extract_security_schemes(self, spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
        components = spec.get("components", {})
        schemes = components.get("securitySchemes", {})
        if not isinstance(schemes, dict):
            return {}
        return {k: v for k, v in schemes.items() if isinstance(v, dict)}
