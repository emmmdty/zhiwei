"""S4-T8：Connection API——Connection CRUD + OAuth/status + suspend/revoke。

事实源：S4 spec §5（Connection and execution）、§6（Web journey）、T8 plan。

- GET /api/v1/connections — list connections
- POST /api/v1/connections — create connection
- GET /api/v1/connections/{id} — get connection detail
- GET /api/v1/connections/{id}/status — connection status + credential fingerprint
- POST /api/v1/connections/{id}/actions — suspend/revoke
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from zhiwei.capabilities.connections import (
    Connection,
    ConnectionStatus,
    SubjectMode,
)
from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.domain import ActorContext

logger = logging.getLogger(__name__)


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


class ConnectionRecord(BaseModel):
    """Connection record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    provider_version_id: UUID
    subject_mode: str
    status: str
    principal_id: UUID | None
    version: int
    fingerprint: str


class ConnectionStatusRecord(BaseModel):
    """Connection status projection (fingerprint + credential status)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: UUID
    status: str
    fingerprint: str
    credential_status: str


class CreateConnectionRequest(BaseModel):
    """POST body for creating a connection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_version_id: UUID
    subject_mode: str = "workspace_service"
    principal_id: UUID | None = None


class ConnectionActionRequest(BaseModel):
    """POST body for connection lifecycle actions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str


class _ConnectionStore:
    """In-memory connection store (simulates DB RLS)."""

    def __init__(self) -> None:
        self._connections: dict[UUID, Connection] = {}

    def store(self, connection: Connection) -> None:
        self._connections[connection.id] = connection

    def get(self, connection_id: UUID) -> Connection | None:
        return self._connections.get(connection_id)

    def list_by_tenant(
        self, organization_id: UUID, workspace_id: UUID
    ) -> list[Connection]:
        return [
            c
            for c in self._connections.values()
            if c.organization_id == organization_id
            and c.workspace_id == workspace_id
        ]

    def update(self, connection_id: UUID, **kwargs: Any) -> Connection | None:
        conn = self._connections.get(connection_id)
        if conn is None:
            return None
        updated = conn.model_copy(update=kwargs)
        self._connections[connection_id] = updated
        return updated


_store = _ConnectionStore()


def create_connections_router(
    *,
    actor_dependency: Callable[[], ActorContext],
) -> APIRouter:
    """Create the connections API router."""
    router = APIRouter(prefix="/api/v1/connections", tags=["connections"])

    @router.get("", response_model=list[ConnectionRecord])
    async def list_connections(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ConnectionRecord]:
        ctx = _TenantContext(actor)
        connections = _store.list_by_tenant(ctx.organization_id, ctx.workspace_id)
        return [
            ConnectionRecord(
                id=c.id,
                organization_id=c.organization_id,
                workspace_id=c.workspace_id,
                provider_version_id=c.provider_version_id,
                subject_mode=c.subject_mode.value,
                status=c.status.value,
                principal_id=c.principal_id,
                version=c.version,
                fingerprint=c.compute_fingerprint(),
            )
            for c in connections
        ]

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=ConnectionRecord,
    )
    async def create_connection(
        request: CreateConnectionRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConnectionRecord:
        ctx = _TenantContext(actor)
        try:
            subject_mode = SubjectMode(request.subject_mode)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid subject_mode: {request.subject_mode}",
            ) from exc
        now = datetime.now(UTC)
        connection = Connection(
            id=new_id(),
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            provider_version_id=request.provider_version_id,
            subject_mode=subject_mode,
            status=ConnectionStatus.ACTIVE,
            principal_id=request.principal_id,
            created_at=now,
            updated_at=now,
        )
        _store.store(connection)
        return ConnectionRecord(
            id=connection.id,
            organization_id=connection.organization_id,
            workspace_id=connection.workspace_id,
            provider_version_id=connection.provider_version_id,
            subject_mode=connection.subject_mode.value,
            status=connection.status.value,
            principal_id=connection.principal_id,
            version=connection.version,
            fingerprint=connection.compute_fingerprint(),
        )

    @router.get("/{connection_id}", response_model=ConnectionRecord)
    async def get_connection(
        connection_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConnectionRecord:
        ctx = _TenantContext(actor)
        connection = _store.get(connection_id)
        if connection is None or connection.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="connection not found",
            )
        return ConnectionRecord(
            id=connection.id,
            organization_id=connection.organization_id,
            workspace_id=connection.workspace_id,
            provider_version_id=connection.provider_version_id,
            subject_mode=connection.subject_mode.value,
            status=connection.status.value,
            principal_id=connection.principal_id,
            version=connection.version,
            fingerprint=connection.compute_fingerprint(),
        )

    @router.get("/{connection_id}/status", response_model=ConnectionStatusRecord)
    async def get_connection_status(
        connection_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConnectionStatusRecord:
        ctx = _TenantContext(actor)
        connection = _store.get(connection_id)
        if connection is None or connection.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="connection not found",
            )
        # credential_status is opaque — in production it would query SecretBackend
        credential_status = "active" if connection.status == ConnectionStatus.ACTIVE else connection.status.value
        return ConnectionStatusRecord(
            connection_id=connection.id,
            status=connection.status.value,
            fingerprint=connection.compute_fingerprint(),
            credential_status=credential_status,
        )

    @router.post(
        "/{connection_id}/actions",
        response_model=ConnectionRecord,
    )
    async def connection_action(
        connection_id: UUID,
        request: ConnectionActionRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConnectionRecord:
        ctx = _TenantContext(actor)
        connection = _store.get(connection_id)
        if connection is None or connection.organization_id != ctx.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="connection not found",
            )
        action = request.action
        transition_map = {
            "suspend": ConnectionStatus.SUSPENDED,
            "revoke": ConnectionStatus.REVOKED,
        }
        if action not in transition_map:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown action: {action}",
            )
        if connection.status == ConnectionStatus.REVOKED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot act on revoked connection",
            )
        target_status = transition_map[action]
        now = datetime.now(UTC)
        updated = _store.update(
            connection_id,
            status=target_status,
            updated_at=now,
            version=connection.version + 1,
        )
        assert updated is not None
        return ConnectionRecord(
            id=updated.id,
            organization_id=updated.organization_id,
            workspace_id=updated.workspace_id,
            provider_version_id=updated.provider_version_id,
            subject_mode=updated.subject_mode.value,
            status=updated.status.value,
            principal_id=updated.principal_id,
            version=updated.version,
            fingerprint=updated.compute_fingerprint(),
        )

    return router
