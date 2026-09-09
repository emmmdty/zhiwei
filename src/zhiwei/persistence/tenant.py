"""Transaction-local PostgreSQL tenant context."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class TenantContextRequired(RuntimeError):
    """Raised when a tenant repository is used without organization context."""


class TenantScopeError(PermissionError):
    """Raised when a repository target falls outside its explicit tenant scope."""


class TenantContext(BaseModel):
    """Explicit organization and optional workspace scope for one transaction."""

    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    workspace_id: UUID | None = None


async def set_tenant_context(session: AsyncSession, context: TenantContext) -> None:
    """Install tenant GUCs for only the current database transaction."""
    if not session.in_transaction():
        raise RuntimeError("tenant context requires an active transaction")
    await session.execute(
        text("SELECT set_config('zhiwei.organization_id', :organization_id, true)"),
        {"organization_id": str(context.organization_id)},
    )
    await session.execute(
        text("SELECT set_config('zhiwei.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(context.workspace_id) if context.workspace_id else ""},
    )


@asynccontextmanager
async def tenant_session(
    sessions: async_sessionmaker[AsyncSession], context: TenantContext
) -> AsyncIterator[AsyncSession]:
    """Yield a session inside one atomic, tenant-scoped transaction."""
    async with sessions() as session, session.begin():
        await set_tenant_context(session, context)
        yield session
