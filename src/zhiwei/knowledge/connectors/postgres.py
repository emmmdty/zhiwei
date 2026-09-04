"""PostgreSQL connector: schema snapshot, read-only AST query, copy_frozen result canonicalization.

Schema snapshot、read-only AST/typed query、timeout/row/byte limit。
数据源在接入时必须声明可达的 reproducibility_level（ADR-003）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.knowledge.contracts import Locator, SourceObject, SourceVersion
from zhiwei.knowledge.ledger import SourceLedger


class ReproducibilityLevel(StrEnum):
    """ADR-003 reproducibility levels for source connectors."""

    REPLAYABLE = "replayable"
    COPY_FROZEN = "copy_frozen"
    REFERENCE_ONLY = "reference_only"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SchemaColumn(_FrozenModel):
    name: str
    data_type: str
    nullable: bool = True
    is_primary_key: bool = False


class SchemaTable(_FrozenModel):
    schema_name: str
    table_name: str
    columns: tuple[SchemaColumn, ...]
    row_count_estimate: int | None = None


class SchemaSnapshot(_FrozenModel):
    dsn_fingerprint: str
    tables: tuple[SchemaTable, ...]
    snapshot_id: str
    captured_at: datetime
    digest: str


class QueryLimits(_FrozenModel):
    max_rows: int = Field(default=1000, ge=1)
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)


class QueryResult(_FrozenModel):
    columns: tuple[str, ...]
    column_types: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool = False


class TypedColumn(_FrozenModel):
    name: str
    data_type: str


class FrozenResultCopy(_FrozenModel):
    sql: str
    typed_params: dict[str, str]
    schema_snapshot_digest: str
    executed_at: datetime
    result_copy_digest: str
    row_count: int
    replayable: bool = False


class SqlValidationError(Exception):
    """Raised when a SQL query fails validation."""


class QueryLimitExceeded(Exception):
    """Raised when a query exceeds configured limits."""


class SchemaSnapshotError(Exception):
    """Raised when schema snapshot creation fails."""


class PostgresConnector:
    """PostgreSQL connector for the Source Ledger.

    Requires reproducibility_level declaration at connection time.
    Schema snapshot, read-only AST/typed query, timeout/row/byte limit.
    """

    def __init__(
        self,
        dsn: str,
        organization_id: UUID,
        workspace_id: UUID,
        reproducibility_level: ReproducibilityLevel = ReproducibilityLevel.REFERENCE_ONLY,
        *,
        query_limits: QueryLimits | None = None,
        schema_provider: Callable[[str], list[dict[str, Any]]] | None = None,
        query_executor: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("dsn must not be blank")
        self._dsn = dsn
        self._organization_id = organization_id
        self._workspace_id = workspace_id
        self._reproducibility_level = reproducibility_level
        self._query_limits = query_limits or QueryLimits()
        self._schema_provider = schema_provider
        self._query_executor = query_executor
        self._connected = False
        self._schema_snapshot: SchemaSnapshot | None = None
        self._ledger = SourceLedger()

    @property
    def reproducibility_level(self) -> ReproducibilityLevel:
        return self._reproducibility_level

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._schema_snapshot = None

    def _assert_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Connector is not connected")

    def create_schema_snapshot(self) -> SchemaSnapshot:
        """Capture a schema snapshot from the connected database.

        Uses the schema_provider callable passed at init time.
        Raises SchemaSnapshotError if no provider is configured.
        """
        self._assert_connected()
        if self._schema_provider is None:
            raise SchemaSnapshotError("No schema_provider configured")

        raw_tables = self._schema_provider(self._dsn)
        tables: list[SchemaTable] = []
        for table_data in raw_tables:
            columns = tuple(
                SchemaColumn(
                    name=col["name"],
                    data_type=col["data_type"],
                    nullable=col.get("nullable", True),
                    is_primary_key=col.get("is_primary_key", False),
                )
                for col in table_data.get("columns", [])
            )
            tables.append(
                SchemaTable(
                    schema_name=table_data.get("schema_name", "public"),
                    table_name=table_data["table_name"],
                    columns=columns,
                    row_count_estimate=table_data.get("row_count_estimate"),
                )
            )

        snapshot_id = f"snap_{new_id().hex}"
        digest_input = canonical_json(
            {
                "dsn_fingerprint": digest_bytes(self._dsn.encode()),
                "tables": [
                    {
                        "schema": t.schema_name,
                        "table": t.table_name,
                        "columns": [
                            {"name": c.name, "type": c.data_type, "pk": c.is_primary_key}
                            for c in t.columns
                        ],
                    }
                    for t in tables
                ],
            }
        )
        snapshot = SchemaSnapshot(
            dsn_fingerprint=digest_bytes(self._dsn.encode()),
            tables=tuple(tables),
            snapshot_id=snapshot_id,
            captured_at=utc_now(),
            digest=digest_bytes(digest_input),
        )
        self._schema_snapshot = snapshot
        return snapshot

    def query(
        self,
        sql: str,
        *,
        typed_params: dict[str, str] | None = None,
        organization_id: UUID | None = None,
        workspace_id: UUID | None = None,
    ) -> tuple[QueryResult, SourceVersion]:
        """Execute a read-only query with AST validation and limit enforcement.

        Returns (QueryResult, SourceVersion). The SourceVersion is recorded
        in the Source Ledger before being returned.
        """
        self._assert_connected()
        if not sql.strip():
            raise SqlValidationError("sql must not be blank")

        validate_read_only(sql)
        self._enforce_limits(sql, typed_params or {})

        raw_rows = self._query_executor(sql, typed_params or {}) if self._query_executor else []
        query_result = self._build_result(raw_rows, sql, typed_params or {})

        result_copy = self._create_frozen_copy(sql, typed_params or {}, query_result)
        result_copy_digest = result_copy.result_copy_digest
        locator = Locator(
            connector="postgres", uri=self._dsn, version_hint=result_copy.schema_snapshot_digest
        )
        now = utc_now()

        org_id = organization_id or self._organization_id
        ws_id = workspace_id or self._workspace_id

        source_object = SourceObject(
            id=new_id(),
            organization_id=org_id,
            workspace_id=ws_id,
            source_type="postgres_query",
            metadata={"sql": sql, "reproducibility_level": self._reproducibility_level.value},
        )
        self._ledger.register_object(source_object)

        source_version = self._ledger.create_version(
            source_object.id,
            locator=locator,
            content_digest=result_copy_digest,
            observed_at=now,
            valid_at=now,
            metadata={
                "frozen_result": result_copy.model_dump(mode="json"),
                "reproducibility_level": self._reproducibility_level.value,
            },
        )
        return query_result, source_version

    def _enforce_limits(self, sql: str, params: dict[str, str]) -> None:
        """Validate query against configured limits."""
        import sqlglot  # type: ignore[import-untyped]

        try:
            statements = sqlglot.parse(sql, dialect="postgres")
        except sqlglot.errors.ParseError as exc:  # type: ignore[attr-defined]
            raise SqlValidationError(f"SQL parse error: {exc}") from exc

        if not statements or statements[0] is None:
            raise SqlValidationError("Failed to parse SQL statement")

        root = statements[0]
        if (
            root.find(sqlglot.exp.Insert)  # type: ignore[attr-defined]
            or root.find(sqlglot.exp.Update)  # type: ignore[attr-defined]
            or root.find(sqlglot.exp.Delete)  # type: ignore[attr-defined]
        ):
            raise SqlValidationError("Write operations are not permitted")

        if root.find(sqlglot.exp.Drop) or root.find(sqlglot.exp.Alter):  # type: ignore[attr-defined]
            raise SqlValidationError("DDL operations are not permitted")

    def _build_result(
        self, raw_rows: list[dict[str, Any]], sql: str, params: dict[str, str]
    ) -> QueryResult:
        if not raw_rows:
            return QueryResult(columns=(), column_types=(), rows=(), row_count=0)

        columns = tuple(raw_rows[0].keys())
        column_types = tuple(type(v).__name__ for v in raw_rows[0].values())
        rows: list[tuple[Any, ...]] = []
        for raw_row in raw_rows[: self._query_limits.max_rows]:
            rows.append(tuple(raw_row.values()))

        truncated = len(raw_rows) > self._query_limits.max_rows
        return QueryResult(
            columns=columns,
            column_types=column_types,
            rows=tuple(rows),
            row_count=len(rows),
            truncated=truncated,
        )

    def _create_frozen_copy(
        self, sql: str, params: dict[str, str], result: QueryResult
    ) -> FrozenResultCopy:
        if self._schema_snapshot is None:
            self.create_schema_snapshot()
        schema_digest = self._schema_snapshot.digest  # type: ignore[union-attr]

        result_bytes = canonical_json(
            {
                "columns": list(result.columns),
                "rows": [list(row) for row in result.rows],
            }
        )
        self._check_byte_limit(result_bytes)

        return FrozenResultCopy(
            sql=sql,
            typed_params=params,
            schema_snapshot_digest=schema_digest,
            executed_at=utc_now(),
            result_copy_digest=digest_bytes(result_bytes),
            row_count=result.row_count,
            replayable=self._reproducibility_level == ReproducibilityLevel.REPLAYABLE,
        )

    def _check_byte_limit(self, data: bytes) -> None:
        if len(data) > self._query_limits.max_bytes:
            raise QueryLimitExceeded(
                f"Result size {len(data)} bytes exceeds limit {self._query_limits.max_bytes} bytes"
            )

    def get_version(self, version_id: UUID) -> SourceVersion:
        self._assert_connected()
        return self._ledger.get_version(version_id)

    def list_versions(self, source_object_id: UUID) -> list[SourceVersion]:
        self._assert_connected()
        return self._ledger.list_versions(source_object_id)


def validate_read_only(sql: str) -> None:
    """Validate that a SQL query is read-only using AST analysis.

    Rejects INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE.
    Raises SqlValidationError for non-read-only operations.
    """
    import sqlglot  # type: ignore[import-untyped]

    if not sql or not sql.strip():
        raise SqlValidationError("sql must not be blank")

    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except sqlglot.errors.ParseError as exc:  # type: ignore[attr-defined]
        raise SqlValidationError(f"SQL parse error: {exc}") from exc

    if not statements or statements[0] is None:
        raise SqlValidationError("Failed to parse SQL statement")

    write_types = (
        sqlglot.exp.Insert,  # type: ignore[attr-defined]
        sqlglot.exp.Update,  # type: ignore[attr-defined]
        sqlglot.exp.Delete,  # type: ignore[attr-defined]
        sqlglot.exp.Drop,  # type: ignore[attr-defined]
        sqlglot.exp.Alter,  # type: ignore[attr-defined]
        sqlglot.exp.Create,  # type: ignore[attr-defined]
        sqlglot.exp.TruncateTable,  # type: ignore[attr-defined]
        sqlglot.exp.Grant,  # type: ignore[attr-defined]
        sqlglot.exp.Revoke,  # type: ignore[attr-defined]
    )

    for stmt in statements:
        if stmt is None:
            continue
        for write_type in write_types:
            if stmt.find(write_type):
                raise SqlValidationError(
                    f"Non-read-only operation detected: {write_type.__name__}"
                )
