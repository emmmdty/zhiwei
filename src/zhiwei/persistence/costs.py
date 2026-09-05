"""S9-T6：cost_reservations / cost_reconciliations 的租户显式仓储（0014，FORCE RLS）。

事务纪律：与 persistence.approvals / memory.repositories 同型——绑定调用方事务内的
session，不自行 commit/rollback；RLS 之外再带显式租户谓词（两段防线：RLS 被剥离时
谓词仍然挡住，见 tests/security/tenancy/test_idor.py 的契约）。

表只追加（无 UPDATE/DELETE 授权）：预订与对账都是一次性事实，纠正通过新的
对账 variance 如实记录，不原地改写——与迁移的数据面授权一致。
本仓储只面向 ORM 行，不 import telemetry 域模型（依赖方向：telemetry → persistence，
反向会造成环形导入——域到行的映射归 telemetry.costs）。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.persistence.models import CostReconciliationRow, CostReservationRow
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired


class CostLedgerRepository:
    """0014 cost 表的 append-only 仓储（workspace 级租户作用域）。"""

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        if context.workspace_id is None:
            raise TenantContextRequired("cost ledger requires workspace context")
        self._session = session
        self._organization_id = context.organization_id
        self._workspace_id = context.workspace_id

    async def insert_reservation(
        self,
        *,
        reservation_id: str,
        run_id: UUID,
        amount_usd: Decimal,
        price_source: str,
        price_confidence: str,
        actor_ref: str,
        schema_version: int,
    ) -> CostReservationRow:
        row = CostReservationRow(
            id=UUID(reservation_id),
            organization_id=self._organization_id,
            workspace_id=self._workspace_id,
            run_id=run_id,
            amount_usd=amount_usd,
            price_source=price_source,
            price_confidence=price_confidence,
            actor_ref=actor_ref,
            schema_version=schema_version,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def insert_reconciliation(
        self,
        *,
        reservation_id: str,
        reserved_usd: Decimal,
        actual_usd: Decimal,
        variance_usd: Decimal,
        retry_cost_usd: Decimal,
        child_run_cost_usd: Decimal,
        tool_external_cost_usd: Decimal,
        actor_ref: str,
        schema_version: int,
    ) -> CostReconciliationRow:
        row = CostReconciliationRow(
            id=uuid4(),
            organization_id=self._organization_id,
            workspace_id=self._workspace_id,
            reservation_id=UUID(reservation_id),
            reserved_usd=reserved_usd,
            actual_usd=actual_usd,
            variance_usd=variance_usd,
            retry_cost_usd=retry_cost_usd,
            child_run_cost_usd=child_run_cost_usd,
            tool_external_cost_usd=tool_external_cost_usd,
            actor_ref=actor_ref,
            schema_version=schema_version,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_reservation_rows(self) -> list[CostReservationRow]:
        return list(
            (
                await self._session.scalars(
                    select(CostReservationRow)
                    .where(
                        CostReservationRow.organization_id == self._organization_id,
                        CostReservationRow.workspace_id == self._workspace_id,
                    )
                    .order_by(CostReservationRow.created_at, CostReservationRow.id)
                )
            ).all()
        )

    async def list_reconciliation_rows(self) -> list[CostReconciliationRow]:
        return list(
            (
                await self._session.scalars(
                    select(CostReconciliationRow)
                    .where(
                        CostReconciliationRow.organization_id == self._organization_id,
                        CostReconciliationRow.workspace_id == self._workspace_id,
                    )
                    .order_by(CostReconciliationRow.created_at, CostReconciliationRow.id)
                )
            ).all()
        )

    async def get_reservation_row(self, reservation_id: str) -> CostReservationRow | None:
        """按 id 取预订；显式租户谓词——跨租户 id 与「不存在」同语义 None（防枚举）。"""
        return await self._session.scalar(
            select(CostReservationRow).where(
                CostReservationRow.id == UUID(reservation_id),
                CostReservationRow.organization_id == self._organization_id,
                CostReservationRow.workspace_id == self._workspace_id,
            )
        )

    # 行级 API 的语义别名：调用方在 tenant session 内拿到的就是「本租户可见的行」，
    # 与 domain 投影的字段契约一致（amount_usd/run_id 等），无需二次映射。
    list_reservations = list_reservation_rows
    list_reconciliations = list_reconciliation_rows
    get_reservation = get_reservation_row
