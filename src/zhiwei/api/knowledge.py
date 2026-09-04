"""S5-T7: Knowledge management API — source CRUD, sync, status, version, ACL, disable.

事实源：S5 spec §7、T7 plan。

- GET  /api/v1/knowledge/sources — list knowledge sources
- POST /api/v1/knowledge/sources — add a new source
- POST /api/v1/knowledge/sources/{id}/connect — connect (enable sync)
- POST /api/v1/knowledge/sources/{id}/sync — trigger sync
- GET  /api/v1/knowledge/sources/{id}/status — source status (freshness, ACL, score)
- GET  /api/v1/knowledge/sources/{id}/versions — list source versions
- PUT  /api/v1/knowledge/sources/{id}/acl — update source ACL
- POST /api/v1/knowledge/sources/{id}/disable — disable source
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.domain import ActorContext
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceObject,
)
from zhiwei.knowledge.freshness import FreshnessPolicy, FreshnessState, evaluate_freshness
from zhiwei.knowledge.ledger import SourceLedger


class _TenantContext:
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


class SourceRecord(BaseModel):
    """Source record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    source_type: str
    connector: str
    uri: str
    classification: str
    status: str
    version_count: int
    latest_version_seq: int | None
    latest_content_digest: str | None
    acl_allowed_principals: tuple[str, ...]
    acl_denied_principals: tuple[str, ...]
    acl_allowed_groups: tuple[str, ...]


class AddSourceRequest(BaseModel):
    """POST body for adding a knowledge source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    classification: str = "PUBLIC"
    acl_allowed_principals: tuple[str, ...] = Field(default_factory=tuple)
    acl_denied_principals: tuple[str, ...] = Field(default_factory=tuple)
    acl_allowed_groups: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SyncRequest(BaseModel):
    """POST body for triggering a sync."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    force: bool = False


class UpdateACLRequest(BaseModel):
    """PUT body for updating source ACL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_principals: tuple[str, ...] = Field(default_factory=tuple)
    denied_principals: tuple[str, ...] = Field(default_factory=tuple)
    allowed_groups: tuple[str, ...] = Field(default_factory=tuple)


class SourceStatusRecord(BaseModel):
    """Source status with freshness, ACL, and score breakdown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    status: str
    version_seq: int | None
    content_digest: str | None
    locator_connector: str | None
    locator_uri: str | None
    freshness_state: str
    acl_allowed: bool
    acl_reason: str
    classification: str
    score_breakdown: dict[str, Any] = Field(default_factory=dict)


class SourceVersionRecord(BaseModel):
    """A single source version record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    source_object_id: UUID
    version_seq: int
    connector: str
    uri: str
    content_digest: str
    state: str
    classification: str
    observed_at: datetime
    valid_at: datetime
    connector_version: str
    parser_version: str
    index_version: str


class SyncResultRecord(BaseModel):
    """Result of a sync operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    sync_status: str
    versions_created: int
    versions_marked_stale: int
    connector: str
    sync_watermark: str | None
    error: str | None = None


class _SourceStore:
    """In-memory per-tenant source store."""

    def __init__(self) -> None:
        self._objects: dict[UUID, SourceObject] = {}
        self._ledgers: dict[tuple[UUID, UUID], SourceLedger] = {}
        self._status: dict[UUID, str] = {}  # source_id -> "active"/"disabled"/"error"
        self._sync_errors: dict[UUID, str] = {}

    def _ledger_key(self, org_id: UUID, ws_id: UUID) -> tuple[UUID, UUID]:
        return (org_id, ws_id)

    def get_ledger(self, ctx: _TenantContext) -> SourceLedger:
        key = self._ledger_key(ctx.organization_id, ctx.workspace_id)
        if key not in self._ledgers:
            self._ledgers[key] = SourceLedger()
        return self._ledgers[key]

    def get_object(self, source_id: UUID) -> SourceObject | None:
        return self._objects.get(source_id)

    def store_object(self, obj: SourceObject) -> None:
        self._objects[obj.id] = obj
        self._status[obj.id] = "active"

    def list_objects(self, ctx: _TenantContext) -> list[SourceObject]:
        return [
            obj
            for obj in self._objects.values()
            if obj.organization_id == ctx.organization_id
            and obj.workspace_id == ctx.workspace_id
        ]

    def get_status(self, source_id: UUID) -> str:
        return self._status.get(source_id, "unknown")

    def set_status(self, source_id: UUID, source_status: str) -> None:
        self._status[source_id] = source_status

    def set_sync_error(self, source_id: UUID, error: str) -> None:
        self._sync_errors[source_id] = error

    def get_sync_error(self, source_id: UUID) -> str | None:
        return self._sync_errors.get(source_id)


_store = _SourceStore()


def create_knowledge_router(
    *,
    actor_dependency: Callable[[], ActorContext],
) -> APIRouter:
    """Create the knowledge sources API router."""
    router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

    @router.get("/sources", response_model=list[SourceRecord])
    async def list_sources(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[SourceRecord]:
        ctx = _TenantContext(actor)
        ledger = _store.get_ledger(ctx)
        objects = _store.list_objects(ctx)
        records = []
        for obj in objects:
            latest = ledger.latest_version(obj.id)
            records.append(
                SourceRecord(
                    id=obj.id,
                    source_type=obj.source_type,
                    connector=latest.locator.connector if latest else "",
                    uri=latest.locator.uri if latest else "",
                    classification=obj.classification.value,
                    status=_store.get_status(obj.id),
                    version_count=len(ledger.list_versions(obj.id)),
                    latest_version_seq=latest.version_seq if latest else None,
                    latest_content_digest=latest.content_digest if latest else None,
                    acl_allowed_principals=obj.acl.allowed_principals,
                    acl_denied_principals=obj.acl.denied_principals,
                    acl_allowed_groups=obj.acl.allowed_groups,
                )
            )
        return records

    @router.post(
        "/sources",
        status_code=status.HTTP_201_CREATED,
        response_model=SourceRecord,
    )
    async def add_source(
        request: AddSourceRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceRecord:
        ctx = _TenantContext(actor)
        try:
            classification = Classification(request.classification)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid classification: {request.classification}",
            ) from exc
        obj = SourceObject(
            id=new_id(),
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            source_type=request.source_type,
            acl=ACLSnapshot(
                allowed_principals=request.acl_allowed_principals,
                denied_principals=request.acl_denied_principals,
                allowed_groups=request.acl_allowed_groups,
            ),
            classification=classification,
            metadata=request.metadata,
        )
        _store.store_object(obj)
        ledger = _store.get_ledger(ctx)
        ledger.register_object(obj)
        return SourceRecord(
            id=obj.id,
            source_type=obj.source_type,
            connector="",
            uri="",
            classification=obj.classification.value,
            status="active",
            version_count=0,
            latest_version_seq=None,
            latest_content_digest=None,
            acl_allowed_principals=obj.acl.allowed_principals,
            acl_denied_principals=obj.acl.denied_principals,
            acl_allowed_groups=obj.acl.allowed_groups,
        )

    @router.post(
        "/sources/{source_id}/connect",
        response_model=SourceRecord,
    )
    async def connect_source(
        source_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceRecord:
        ctx = _TenantContext(actor)
        obj = _store.get_object(source_id)
        if obj is None or obj.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="source not found",
            )
        _store.set_status(source_id, "active")
        ledger = _store.get_ledger(ctx)
        latest = ledger.latest_version(obj.id)
        return SourceRecord(
            id=obj.id,
            source_type=obj.source_type,
            connector=latest.locator.connector if latest else "",
            uri=latest.locator.uri if latest else "",
            classification=obj.classification.value,
            status="active",
            version_count=len(ledger.list_versions(obj.id)),
            latest_version_seq=latest.version_seq if latest else None,
            latest_content_digest=latest.content_digest if latest else None,
            acl_allowed_principals=obj.acl.allowed_principals,
            acl_denied_principals=obj.acl.denied_principals,
            acl_allowed_groups=obj.acl.allowed_groups,
        )

    @router.post(
        "/sources/{source_id}/sync",
        response_model=SyncResultRecord,
    )
    async def sync_source(
        source_id: UUID,
        request: SyncRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SyncResultRecord:
        ctx = _TenantContext(actor)
        obj = _store.get_object(source_id)
        if obj is None or obj.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="source not found",
            )
        if _store.get_status(source_id) == "disabled":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="source is disabled",
            )
        ledger = _store.get_ledger(ctx)
        versions_before = ledger.list_versions(obj.id)

        now = datetime.now(UTC)
        locator = Locator(connector=obj.source_type, uri=f"source://{source_id}")
        digest = f"sha256:{'a' * 64}"

        if not request.force:
            existing_digests = {v.content_digest for v in versions_before}
            if digest in existing_digests:
                return SyncResultRecord(
                    source_id=source_id,
                    sync_status="unchanged",
                    versions_created=0,
                    versions_marked_stale=0,
                    connector=obj.source_type,
                    sync_watermark=None,
                )

        if versions_before:
            latest = versions_before[-1]
            ledger.mark_stale(latest.id)

        try:
            new_version = ledger.create_version(
                obj.id,
                locator=locator,
                content_digest=digest,
                observed_at=now,
                valid_at=now,
            )
        except Exception as exc:
            _store.set_sync_error(source_id, str(exc))
            _store.set_status(source_id, "error")
            return SyncResultRecord(
                source_id=source_id,
                sync_status="failed",
                versions_created=0,
                versions_marked_stale=0,
                connector=obj.source_type,
                sync_watermark=None,
                error=str(exc),
            )

        return SyncResultRecord(
            source_id=source_id,
            sync_status="completed",
            versions_created=1,
            versions_marked_stale=1 if versions_before else 0,
            connector=obj.source_type,
            sync_watermark=str(new_version.id),
        )

    @router.get(
        "/sources/{source_id}/status",
        response_model=SourceStatusRecord,
    )
    async def source_status(
        source_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceStatusRecord:
        ctx = _TenantContext(actor)
        obj = _store.get_object(source_id)
        if obj is None or obj.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="source not found",
            )
        ledger = _store.get_ledger(ctx)
        latest = ledger.latest_version(obj.id)
        source_status = _store.get_status(source_id)

        freshness_state = FreshnessState.EXPIRED.value
        acl_allowed = False
        acl_reason = "no_version"
        score_breakdown: dict[str, Any] = {}

        if latest:
            fp = FreshnessPolicy(connector=obj.source_type)
            fr = evaluate_freshness(latest, fp)
            freshness_state = fr.state.value

            principal_str = str(actor.principal_id)
            current_acl = obj.acl
            if principal_str in current_acl.denied_principals:
                acl_reason = "denied_principal"
            elif principal_str in current_acl.allowed_principals:
                acl_allowed = True
                acl_reason = "allowed"
            elif not current_acl.allowed_principals and not current_acl.allowed_groups:
                acl_reason = "unknown"
            else:
                acl_reason = "not_in_acl"

            score_breakdown = {
                "acl_score": 1.0 if acl_allowed else 0.0,
                "freshness_score": {
                    FreshnessState.FRESH: 1.0,
                    FreshnessState.AGING: 0.7,
                    FreshnessState.STALE: 0.3,
                    FreshnessState.EXPIRED: 0.0,
                }.get(fr.state, 0.0),
            }

        return SourceStatusRecord(
            source_id=source_id,
            status=source_status,
            version_seq=latest.version_seq if latest else None,
            content_digest=latest.content_digest if latest else None,
            locator_connector=latest.locator.connector if latest else None,
            locator_uri=latest.locator.uri if latest else None,
            freshness_state=freshness_state,
            acl_allowed=acl_allowed,
            acl_reason=acl_reason,
            classification=obj.classification.value,
            score_breakdown=score_breakdown,
        )

    @router.get(
        "/sources/{source_id}/versions",
        response_model=list[SourceVersionRecord],
    )
    async def list_source_versions(
        source_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[SourceVersionRecord]:
        ctx = _TenantContext(actor)
        obj = _store.get_object(source_id)
        if obj is None or obj.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="source not found",
            )
        ledger = _store.get_ledger(ctx)
        versions = ledger.list_versions(obj.id)
        return [
            SourceVersionRecord(
                id=v.id,
                source_object_id=v.source_object_id,
                version_seq=v.version_seq,
                connector=v.locator.connector,
                uri=v.locator.uri,
                content_digest=v.content_digest,
                state=v.state.value,
                classification=v.classification.value,
                observed_at=v.observed_at,
                valid_at=v.valid_at,
                connector_version=v.connector_version,
                parser_version=v.parser_version,
                index_version=v.index_version,
            )
            for v in versions
        ]

    @router.put(
        "/sources/{source_id}/acl",
        response_model=SourceRecord,
    )
    async def update_source_acl(
        source_id: UUID,
        request: UpdateACLRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceRecord:
        ctx = _TenantContext(actor)
        obj = _store.get_object(source_id)
        if obj is None or obj.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="source not found",
            )
        updated = obj.model_copy(
            update={
                "acl": ACLSnapshot(
                    allowed_principals=request.allowed_principals,
                    denied_principals=request.denied_principals,
                    allowed_groups=request.allowed_groups,
                )
            }
        )
        _store.store_object(updated)
        ledger = _store.get_ledger(ctx)
        latest = ledger.latest_version(updated.id)
        return SourceRecord(
            id=updated.id,
            source_type=updated.source_type,
            connector=latest.locator.connector if latest else "",
            uri=latest.locator.uri if latest else "",
            classification=updated.classification.value,
            status=_store.get_status(updated.id),
            version_count=len(ledger.list_versions(updated.id)),
            latest_version_seq=latest.version_seq if latest else None,
            latest_content_digest=latest.content_digest if latest else None,
            acl_allowed_principals=updated.acl.allowed_principals,
            acl_denied_principals=updated.acl.denied_principals,
            acl_allowed_groups=updated.acl.allowed_groups,
        )

    @router.post(
        "/sources/{source_id}/disable",
        response_model=SourceRecord,
    )
    async def disable_source(
        source_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> SourceRecord:
        ctx = _TenantContext(actor)
        obj = _store.get_object(source_id)
        if obj is None or obj.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="source not found",
            )
        _store.set_status(source_id, "disabled")
        ledger = _store.get_ledger(ctx)
        latest = ledger.latest_version(obj.id)
        return SourceRecord(
            id=obj.id,
            source_type=obj.source_type,
            connector=latest.locator.connector if latest else "",
            uri=latest.locator.uri if latest else "",
            classification=obj.classification.value,
            status="disabled",
            version_count=len(ledger.list_versions(obj.id)),
            latest_version_seq=latest.version_seq if latest else None,
            latest_content_digest=latest.content_digest if latest else None,
            acl_allowed_principals=obj.acl.allowed_principals,
            acl_denied_principals=obj.acl.denied_principals,
            acl_allowed_groups=obj.acl.allowed_groups,
        )

    return router
