"""SQL query handling: AST validation, typed params, limit enforcement, result canonicalization.

Schema snapshot、read-only AST/typed query、timeout/row/byte limit。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TypedParam(_FrozenModel):
    """A typed parameter for SQL query execution."""

    name: str = Field(min_length=1)
    sql_type: str = Field(min_length=1, alias="type")
    value: Any = None


class ParsedQuery(_FrozenModel):
    """A parsed and validated SQL query."""

    sql: str
    typed_params: tuple[TypedParam, ...] = Field(default_factory=tuple)
    is_read_only: bool = True
    table_references: tuple[str, ...] = Field(default_factory=tuple)
    statement_type: str = "SELECT"


def parse_sql(sql: str, *, dialect: str = "postgres") -> ParsedQuery:
    """Parse and validate a SQL query.

    Returns a ParsedQuery with AST analysis results.
    Raises SqlValidationError for invalid or non-read-only queries.
    """
    from zhiwei.knowledge.connectors.postgres import SqlValidationError, validate_read_only

    if not sql or not sql.strip():
        raise SqlValidationError("sql must not be blank")

    validate_read_only(sql)

    import sqlglot  # type: ignore[import-untyped]

    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except sqlglot.errors.ParseError as exc:  # type: ignore[attr-defined]
        raise SqlValidationError(f"SQL parse error: {exc}") from exc

    if not statements or statements[0] is None:
        raise SqlValidationError("Failed to parse SQL statement")

    root = statements[0]
    statement_type = type(root).__name__
    table_references = _extract_table_references(root)

    return ParsedQuery(
        sql=sql.strip(),
        is_read_only=True,
        table_references=tuple(table_references),
        statement_type=statement_type,
    )


def build_typed_params(params: dict[str, Any]) -> tuple[TypedParam, ...]:
    """Build typed parameters from a dict of name-value pairs.

    Infers SQL types from Python types.
    """
    result: list[TypedParam] = []
    for name, value in params.items():
        sql_type = _infer_sql_type(value)
        result.append(TypedParam(name=name, type=sql_type, value=value))
    return tuple(result)


def _infer_sql_type(value: Any) -> str:
    """Infer SQL type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "text"
    if isinstance(value, dict):
        return "jsonb"
    if isinstance(value, list):
        return "jsonb"
    return "text"


def _extract_table_references(root: Any) -> list[str]:
    """Extract table references from a SQL AST node."""
    import sqlglot  # type: ignore[import-untyped]

    tables: list[str] = []
    for table_node in root.find_all(sqlglot.exp.Table):  # type: ignore[attr-defined]
        if table_node.name:
            tables.append(table_node.name)
    return list(dict.fromkeys(tables))
