"""S5 Context Graph: typed temporal edges in PostgreSQL with source references.

The Context Graph stores relationships between source objects using
typed temporal edges.  Each edge has a source reference (version_id)
and timestamps for valid time.  Edges cannot directly produce Evidence;
they are navigational aids for the Knowledge Planner.

Deletion and rebuild from the Source Ledger are first-class operations.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.contracts import SourceVersion


class EdgeType(StrEnum):
    """Typed temporal edges in the Context Graph."""

    REFERENCES = "references"
    IMPLEMENTS = "implements"
    DEPENDS_ON = "depends_on"
    DERIVED_FROM = "derived_from"
    SUPERSEDES = "supersedes"
    TESTS = "tests"
    IMPORTS = "imports"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextEdge(_FrozenModel):
    """A single typed temporal edge with source provenance."""

    id: UUID
    source_id: UUID = Field(description="Source node id")
    target_id: UUID = Field(description="Target node id")
    edge_type: EdgeType
    source_version_id: UUID = Field(description="SourceVersion that produced this edge")
    valid_from: datetime
    valid_to: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1


class GraphQuery(_FrozenModel):
    """Parameters for querying the Context Graph."""

    source_id: UUID | None = None
    target_id: UUID | None = None
    edge_type: EdgeType | None = None
    source_version_id: UUID | None = None
    active_at: datetime | None = None


class GraphStats(_FrozenModel):
    """Summary statistics for the Context Graph."""

    total_edges: int = Field(ge=0)
    active_edges: int = Field(ge=0)
    edge_types: dict[str, int] = Field(default_factory=dict)


class ContextGraph:
    """In-memory Context Graph backed by typed temporal edges.

    In production this is persisted to PostgreSQL; here we model the
    invariants with in-memory storage to prove correctness.

    Key invariants:
    - Every edge requires a source_version_id (source refs mandatory).
    - Edges are temporal: valid_from/valid_to define the active window.
    - Deletion marks valid_to; rebuild recreates from Source Ledger.
    - The graph cannot directly produce Evidence.
    """

    def __init__(self) -> None:
        self._edges: dict[UUID, ContextEdge] = {}

    def add_edge(
        self,
        *,
        source_id: UUID,
        target_id: UUID,
        edge_type: EdgeType,
        source_version_id: UUID,
        valid_from: datetime,
        valid_to: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContextEdge:
        """Add a typed temporal edge to the graph.

        Args:
            source_id: Source node id.
            target_id: Target node id.
            edge_type: Type of relationship.
            source_version_id: SourceVersion that produced this edge.
            valid_from: When this edge becomes valid.
            valid_to: When this edge expires (None = still active).
            metadata: Optional metadata.

        Returns:
            The created edge.

        Raises:
            ValueError: If valid_to is before valid_from.
        """
        if valid_to is not None and valid_to < valid_from:
            raise ValueError("valid_to must not precede valid_from")

        edge = ContextEdge(
            id=new_id(),
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            source_version_id=source_version_id,
            valid_from=valid_from,
            valid_to=valid_to,
            metadata=metadata or {},
        )
        self._edges[edge.id] = edge
        return edge

    def query(self, query: GraphQuery) -> list[ContextEdge]:
        """Query edges with optional filters.

        Filters are AND-combined.  active_at filters to edges valid at that time.
        Returns matching edges sorted by valid_from descending.
        """
        results: list[ContextEdge] = []
        for edge in self._edges.values():
            if query.source_id is not None and edge.source_id != query.source_id:
                continue
            if query.target_id is not None and edge.target_id != query.target_id:
                continue
            if query.edge_type is not None and edge.edge_type != query.edge_type:
                continue
            if query.source_version_id is not None and edge.source_version_id != query.source_version_id:
                continue
            if query.active_at is not None:
                if edge.valid_from > query.active_at:
                    continue
                if edge.valid_to is not None and edge.valid_to <= query.active_at:
                    continue
            results.append(edge)

        results.sort(key=lambda e: e.valid_from, reverse=True)
        return results

    def delete_edges_for_version(
        self,
        version_id: UUID,
        *,
        reference_time: datetime | None = None,
    ) -> int:
        """Mark all edges from a specific SourceVersion as expired.

        Sets valid_to to reference_time (defaults to utc now).
        Returns the count of affected edges.

        This is the primary deletion mechanism: when a SourceVersion is
        revoked, its edges expire.
        """
        from datetime import UTC, datetime

        now = reference_time or datetime.now(UTC)
        count = 0
        for edge_id, edge in list(self._edges.items()):
            if edge.source_version_id == version_id and edge.valid_to is None:
                updated = edge.model_copy(update={"valid_to": now})
                self._edges[edge_id] = updated
                count += 1
        return count

    def rebuild_from_ledger(
        self,
        versions: list[SourceVersion],
        edge_factory: EdgeFactory,
    ) -> int:
        """Rebuild the graph from Source Ledger versions.

        Deletes all existing edges and recreates them using the edge_factory.
        The factory produces edges from a SourceVersion; versions that don't
        produce edges are silently skipped.

        Returns the count of edges created.
        """
        self._edges.clear()
        count = 0
        for version in versions:
            edges = edge_factory(version)
            for edge in edges:
                self._edges[edge.id] = edge
                count += 1
        return count

    def stats(self) -> GraphStats:
        """Return summary statistics for the graph."""
        active = 0
        type_counts: dict[str, int] = {}
        for edge in self._edges.values():
            type_key = edge.edge_type.value
            type_counts[type_key] = type_counts.get(type_key, 0) + 1
            if edge.valid_to is None:
                active += 1
        return GraphStats(
            total_edges=len(self._edges),
            active_edges=active,
            edge_types=type_counts,
        )

    def clear(self) -> None:
        """Remove all edges from the graph."""
        self._edges.clear()


# Type alias for the edge factory callable.
# A factory takes a SourceVersion and returns zero or more ContextEdge objects.
EdgeFactory = Callable[[SourceVersion], list[ContextEdge]]
