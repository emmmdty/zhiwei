"""S7-T6 Memory Center API — user views, confirm/correct/resolve/revoke/delete/export.

用户查看本人和可见团队/Case memory，按来源/类型/状态筛选，执行
confirm/correct/resolve/revoke/delete/export。团队确认仅 Steward。

事实源：S7 spec §5（Memory Center）、ADR-009。
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc
from zhiwei.identity.domain import ActorContext
from zhiwei.memory.conflicts import ConflictDetector
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
)

logger = logging.getLogger(__name__)

_STEWARD_ROLE_NAMES = frozenset({"memory_steward", "steward", "admin"})


# ── API response / request models ──────────────────────────────────────


class MemoryRecordResponse(BaseModel):
    """Memory record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    version: int
    organization_id: UUID
    workspace_id: UUID
    scope: str
    scope_subject_id: UUID
    type: str
    subject: str
    key: str
    canonical_value: str
    source_refs: list[dict[str, Any]]
    observed_at: str
    confidence: float
    sensitivity: str
    status: str
    author_ref: UUID
    approver_ref: UUID | None
    conflict_refs: list[str]
    created_at: str
    updated_at: str
    tombstone: bool = False


class ConflictResponse(BaseModel):
    """Conflict record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: UUID
    kind: str
    record_a_id: UUID
    record_b_id: UUID
    detected_at: str
    resolved: bool
    resolved_by: UUID | None
    resolved_at: str | None


class ConfirmRequest(BaseModel):
    """Request body for confirming a memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID


class CorrectRequest(BaseModel):
    """Request body for correcting (superseding) a memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID
    canonical_value: str
    subject: str | None = None


class ResolveRequest(BaseModel):
    """Request body for resolving a conflict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: UUID


class RevokeRequest(BaseModel):
    """Request body for revoking a memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID
    reason: str = ""


class DeleteRequest(BaseModel):
    """Request body for deleting a memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID


class ExportRequest(BaseModel):
    """Request body for exporting memory records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str | None = None
    record_type: str | None = None
    status: str | None = None


class ExportResponse(BaseModel):
    """Export response with records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: list[MemoryRecordResponse]
    count: int


class MemoryStatsResponse(BaseModel):
    """Memory center statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_records: int
    by_status: dict[str, int]
    by_scope: dict[str, int]
    by_type: dict[str, int]
    unresolved_conflicts: int


# ── In-memory store (simulates DB RLS) ────────────────────────────────


class _MemoryStore:
    """In-memory per-tenant memory store."""

    def __init__(self) -> None:
        self._records: dict[UUID, MemoryRecord] = {}
        self._conflict_detector = ConflictDetector()

    def add(self, record: MemoryRecord) -> None:
        self._records[record.id] = record

    def get(self, record_id: UUID) -> MemoryRecord | None:
        return self._records.get(record_id)

    def list_for_tenant(
        self,
        organization_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        *,
        scope: MemoryScope | None = None,
        mem_type: MemoryType | None = None,
        mem_status: MemoryStatus | None = None,
        source: str | None = None,
    ) -> list[MemoryRecord]:
        """List records visible to a user within a tenant.

        Visibility rules:
        - User scope: own records (scope_subject_id == user_id)
        - Team scope: visible if user is a member (simulated: always visible)
        - Case scope: visible if user is a member (simulated: always visible)
        """
        results: list[MemoryRecord] = []
        for record in self._records.values():
            if record.organization_id != organization_id:
                continue
            if record.workspace_id != workspace_id:
                continue

            # Scope visibility
            if record.scope == MemoryScope.USER and record.scope_subject_id != user_id:
                continue

            # Filters
            if scope is not None and record.scope != scope:
                continue
            if mem_type is not None and record.type != mem_type:
                continue
            if mem_status is not None and record.status != mem_status:
                continue
            if source is not None:
                has_source = any(sr.source_type == source for sr in record.source_refs)
                if not has_source:
                    continue

            results.append(record)

        return results


_store = _MemoryStore()


# ── Router factory ─────────────────────────────────────────────────────


def create_memory_router(
    *,
    actor_dependency: Callable[[], ActorContext],
) -> APIRouter:
    """Create the memory center API router."""
    router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

    @router.get("/records", response_model=list[MemoryRecordResponse])
    async def list_memory_records(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
        scope: str | None = Query(None),
        type: str | None = Query(None, alias="type"),
        status_filter: str | None = Query(None, alias="status"),
        source: str | None = Query(None),
    ) -> list[MemoryRecordResponse]:
        ctx = _TenantContext(actor)
        scope_enum = MemoryScope(scope) if scope else None
        type_enum = MemoryType(type) if type else None
        status_enum = MemoryStatus(status_filter) if status_filter else None

        records = _store.list_for_tenant(
            ctx.organization_id,
            ctx.workspace_id,
            ctx.user_id,
            scope=scope_enum,
            mem_type=type_enum,
            mem_status=status_enum,
            source=source,
        )
        return [_to_response(r) for r in records]

    @router.get("/records/{record_id}", response_model=MemoryRecordResponse)
    async def get_memory_record(
        record_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryRecordResponse:
        record = _store.get(record_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory record not found",
            )
        ctx = _TenantContext(actor)
        if record.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory record not found",
            )
        return _to_response(record)

    @router.post("/records/{record_id}/confirm", response_model=MemoryRecordResponse)
    async def confirm_record(
        record_id: UUID,
        request: ConfirmRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryRecordResponse:
        record = _store.get(record_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory record not found",
            )

        ctx = _TenantContext(actor)

        # Team memory: only Steward can confirm
        if record.scope == MemoryScope.TEAM and not ctx.is_steward:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="only Memory Steward can confirm team records",
            )

        if record.status != MemoryStatus.CANDIDATE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"record status is {record.status.value}, expected candidate",
            )

        now = ensure_utc(datetime.now(tz=UTC))
        confirmed = record.model_copy(
            update={
                "status": MemoryStatus.CONFIRMED,
                "approver_ref": ctx.user_id,
                "updated_at": now,
            }
        )
        _store.add(confirmed)
        return _to_response(confirmed)

    @router.post("/records/{record_id}/correct", response_model=MemoryRecordResponse)
    async def correct_record(
        record_id: UUID,
        request: CorrectRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryRecordResponse:
        original = _store.get(record_id)
        if original is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory record not found",
            )

        ctx = _TenantContext(actor)
        now = ensure_utc(datetime.now(tz=UTC))

        # Mark original as superseded
        superseded = original.model_copy(
            update={
                "status": MemoryStatus.SUPERSEDED,
                "updated_at": now,
            }
        )
        _store.add(superseded)

        # Create corrected version
        corrected = original.model_copy(
            update={
                "id": new_id(),
                "version": original.version + 1,
                "canonical_value": request.canonical_value,
                "subject": request.subject or original.subject,
                "status": MemoryStatus.CONFIRMED,
                "approver_ref": ctx.user_id,
                "superseded_by": original.id,
                "created_at": now,
                "updated_at": now,
            }
        )
        _store.add(corrected)
        return _to_response(corrected)

    @router.post("/records/{record_id}/revoke", response_model=MemoryRecordResponse)
    async def revoke_record(
        record_id: UUID,
        request: RevokeRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryRecordResponse:
        record = _store.get(record_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory record not found",
            )

        if record.terminal_status():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"record is already in terminal status: {record.status.value}",
            )

        now = ensure_utc(datetime.now(tz=UTC))
        revoked = record.model_copy(
            update={
                "status": MemoryStatus.REVOKED,
                "revoked_reason": request.reason,
                "tombstone": True,
                "updated_at": now,
            }
        )
        _store.add(revoked)
        return _to_response(revoked)

    @router.post("/records/{record_id}/delete", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_record(
        record_id: UUID,
        request: DeleteRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> None:
        record = _store.get(record_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory record not found",
            )

        now = ensure_utc(datetime.now(tz=UTC))

        # Soft delete: revoke with tombstone
        deleted = record.model_copy(
            update={
                "status": MemoryStatus.REVOKED,
                "revoked_reason": "user delete",
                "tombstone": True,
                "updated_at": now,
            }
        )
        _store.add(deleted)

    @router.post("/conflicts/resolve", response_model=ConflictResponse)
    async def resolve_conflict(
        request: ResolveRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConflictResponse:
        resolved = _store._conflict_detector.resolve_conflict(
            request.conflict_id, actor.principal_id
        )
        if resolved is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conflict not found or already resolved",
            )
        return ConflictResponse(
            conflict_id=resolved.conflict_id,
            kind=resolved.kind.value,
            record_a_id=resolved.record_a_id,
            record_b_id=resolved.record_b_id,
            detected_at=resolved.detected_at.isoformat(),
            resolved=resolved.resolved,
            resolved_by=resolved.resolved_by,
            resolved_at=resolved.resolved_at.isoformat() if resolved.resolved_at else None,
        )

    @router.get("/conflicts", response_model=list[ConflictResponse])
    async def list_conflicts(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ConflictResponse]:
        conflicts = _store._conflict_detector.get_unresolved_conflicts()
        return [
            ConflictResponse(
                conflict_id=c.conflict_id,
                kind=c.kind.value,
                record_a_id=c.record_a_id,
                record_b_id=c.record_b_id,
                detected_at=c.detected_at.isoformat(),
                resolved=c.resolved,
                resolved_by=c.resolved_by,
                resolved_at=c.resolved_at.isoformat() if c.resolved_at else None,
            )
            for c in conflicts
        ]

    @router.post("/export", response_model=ExportResponse)
    async def export_records(
        request: ExportRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ExportResponse:
        ctx = _TenantContext(actor)
        scope_enum = MemoryScope(request.scope) if request.scope else None
        type_enum = MemoryType(request.record_type) if request.record_type else None
        status_enum = MemoryStatus(request.status) if request.status else None

        records = _store.list_for_tenant(
            ctx.organization_id,
            ctx.workspace_id,
            ctx.user_id,
            scope=scope_enum,
            mem_type=type_enum,
            mem_status=status_enum,
        )
        return ExportResponse(
            records=[_to_response(r) for r in records],
            count=len(records),
        )

    @router.get("/stats", response_model=MemoryStatsResponse)
    async def get_stats(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryStatsResponse:
        ctx = _TenantContext(actor)
        records = _store.list_for_tenant(
            ctx.organization_id, ctx.workspace_id, ctx.user_id
        )

        by_status: dict[str, int] = {}
        by_scope: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for r in records:
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
            by_scope[r.scope.value] = by_scope.get(r.scope.value, 0) + 1
            by_type[r.type.value] = by_type.get(r.type.value, 0) + 1

        unresolved = _store._conflict_detector.get_unresolved_conflicts()

        return MemoryStatsResponse(
            total_records=len(records),
            by_status=by_status,
            by_scope=by_scope,
            by_type=by_type,
            unresolved_conflicts=len(unresolved),
        )

    return router


# ── Helpers ────────────────────────────────────────────────────────────


class _TenantContext:
    """Minimal tenant context extracted from actor."""

    def __init__(self, actor: ActorContext) -> None:
        if actor.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization context required",
            )
        if actor.workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="workspace context required",
            )
        self.organization_id = actor.organization_id
        self.workspace_id = actor.workspace_id
        self.user_id = actor.principal_id
        self.is_steward = any(
            rb.name in _STEWARD_ROLE_NAMES for rb in actor.role_bindings
        )


def _to_response(record: MemoryRecord) -> MemoryRecordResponse:
    """Convert MemoryRecord to API response model."""
    return MemoryRecordResponse(
        id=record.id,
        version=record.version,
        organization_id=record.organization_id,
        workspace_id=record.workspace_id,
        scope=record.scope.value,
        scope_subject_id=record.scope_subject_id,
        type=record.type.value,
        subject=record.subject,
        key=record.key,
        canonical_value=record.canonical_value,
        source_refs=[
            {
                "source_id": sr.source_id,
                "source_type": sr.source_type,
                "description": sr.description,
            }
            for sr in record.source_refs
        ],
        observed_at=record.observed_at.isoformat(),
        confidence=record.confidence,
        sensitivity=record.sensitivity.value,
        status=record.status.value,
        author_ref=record.author_ref,
        approver_ref=record.approver_ref,
        conflict_refs=[str(c) for c in record.conflict_refs],
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        tombstone=record.tombstone,
    )
