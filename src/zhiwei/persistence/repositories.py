"""Tenant-explicit S0 repositories."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.persistence.models import IdempotencyRecord, Organization, Workspace
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
)


class IdempotencyConflict(RuntimeError):
    """Raised when an idempotency key is reused for a different request digest."""


class OrganizationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    status: str


class WorkspaceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    name: str


class IdempotencyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    created: bool
    response: dict[str, Any]


class IdempotencyLookup(BaseModel):
    """只读幂等查询结果（既有资源路径专用，不写入任何记录）。"""

    model_config = ConfigDict(frozen=True)

    request_digest: str
    response: dict[str, Any]


class TenantRepository:
    """Repository that checks explicit scope before relying on RLS defense-in-depth."""

    def __init__(self, session: AsyncSession, context: TenantContext | None) -> None:
        self._session = session
        self._context = context

    async def create_organization(self, organization_id: UUID, *, status: str) -> OrganizationRecord:
        context = self._require_context()
        self._require_organization(organization_id, context)
        row = Organization(
            id=organization_id,
            status=status,
            retention_policy={},
            schema_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return OrganizationRecord(id=row.id, status=row.status)

    async def get_organization(self, organization_id: UUID) -> OrganizationRecord | None:
        context = self._require_context()
        self._require_organization(organization_id, context)
        row = await self._session.get(Organization, organization_id)
        return None if row is None else OrganizationRecord(id=row.id, status=row.status)

    async def create_workspace(self, workspace_id: UUID, *, name: str) -> WorkspaceRecord:
        context = self._require_context()
        if context.workspace_id is not None and context.workspace_id != workspace_id:
            raise TenantScopeError("workspace target does not match tenant context")
        row = Workspace(
            id=workspace_id,
            organization_id=context.organization_id,
            name=name,
            classification_ceiling="PUBLIC",
            budget_policy={},
            schema_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return WorkspaceRecord(id=row.id, organization_id=row.organization_id, name=row.name)

    async def claim_idempotency(
        self,
        *,
        scope: str,
        key: str,
        request_digest: str,
        response: dict[str, Any],
    ) -> IdempotencyResult:
        context = self._require_context()
        statement = (
            insert(IdempotencyRecord)
            .values(
                id=uuid4(),
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                scope=scope,
                idempotency_key=key,
                request_digest=request_digest,
                response=response,
                status="completed",
                schema_version=1,
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_tenant_scope_key")
            .returning(IdempotencyRecord.response)
        )
        inserted_response = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted_response is not None:
            return IdempotencyResult(created=True, response=inserted_response)

        existing = (
            await self._session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.organization_id == context.organization_id,
                    IdempotencyRecord.workspace_id == context.workspace_id,
                    IdempotencyRecord.scope == scope,
                    IdempotencyRecord.idempotency_key == key,
                )
            )
        ).scalar_one()
        if existing.request_digest != request_digest:
            raise IdempotencyConflict("idempotency key was already used for another request")
        return IdempotencyResult(created=False, response=existing.response)

    async def lookup_idempotency(self, *, scope: str, key: str) -> IdempotencyLookup | None:
        """只读幂等查询：不写入任何记录（既有资源路径专用）。"""
        context = self._require_context()
        existing = (
            await self._session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.organization_id == context.organization_id,
                    IdempotencyRecord.workspace_id == context.workspace_id,
                    IdempotencyRecord.scope == scope,
                    IdempotencyRecord.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return None
        return IdempotencyLookup(
            request_digest=existing.request_digest, response=existing.response
        )

    def _require_context(self) -> TenantContext:
        if self._context is None:
            raise TenantContextRequired("organization context is required")
        return self._context

    @staticmethod
    def _require_organization(organization_id: UUID, context: TenantContext) -> None:
        if organization_id != context.organization_id:
            raise TenantScopeError("organization target does not match tenant context")
