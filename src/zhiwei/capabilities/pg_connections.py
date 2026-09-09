"""S4 Connection 的 PG 持久化仓储（F-R6-03，P2b）。

事务纪律与 memory/repositories.PgMemoryRepository 同型：绑定调用方事务内的
session（不自行 commit/rollback），租户作用域显式谓词 + RLS 纵深防御。状态
转移（suspend/revoke）用 CAS（WHERE status = 预期 + version 递增），失配即
fail closed 抛 ConnectionTransitionConflict；无内容不可变列（连接是生命周期
行），不经域层队列（Connection 无独立状态机域实现——转移语义在本仓储与
API 层之间只有这一处）。

provider_version_id 是 capability 目录引用：目录持久层未建，存在性校验由
API 层经注入查询完成（见 api/connections.py），本仓储不校验。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.capabilities.connections import Connection, ConnectionStatus, SubjectMode
from zhiwei.contracts.time import ensure_utc
from zhiwei.persistence.models import ConnectionRow
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
)


class ConnectionTransitionConflict(RuntimeError):
    """Raised when a lifecycle transition loses its CAS (concurrent modification)."""


def connection_to_row(connection: Connection) -> ConnectionRow:
    return ConnectionRow(
        id=connection.id if connection.id != UUID(int=0) else uuid4(),
        organization_id=connection.organization_id,
        workspace_id=connection.workspace_id,
        provider_version_id=connection.provider_version_id,
        subject_mode=connection.subject_mode.value,
        status=connection.status.value,
        principal_id=connection.principal_id,
        metadata_=connection.metadata,
        version=connection.version,
        schema_version=connection.schema_version,
        created_at=ensure_utc(connection.created_at),
        updated_at=ensure_utc(connection.updated_at),
    )


def row_to_connection(row: ConnectionRow) -> Connection:
    return Connection(
        id=row.id,
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        provider_version_id=row.provider_version_id,
        subject_mode=SubjectMode(row.subject_mode),
        status=ConnectionStatus(row.status),
        principal_id=row.principal_id,
        metadata=row.metadata_,
        version=row.version,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PgConnectionRepository:
    """connections 的租户显式仓储。"""

    def __init__(self, session: AsyncSession, context: TenantContext | None) -> None:
        self._session = session
        self._context = context

    async def create(self, connection: Connection) -> Connection:
        """插入连接行；id 缺省（UUID(int=0)）时由仓储分配并回填返回值。"""
        self._require_scope(connection)
        row = connection_to_row(connection)
        self._session.add(row)
        await self._session.flush()
        return row_to_connection(row)

    async def get(self, connection_id: UUID) -> Connection | None:
        context = self._require_context()
        row = await self._session.scalar(
            select(ConnectionRow).where(
                ConnectionRow.organization_id == context.organization_id,
                ConnectionRow.workspace_id == context.workspace_id,
                ConnectionRow.id == connection_id,
            )
        )
        return None if row is None else row_to_connection(row)

    async def list_tenant(self) -> list[Connection]:
        context = self._require_context()
        statement = (
            select(ConnectionRow)
            .where(
                ConnectionRow.organization_id == context.organization_id,
                ConnectionRow.workspace_id == context.workspace_id,
            )
            .order_by(ConnectionRow.created_at, ConnectionRow.id)
        )
        rows = list((await self._session.scalars(statement)).all())
        return [row_to_connection(row) for row in rows]

    async def transition_status(
        self,
        connection_id: UUID,
        *,
        expected_status: ConnectionStatus,
        target_status: ConnectionStatus,
        now: datetime,
    ) -> Connection:
        """suspend/revoke：CAS（WHERE status = 预期）+ version 递增。

        rowcount 失配 = 连接不存在/已被并发转移——调用方以 404/409 语义区分
        （get 先行），本层统一 fail closed。
        """
        context = self._require_context()
        statement = (
            update(ConnectionRow)
            .where(
                ConnectionRow.organization_id == context.organization_id,
                ConnectionRow.workspace_id == context.workspace_id,
                ConnectionRow.id == connection_id,
                ConnectionRow.status == expected_status.value,
            )
            .values(
                status=target_status.value,
                version=ConnectionRow.version + 1,
                updated_at=ensure_utc(now),
            )
            .returning(
                ConnectionRow.version,
                ConnectionRow.updated_at,
            )
        )
        result = (await self._session.execute(statement)).first()
        if result is None:
            raise ConnectionTransitionConflict(
                f"connection {connection_id} transition lost a race "
                f"(expected status {expected_status.value})"
            )
        row = await self._session.scalar(
            select(ConnectionRow).where(
                ConnectionRow.organization_id == context.organization_id,
                ConnectionRow.workspace_id == context.workspace_id,
                ConnectionRow.id == connection_id,
            )
        )
        assert row is not None  # 同事务内 CAS 刚成功
        return row_to_connection(row)

    def _require_context(self) -> TenantContext:
        if self._context is None:
            raise TenantContextRequired("organization context is required")
        if self._context.workspace_id is None:
            raise TenantContextRequired("connections require workspace context")
        return self._context

    def _require_scope(self, connection: Connection) -> None:
        context = self._require_context()
        if connection.organization_id != context.organization_id:
            raise TenantScopeError("connection does not match tenant context")
        if connection.workspace_id != context.workspace_id:
            raise TenantScopeError("connection does not match workspace context")
