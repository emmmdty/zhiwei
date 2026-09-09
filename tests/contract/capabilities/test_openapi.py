"""S4-T5 contract: OpenAPI importer, operations and auth tests."""

from __future__ import annotations

import pytest

from zhiwei.capabilities.openapi.auth import (
    AuthScheme,
    AuthSchemeType,
    OpenAPIAuthExtractor,
)
from zhiwei.capabilities.openapi.importer import (
    InvalidOpenAPISpecError,
    OpenAPIImporter,
    RefCycleError,
    RefDepthExceededError,
)
from zhiwei.capabilities.openapi.operations import (
    OperationFilter,
    OperationSelector,
    ParameterLocation,
    ValidatedOperation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_OPENAPI_31 = {
    "openapi": "3.1.0",
    "info": {"title": "Test API", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/items": {
            "get": {
                "operationId": "listItems",
                "summary": "List items",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer", "default": 10},
                    }
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "operationId": "createItem",
                "summary": "Create an item",
                "parameters": [],
                "requestBody": {
                    "content": {"application/json": {"schema": {"type": "object"}}}
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/items/{itemId}": {
            "get": {
                "operationId": "getItem",
                "summary": "Get an item",
                "parameters": [
                    {
                        "name": "itemId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "put": {
                "operationId": "updateItem",
                "summary": "Update an item",
                "parameters": [
                    {
                        "name": "itemId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "requestBody": {
                    "content": {"application/json": {"schema": {"type": "object"}}}
                },
                "responses": {"200": {"description": "OK"}},
            },
            "delete": {
                "operationId": "deleteItem",
                "summary": "Delete an item",
                "deprecated": True,
                "parameters": [
                    {
                        "name": "itemId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"204": {"description": "Deleted"}},
            },
        },
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            },
            "apiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
            },
        }
    },
}


# ---------------------------------------------------------------------------
# Importer tests
# ---------------------------------------------------------------------------


class TestOpenAPIImporter:
    def test_import_minimal_spec(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        assert result.openapi_version == "3.1.0"
        assert result.title == "Test API"
        assert result.version == "1.0.0"
        assert len(result.operations) > 0

    def test_source_digest_is_deterministic(self) -> None:
        importer = OpenAPIImporter()
        r1 = importer.import_spec(_MINIMAL_OPENAPI_31)
        r2 = importer.import_spec(_MINIMAL_OPENAPI_31)
        assert r1.source_digest == r2.source_digest
        assert r1.source_digest.startswith("sha256:")

    def test_source_digest_changes_with_content(self) -> None:
        importer = OpenAPIImporter()
        r1 = importer.import_spec(_MINIMAL_OPENAPI_31)
        modified = {**_MINIMAL_OPENAPI_31, "info": {"title": "Other", "version": "2.0.0"}}
        r2 = importer.import_spec(modified)
        assert r1.source_digest != r2.source_digest

    def test_rejects_non_31_version(self) -> None:
        importer = OpenAPIImporter()
        spec = {"openapi": "3.0.3", "paths": {}}
        with pytest.raises(InvalidOpenAPISpecError, match=r"3\.1"):
            importer.import_spec(spec)

    def test_rejects_missing_paths(self) -> None:
        importer = OpenAPIImporter()
        spec = {"openapi": "3.1.0"}
        with pytest.raises(InvalidOpenAPISpecError, match="paths"):
            importer.import_spec(spec)

    def test_extracts_servers(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        assert len(result.servers) == 1
        assert result.servers[0].url == "https://api.example.com/v1"

    def test_extracts_security_schemes(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        assert "bearerAuth" in result.security_schemes
        assert "apiKey" in result.security_schemes
        assert result.security_schemes["bearerAuth"]["type"] == "http"

    def test_source_url_recorded(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(
            _MINIMAL_OPENAPI_31, source_url="https://example.com/openapi.json"
        )
        assert result.source_url == "https://example.com/openapi.json"


# ---------------------------------------------------------------------------
# $ref tests
# ---------------------------------------------------------------------------


class TestRefResolution:
    def test_ref_cycle_rejected(self) -> None:
        spec = {
            "openapi": "3.1.0",
            "paths": {},
            "components": {
                "schemas": {
                    "A": {"$ref": "#/components/schemas/B"},
                    "B": {"$ref": "#/components/schemas/A"},
                }
            },
        }
        importer = OpenAPIImporter()
        importer._root_spec = spec
        with pytest.raises(RefCycleError, match="cycle"):
            importer._resolve_refs(spec, depth=0, seen=())

    def test_ref_depth_limit(self) -> None:
        # Create a spec where a ref points to another ref that points to another ref
        # With max_ref_depth=2, the third level should fail
        spec = {
            "openapi": "3.1.0",
            "paths": {},
            "components": {
                "schemas": {
                    "A": {"$ref": "#/components/schemas/B"},
                    "B": {"$ref": "#/components/schemas/C"},
                    "C": {"type": "string"},
                }
            },
        }
        importer = OpenAPIImporter(max_ref_depth=2)
        importer._root_spec = spec
        # depth=0 -> resolves A->B (depth becomes 1) -> resolves B->C (depth becomes 2) -> ok
        # This should work because depth < max_ref_depth at each step
        result = importer._resolve_refs(spec["components"]["schemas"]["A"], depth=0, seen=())
        assert result == {"type": "string"}

        # Now test with depth starting at the limit
        importer2 = OpenAPIImporter(max_ref_depth=1)
        importer2._root_spec = spec
        with pytest.raises(RefDepthExceededError):
            importer2._resolve_refs(
                {"$ref": "#/components/schemas/A"},
                depth=1,
                seen=(),
            )

    def test_ref_resolves_inline(self) -> None:
        spec = {
            "openapi": "3.1.0",
            "paths": {
                "/test": {
                    "get": {
                        "operationId": "testOp",
                        "parameters": [
                            {"$ref": "#/components/parameters/LimitParam"}
                        ],
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
            "components": {
                "parameters": {
                    "LimitParam": {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer"},
                    }
                }
            },
        }
        importer = OpenAPIImporter()
        result = importer.import_spec(spec)
        op = next(o for o in result.operations if o.operation_id == "testOp")
        assert any(p.get("name") == "limit" for p in op.parameters)

    def test_external_ref_rejected(self) -> None:
        importer = OpenAPIImporter()
        with pytest.raises(InvalidOpenAPISpecError, match="External"):
            importer._resolve_refs({"$ref": "https://evil.com/schema.json"}, depth=0, seen=())


# ---------------------------------------------------------------------------
# Operations tests
# ---------------------------------------------------------------------------


class TestOperationSelector:
    def test_select_all_operations(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        assert len(validated) > 0

    def test_filter_by_method(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        filter = OperationFilter(methods=frozenset({"GET"}))
        validated = selector.select_operations(result.operations, filter=filter)
        for v in validated:
            assert v.operation.method == "GET"

    def test_filter_by_path_prefix(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        filter = OperationFilter(path_prefix="/items/{itemId}")
        validated = selector.select_operations(result.operations, filter=filter)
        for v in validated:
            assert v.operation.path.startswith("/items/{itemId}")

    def test_filter_excludes_deprecated(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        filter = OperationFilter(include_deprecated=False)
        validated = selector.select_operations(result.operations, filter=filter)
        for v in validated:
            assert not v.operation.deprecated

    def test_filter_includes_deprecated(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        filter = OperationFilter(include_deprecated=True)
        validated = selector.select_operations(result.operations, filter=filter)
        deprecated_ops = [v for v in validated if v.operation.deprecated]
        assert len(deprecated_ops) > 0

    def test_typed_parameters_extracted(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        list_items = [v for v in validated if v.operation.operation_id == "listItems"]
        assert len(list_items) == 1
        assert len(list_items[0].typed_parameters) == 1
        assert list_items[0].typed_parameters[0].name == "limit"
        assert list_items[0].typed_parameters[0].schema_type == "integer"

    def test_write_operations_flagged(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        write_ops = [v for v in validated if v.is_write]
        write_methods = {v.operation.method for v in write_ops}
        assert write_methods <= {"POST", "PUT", "PATCH", "DELETE"}

    def test_idempotency_on_write(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        write_ops = [v for v in validated if v.is_write]
        for v in write_ops:
            assert v.idempotency is not None
            assert v.idempotency.requires_key is True

    def test_no_idempotency_on_read(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        read_ops = [v for v in validated if not v.is_write]
        for v in read_ops:
            assert v.idempotency is None

    def test_path_variable_detection(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        get_item = [v for v in validated if v.operation.operation_id == "getItem"]
        assert len(get_item) == 1
        path_param_names = [p.name for p in get_item[0].typed_parameters if p.location == ParameterLocation.PATH]
        assert "itemId" in path_param_names

    def test_validation_error_for_undeclared_path_var(self) -> None:
        spec = {
            "openapi": "3.1.0",
            "paths": {
                "/test/{id}": {
                    "get": {
                        "operationId": "testOp",
                        "parameters": [],
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        importer = OpenAPIImporter()
        result = importer.import_spec(spec)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        assert len(validated) == 1
        errors = validated[0].validation_errors
        assert any("id" in e for e in errors)

    def test_validation_error_for_untyped_param(self) -> None:
        spec = {
            "openapi": "3.1.0",
            "paths": {
                "/test": {
                    "get": {
                        "operationId": "testOp",
                        "parameters": [
                            {"name": "q", "in": "query", "schema": {}}
                        ],
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        importer = OpenAPIImporter()
        result = importer.import_spec(spec)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        assert len(validated) == 1
        errors = validated[0].validation_errors
        assert any("type" in e for e in errors)

    def test_request_body_detected(self) -> None:
        importer = OpenAPIImporter()
        result = importer.import_spec(_MINIMAL_OPENAPI_31)
        selector = OperationSelector()
        validated = selector.select_operations(result.operations)
        create_op = [v for v in validated if v.operation.operation_id == "createItem"]
        assert len(create_op) == 1
        assert create_op[0].has_typed_request_body is True

    def test_frozen_models(self) -> None:
        validated = ValidatedOperation(
            operation=__import__("zhiwei.capabilities.openapi.importer", fromlist=["ImportedOperation"]).ImportedOperation(
                operation_id="test",
                method="GET",
                path="/test",
            ),
        )
        with pytest.raises(Exception, match="frozen"):
            validated.operation_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestOpenAPIAuthExtractor:
    def test_extract_bearer_auth(self) -> None:
        schemes = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            }
        }
        extractor = OpenAPIAuthExtractor()
        result = extractor.extract(schemes)
        assert "bearerAuth" in result.schemes
        assert result.schemes["bearerAuth"].scheme_type == AuthSchemeType.HTTP
        assert result.schemes["bearerAuth"].http_scheme == "bearer"

    def test_extract_api_key(self) -> None:
        schemes = {
            "apiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
            }
        }
        extractor = OpenAPIAuthExtractor()
        result = extractor.extract(schemes)
        assert "apiKey" in result.schemes
        assert result.schemes["apiKey"].scheme_type == AuthSchemeType.API_KEY
        assert result.schemes["apiKey"].api_key_name == "X-API-Key"

    def test_extract_oauth2(self) -> None:
        schemes = {
            "oauth2": {
                "type": "oauth2",
                "flows": {
                    "authorizationCode": {
                        "authorizationUrl": "https://auth.example.com/authorize",
                        "tokenUrl": "https://auth.example.com/token",
                        "scopes": {"read": "Read access"},
                    }
                },
            }
        }
        extractor = OpenAPIAuthExtractor()
        result = extractor.extract(schemes)
        assert "oauth2" in result.schemes
        assert result.schemes["oauth2"].scheme_type == AuthSchemeType.OAUTH2

    def test_extract_global_security(self) -> None:
        extractor = OpenAPIAuthExtractor()
        result = extractor.extract(
            {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            global_security=[{"bearerAuth": []}],
        )
        assert len(result.global_requirements) == 1
        assert "bearerAuth" in result.global_requirements[0].scheme_names

    def test_extract_operation_security(self) -> None:
        extractor = OpenAPIAuthExtractor()
        result = extractor.extract(
            {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Key"}},
            operation_security={"listItems": [{"apiKey": []}]},
        )
        assert "listItems" in result.operation_requirements

    def test_validate_scheme_available_bearer(self) -> None:
        extractor = OpenAPIAuthExtractor()
        scheme = AuthScheme(
            name="bearerAuth",
            scheme_type=AuthSchemeType.HTTP,
            http_scheme="bearer",
        )
        assert extractor.validate_scheme_available(
            scheme, available_credentials={"auth_bearer": "token123"}
        )
        assert not extractor.validate_scheme_available(
            scheme, available_credentials={}
        )

    def test_validate_scheme_available_api_key(self) -> None:
        extractor = OpenAPIAuthExtractor()
        scheme = AuthScheme(
            name="apiKey",
            scheme_type=AuthSchemeType.API_KEY,
            api_key_name="X-API-Key",
        )
        assert extractor.validate_scheme_available(
            scheme, available_credentials={"X-API-Key": "key123"}
        )
        assert not extractor.validate_scheme_available(
            scheme, available_credentials={}
        )

    def test_validate_scheme_available_oauth2(self) -> None:
        extractor = OpenAPIAuthExtractor()
        scheme = AuthScheme(
            name="oauth2",
            scheme_type=AuthSchemeType.OAUTH2,
        )
        assert extractor.validate_scheme_available(
            scheme, available_credentials={"oauth2_oauth2": "token"}
        )
        assert not extractor.validate_scheme_available(
            scheme, available_credentials={}
        )

    def test_unknown_scheme_type_skipped(self) -> None:
        extractor = OpenAPIAuthExtractor()
        result = extractor.extract({"unknown": {"type": "weird"}})
        assert len(result.schemes) == 0

    def test_security_requirement_scopes(self) -> None:
        extractor = OpenAPIAuthExtractor()
        result = extractor.extract(
            {"oauth2": {"type": "oauth2", "flows": {}}},
            global_security=[{"oauth2": ["read", "write"]}],
        )
        assert result.global_requirements[0].scopes["oauth2"] == ("read", "write")
