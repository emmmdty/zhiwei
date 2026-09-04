"""S5-T4 RED: PostgreSQL connector contract tests.

Tests cover:
- reproducibility_level declaration at connection time (ADR-003)
- Schema snapshot creation with digest
- Read-only AST validation (rejects writes, DDL)
- Query result canonicalization (copy_frozen)
- Timeout/row/byte limit enforcement
- Source Ledger integration (observation enters ledger before Evidence)
"""

from __future__ import annotations

from typing import Any

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.connectors.postgres import (
    PostgresConnector,
    QueryLimitExceeded,
    QueryLimits,
    QueryResult,
    ReproducibilityLevel,
    SchemaColumn,
    SchemaSnapshot,
    SchemaSnapshotError,
    SchemaTable,
    SqlValidationError,
    validate_read_only,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_connector(
    *,
    reproducibility_level: ReproducibilityLevel = ReproducibilityLevel.COPY_FROZEN,
    schema_provider: Any = None,
    query_executor: Any = None,
    query_limits: QueryLimits | None = None,
) -> PostgresConnector:
    return PostgresConnector(
        dsn="postgresql://user:pass@localhost:5432/testdb",
        organization_id=new_id(),
        workspace_id=new_id(),
        reproducibility_level=reproducibility_level,
        schema_provider=schema_provider,
        query_executor=query_executor,
        query_limits=query_limits,
    )


SAMPLE_SCHEMA_DATA = [
    {
        "schema_name": "public",
        "table_name": "users",
        "columns": [
            {"name": "id", "data_type": "integer", "nullable": False, "is_primary_key": True},
            {"name": "name", "data_type": "varchar", "nullable": True, "is_primary_key": False},
            {"name": "email", "data_type": "varchar", "nullable": False, "is_primary_key": False},
        ],
        "row_count_estimate": 1000,
    },
    {
        "schema_name": "public",
        "table_name": "orders",
        "columns": [
            {"name": "id", "data_type": "integer", "nullable": False, "is_primary_key": True},
            {"name": "user_id", "data_type": "integer", "nullable": False, "is_primary_key": False},
            {"name": "amount", "data_type": "decimal", "nullable": True, "is_primary_key": False},
        ],
        "row_count_estimate": 5000,
    },
]


def _mock_schema_provider(dsn: str) -> list[dict[str, Any]]:
    return SAMPLE_SCHEMA_DATA


def _mock_query_executor(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": 1, "name": "Alice", "email": "alice@example.com"},
        {"id": 2, "name": "Bob", "email": "bob@example.com"},
    ]


def _empty_query_executor(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return []


# ---------------------------------------------------------------------------
# ReproducibilityLevel tests
# ---------------------------------------------------------------------------


class TestReproducibilityLevel:
    def test_replayable_value(self) -> None:
        assert ReproducibilityLevel.REPLAYABLE == "replayable"

    def test_copy_frozen_value(self) -> None:
        assert ReproducibilityLevel.COPY_FROZEN == "copy_frozen"

    def test_reference_only_value(self) -> None:
        assert ReproducibilityLevel.REFERENCE_ONLY == "reference_only"

    def test_all_levels_covered(self) -> None:
        levels = set(ReproducibilityLevel)
        assert len(levels) == 3


# ---------------------------------------------------------------------------
# Connection lifecycle tests
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    def test_connect_and_disconnect(self) -> None:
        conn = _make_connector()
        conn.connect()
        conn.disconnect()

    def test_operations_when_disconnected_raise(self) -> None:
        conn = _make_connector()
        with pytest.raises(RuntimeError, match="not connected"):
            conn.query("SELECT 1")

    def test_operations_after_disconnect_raise(self) -> None:
        conn = _make_connector()
        conn.connect()
        conn.disconnect()
        with pytest.raises(RuntimeError, match="not connected"):
            conn.query("SELECT 1")

    def test_reproducibility_level_declared_at_init(self) -> None:
        for level in ReproducibilityLevel:
            conn = _make_connector(reproducibility_level=level)
            assert conn.reproducibility_level == level


# ---------------------------------------------------------------------------
# DSN validation tests
# ---------------------------------------------------------------------------


class TestDsnValidation:
    def test_blank_dsn_rejected(self) -> None:
        with pytest.raises(ValueError, match="dsn must not be blank"):
            PostgresConnector(
                dsn="  ",
                organization_id=new_id(),
                workspace_id=new_id(),
            )

    def test_whitespace_stripped_dsn_check(self) -> None:
        with pytest.raises(ValueError, match="dsn must not be blank"):
            PostgresConnector(
                dsn="   ",
                organization_id=new_id(),
                workspace_id=new_id(),
            )


# ---------------------------------------------------------------------------
# Schema snapshot tests
# ---------------------------------------------------------------------------


class TestSchemaSnapshot:
    def test_create_snapshot(self) -> None:
        conn = _make_connector(schema_provider=_mock_schema_provider)
        conn.connect()
        snapshot = conn.create_schema_snapshot()

        assert isinstance(snapshot, SchemaSnapshot)
        assert len(snapshot.tables) == 2
        assert snapshot.tables[0].table_name == "users"
        assert snapshot.tables[1].table_name == "orders"
        assert snapshot.snapshot_id.startswith("snap_")
        assert snapshot.digest.startswith("sha256:")

    def test_snapshot_digest_deterministic(self) -> None:
        conn = _make_connector(schema_provider=_mock_schema_provider)
        conn.connect()
        s1 = conn.create_schema_snapshot()
        conn._schema_snapshot = None
        s2 = conn.create_schema_snapshot()
        assert s1.digest == s2.digest

    def test_snapshot_without_provider_raises(self) -> None:
        conn = _make_connector()
        conn.connect()
        with pytest.raises(SchemaSnapshotError, match="No schema_provider"):
            conn.create_schema_snapshot()

    def test_snapshot_columns_preserved(self) -> None:
        conn = _make_connector(schema_provider=_mock_schema_provider)
        conn.connect()
        snapshot = conn.create_schema_snapshot()

        users_table = snapshot.tables[0]
        assert len(users_table.columns) == 3
        assert users_table.columns[0].name == "id"
        assert users_table.columns[0].is_primary_key is True
        assert users_table.columns[1].nullable is True

    def test_snapshot_row_count_estimate(self) -> None:
        conn = _make_connector(schema_provider=_mock_schema_provider)
        conn.connect()
        snapshot = conn.create_schema_snapshot()
        assert snapshot.tables[0].row_count_estimate == 1000


# ---------------------------------------------------------------------------
# Read-only AST validation tests
# ---------------------------------------------------------------------------


class TestReadOnlyValidation:
    def test_select_allowed(self) -> None:
        validate_read_only("SELECT * FROM users WHERE id = 1")

    def test_select_with_join_allowed(self) -> None:
        validate_read_only(
            "SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id"
        )

    def test_insert_rejected(self) -> None:
        with pytest.raises(SqlValidationError, match="Non-read-only"):
            validate_read_only("INSERT INTO users (name) VALUES ('test')")

    def test_update_rejected(self) -> None:
        with pytest.raises(SqlValidationError, match="Non-read-only"):
            validate_read_only("UPDATE users SET name = 'test' WHERE id = 1")

    def test_delete_rejected(self) -> None:
        with pytest.raises(SqlValidationError, match="Non-read-only"):
            validate_read_only("DELETE FROM users WHERE id = 1")

    def test_drop_rejected(self) -> None:
        with pytest.raises(SqlValidationError, match="Non-read-only"):
            validate_read_only("DROP TABLE users")

    def test_alter_rejected(self) -> None:
        with pytest.raises(SqlValidationError, match="Non-read-only"):
            validate_read_only("ALTER TABLE users ADD COLUMN foo TEXT")

    def test_blank_sql_rejected(self) -> None:
        with pytest.raises(SqlValidationError, match="must not be blank"):
            validate_read_only("")

    def test_whitespace_sql_rejected(self) -> None:
        with pytest.raises(SqlValidationError, match="must not be blank"):
            validate_read_only("   ")

    def test_invalid_sql_rejected(self) -> None:
        with pytest.raises(SqlValidationError):
            validate_read_only("NOT VALID SQL AT ALL")


# ---------------------------------------------------------------------------
# Query execution tests
# ---------------------------------------------------------------------------


class TestQueryExecution:
    def test_query_returns_result_and_version(self) -> None:
        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=_mock_query_executor,
        )
        conn.connect()
        result, version = conn.query("SELECT id, name, email FROM users")

        assert isinstance(result, QueryResult)
        assert result.row_count == 2
        assert result.columns == ("id", "name", "email")
        assert version.content_digest.startswith("sha256:")

    def test_query_empty_result(self) -> None:
        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=_empty_query_executor,
        )
        conn.connect()
        result, _version = conn.query("SELECT * FROM users WHERE 1=0")
        assert result.row_count == 0
        assert result.columns == ()

    def test_query_with_typed_params(self) -> None:
        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=_mock_query_executor,
        )
        conn.connect()
        result, _ = conn.query(
            "SELECT * FROM users WHERE id = $1",
            typed_params={"$1": "integer"},
        )
        assert result.row_count == 2

    def test_query_blank_sql_raises(self) -> None:
        conn = _make_connector(schema_provider=_mock_schema_provider)
        conn.connect()
        with pytest.raises(SqlValidationError, match="must not be blank"):
            conn.query("")

    def test_query_write_rejected(self) -> None:
        conn = _make_connector(schema_provider=_mock_schema_provider)
        conn.connect()
        with pytest.raises(SqlValidationError, match="Non-read-only"):
            conn.query("INSERT INTO users (name) VALUES ('test')")


# ---------------------------------------------------------------------------
# Limit enforcement tests
# ---------------------------------------------------------------------------


class TestLimitEnforcement:
    def test_row_limit_enforced(self) -> None:
        def many_rows_executor(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return [{"id": i, "name": f"user_{i}"} for i in range(2000)]

        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=many_rows_executor,
            query_limits=QueryLimits(max_rows=100),
        )
        conn.connect()
        result, _ = conn.query("SELECT id, name FROM users")
        assert result.row_count == 100
        assert result.truncated is True

    def test_byte_limit_enforced(self) -> None:
        def large_row_executor(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return [{"id": 1, "data": "x" * (10 * 1024 * 1024)}]

        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=large_row_executor,
            query_limits=QueryLimits(max_bytes=100),
        )
        conn.connect()
        with pytest.raises(QueryLimitExceeded, match="exceeds limit"):
            conn.query("SELECT id, data FROM users")

    def test_default_limits(self) -> None:
        conn = _make_connector()
        assert conn._query_limits.max_rows == 1000
        assert conn._query_limits.max_bytes == 10 * 1024 * 1024

    def test_custom_limits(self) -> None:
        conn = _make_connector(query_limits=QueryLimits(max_rows=500, max_bytes=1024))
        assert conn._query_limits.max_rows == 500
        assert conn._query_limits.max_bytes == 1024


# ---------------------------------------------------------------------------
# Source Ledger integration tests
# ---------------------------------------------------------------------------


class TestSourceLedgerIntegration:
    def test_query_records_in_ledger(self) -> None:
        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=_mock_query_executor,
        )
        conn.connect()
        _, version = conn.query("SELECT id, name FROM users")

        retrieved = conn.get_version(version.id)
        assert retrieved.id == version.id
        assert retrieved.locator.connector == "postgres"

    def test_query_version_has_frozen_result_metadata(self) -> None:
        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=_mock_query_executor,
        )
        conn.connect()
        _, version = conn.query("SELECT id, name FROM users")

        assert "frozen_result" in version.metadata
        frozen = version.metadata["frozen_result"]
        assert "sql" in frozen
        assert "result_copy_digest" in frozen
        assert "schema_snapshot_digest" in frozen
        assert "row_count" in frozen

    def test_multiple_queries_create_separate_versions(self) -> None:
        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=_mock_query_executor,
        )
        conn.connect()
        _, v1 = conn.query("SELECT id FROM users")
        _, v2 = conn.query("SELECT name FROM users")
        assert v1.id != v2.id

    def test_version_reproducibility_level_recorded(self) -> None:
        conn = _make_connector(
            reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
            schema_provider=_mock_schema_provider,
            query_executor=_mock_query_executor,
        )
        conn.connect()
        _, version = conn.query("SELECT id FROM users")
        assert version.metadata["reproducibility_level"] == "copy_frozen"

    def test_list_versions(self) -> None:
        conn = _make_connector(
            schema_provider=_mock_schema_provider,
            query_executor=_mock_query_executor,
        )
        conn.connect()
        _, v1 = conn.query("SELECT id FROM users")
        versions = conn.list_versions(v1.source_object_id)
        assert len(versions) == 1
        assert versions[0].id == v1.id


# ---------------------------------------------------------------------------
# QueryResult tests
# ---------------------------------------------------------------------------


class TestQueryResultModel:
    def test_frozen_result(self) -> None:
        from pydantic import ValidationError

        result = QueryResult(columns=("a",), column_types=("int",), rows=((1,),), row_count=1)
        with pytest.raises(ValidationError):
            result.row_count = 0  # type: ignore[misc]

    def test_truncated_flag(self) -> None:
        result = QueryResult(
            columns=("a",), column_types=("int",), rows=((1,),), row_count=1, truncated=True
        )
        assert result.truncated is True


# ---------------------------------------------------------------------------
# Schema model tests
# ---------------------------------------------------------------------------


class TestSchemaModels:
    def test_schema_column_frozen(self) -> None:
        from pydantic import ValidationError

        col = SchemaColumn(name="id", data_type="integer", nullable=False, is_primary_key=True)
        with pytest.raises(ValidationError):
            col.name = "changed"  # type: ignore[misc]

    def test_schema_table_frozen(self) -> None:
        from pydantic import ValidationError

        table = SchemaTable(
            schema_name="public",
            table_name="users",
            columns=(SchemaColumn(name="id", data_type="integer"),),
        )
        with pytest.raises(ValidationError):
            table.table_name = "changed"  # type: ignore[misc]

    def test_query_limits_defaults(self) -> None:
        limits = QueryLimits()
        assert limits.max_rows == 1000
        assert limits.max_bytes == 10 * 1024 * 1024
        assert limits.timeout_seconds == 30.0
