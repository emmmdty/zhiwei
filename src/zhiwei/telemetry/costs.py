"""S9-T6：Cost Ledger——reserve/reconcile 域语义 + canonical 持久化（specs/s9 §6）。

记账纪律（ADR-002）：token 支出是 ROI 指标，不是门禁——reserve 不设支出上限、
reconcile 的 variance（超额/节省）如实记录而不抛错；组织可选的 spend guard 是
独立机制，不在本 ledger 内。price source/confidence 必填（无出处的金额不可审计）；
retry/child-run/tool-external 成本分项归集，不并入主消耗口径。

持久化纪律：与 eval.run.sealed（evals/runs.py）同构——reserve/reconcile 经
CanonicalUnitOfWork 落 cost.reserved / cost.reconciled canonical event（幂等键），
同时写 0014 的 cost_reservations / cost_reconciliations 行，同一 tenant 事务提交。
每次操作先从 DB 行重建域状态（重启安全的单一事实源：进程内存不持有跨事务状态，
double reconcile 由数据面行 + 重建状态共同拒绝）。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.contracts.envelope import SchemaRegistry
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.costs import CostLedgerRepository
from zhiwei.persistence.events import EventCommand
from zhiwei.persistence.models import CostReconciliationRow, CostReservationRow
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired, tenant_session
from zhiwei.persistence.unit_of_work import CanonicalUnitOfWork


def _reservation_from_row(row: CostReservationRow) -> CostReservation:
    """行 → 域投影；amount 的 Decimal 精度由 NUMERIC(18,6) 数据面保证。"""
    return CostReservation(
        reservation_id=str(row.id),
        run_id=row.run_id,
        amount_usd=row.amount_usd,
        price_source=row.price_source,
        price_confidence=row.price_confidence,
        reserved_at=row.created_at,
    )


def _reconciliation_from_row(row: CostReconciliationRow) -> CostReconciliation:
    return CostReconciliation(
        reservation_id=str(row.reservation_id),
        reserved_usd=row.reserved_usd,
        actual_usd=row.actual_usd,
        variance_usd=row.variance_usd,
        retry_cost_usd=row.retry_cost_usd,
        child_run_cost_usd=row.child_run_cost_usd,
        tool_external_cost_usd=row.tool_external_cost_usd,
        reconciled_at=row.created_at,
    )


class CostLedgerError(RuntimeError):
    """Raised when a reserve/reconcile request violates ledger invariants."""


class PriceConfidence(StrEnum):
    """封闭的价格可信档：exact=报价单原价；estimated=估算（须可追溯到估算器）。"""

    EXACT = "exact"
    ESTIMATED = "estimated"


PRICE_CONFIDENCE_VALUES = tuple(item.value for item in PriceConfidence)


class ReserveRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    amount_usd: Decimal
    price_source: str
    price_confidence: str


class CostReservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: str
    run_id: UUID
    amount_usd: Decimal
    price_source: str
    price_confidence: str
    reserved_at: datetime


class CostReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: str
    reserved_usd: Decimal
    actual_usd: Decimal
    variance_usd: Decimal
    retry_cost_usd: Decimal
    child_run_cost_usd: Decimal
    tool_external_cost_usd: Decimal
    reconciled_at: datetime


_ZERO = Decimal("0.0000")
_RESERVED_ACTOR_REF = "system:costs"
_RESERVE_IDEMPOTENCY_PREFIX = "cost:reserve:"
_RECONCILE_IDEMPOTENCY_PREFIX = "cost:reconcile:"


class CostLedger:
    """reserve/reconcile 的纯域状态；持久化包装见 PersistentCostLedger。"""

    def __init__(self) -> None:
        self._reservations: dict[str, CostReservation] = {}
        self._reconciliations: dict[str, CostReconciliation] = {}

    @classmethod
    def restore(
        cls,
        *,
        reservations: Iterable[CostReservation],
        reconciliations: Iterable[CostReconciliation],
    ) -> CostLedger:
        """从持久化行重建域状态（重启路径）；同一预订只允许一条 reconcile。"""
        ledger = cls()
        for reservation in reservations:
            ledger._reservations[reservation.reservation_id] = reservation
        for reconciliation in reconciliations:
            if reconciliation.reservation_id not in ledger._reservations:
                raise CostLedgerError(
                    f"reconciliation {reconciliation.reservation_id} has no reservation"
                )
            ledger._reconciliations[reconciliation.reservation_id] = reconciliation
        return ledger

    def reserve(self, request: ReserveRequest) -> CostReservation:
        # 校验在 ledger 层而非 pydantic 字段约束：调用方可能原样转发上游载荷，
        # 拒绝语义必须是 CostLedgerError（与 reconcile 失败同类型），不能靠
        # 「构造请求对象时碰巧先抛了 ValidationError」。
        if not request.price_source.strip():
            raise CostLedgerError("price_source is required")
        if request.price_confidence not in PRICE_CONFIDENCE_VALUES:
            raise CostLedgerError(
                f"unknown price_confidence: {request.price_confidence!r}"
            )
        if request.amount_usd < 0:
            raise CostLedgerError("amount must not be negative")
        reservation = CostReservation(
            reservation_id=str(uuid4()),
            run_id=request.run_id,
            amount_usd=request.amount_usd,
            price_source=request.price_source.strip(),
            price_confidence=request.price_confidence,
            reserved_at=utc_now(),
        )
        self._reservations[reservation.reservation_id] = reservation
        return reservation

    def reconcile(
        self,
        *,
        reservation_id: str,
        actual_usd: Decimal,
        retry_cost_usd: Decimal | None = None,
        child_run_cost_usd: Decimal | None = None,
        tool_external_cost_usd: Decimal | None = None,
    ) -> CostReconciliation:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise CostLedgerError(f"unknown reservation: {reservation_id!r}")
        if reservation_id in self._reconciliations:
            raise CostLedgerError(f"reservation already reconciled: {reservation_id!r}")
        reconciliation = CostReconciliation(
            reservation_id=reservation_id,
            reserved_usd=reservation.amount_usd,
            actual_usd=actual_usd,
            # variance 如实记录（负值=节省）：ROI 指标不是门禁，超额不抛错。
            variance_usd=actual_usd - reservation.amount_usd,
            retry_cost_usd=retry_cost_usd if retry_cost_usd is not None else _ZERO,
            child_run_cost_usd=child_run_cost_usd
            if child_run_cost_usd is not None
            else _ZERO,
            tool_external_cost_usd=tool_external_cost_usd
            if tool_external_cost_usd is not None
            else _ZERO,
            reconciled_at=utc_now(),
        )
        self._reconciliations[reservation_id] = reconciliation
        return reconciliation


# --------------------------------------------------------------------- canonical events

COST_RESERVED_EVENT_TYPE = "cost.reserved"
COST_RECONCILED_EVENT_TYPE = "cost.reconciled"
COST_PAYLOAD_SCHEMA_VERSION = 1


class CostReservedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: str
    run_id: str
    amount_usd: str
    price_source: str
    price_confidence: str


class CostReconciledPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: str
    run_id: str
    reserved_usd: str
    actual_usd: str
    variance_usd: str
    retry_cost_usd: str
    child_run_cost_usd: str
    tool_external_cost_usd: str


def cost_schema_registry() -> SchemaRegistry:
    """cost.* 事件 payload 的封闭注册表（canonical 写入前的 fail-closed 校验）。"""
    registry = SchemaRegistry()
    registry.register(COST_RESERVED_EVENT_TYPE, COST_PAYLOAD_SCHEMA_VERSION, CostReservedPayload)
    registry.register(
        COST_RECONCILED_EVENT_TYPE, COST_PAYLOAD_SCHEMA_VERSION, CostReconciledPayload
    )
    return registry


def reserved_payload(reservation: CostReservation) -> dict[str, str]:
    # Decimal 以字符串进 payload：JSONB 数值化会引入浮点语义，金额必须逐字符可复算。
    return CostReservedPayload(
        reservation_id=reservation.reservation_id,
        run_id=str(reservation.run_id),
        amount_usd=str(reservation.amount_usd),
        price_source=reservation.price_source,
        price_confidence=reservation.price_confidence,
    ).model_dump()


def reconciled_payload(reconciliation: CostReconciliation, *, run_id: UUID) -> dict[str, str]:
    return CostReconciledPayload(
        reservation_id=reconciliation.reservation_id,
        run_id=str(run_id),
        reserved_usd=str(reconciliation.reserved_usd),
        actual_usd=str(reconciliation.actual_usd),
        variance_usd=str(reconciliation.variance_usd),
        retry_cost_usd=str(reconciliation.retry_cost_usd),
        child_run_cost_usd=str(reconciliation.child_run_cost_usd),
        tool_external_cost_usd=str(reconciliation.tool_external_cost_usd),
    ).model_dump()


# --------------------------------------------------------------------- persistence


class PersistentCostLedger:
    """CostLedger 的 canonical 持久化包装：每次操作一个 tenant 事务。

    状态恢复策略：每次 reserve/reconcile 先从 DB 行重建域状态再施用域操作——
    进程崩溃/重启后行为与常驻进程完全一致（double reconcile 仍被拒绝），
    不存在「内存态与 DB 真相分叉」的第二账本。
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        context: TenantContext,
    ) -> None:
        if context.workspace_id is None:
            raise TenantContextRequired("cost ledger requires workspace context")
        self._sessions = sessions
        self._context = context

    async def reserve(
        self, request: ReserveRequest, *, actor_ref: str = _RESERVED_ACTOR_REF
    ) -> CostReservation:
        async with tenant_session(self._sessions, self._context) as session:
            repository = CostLedgerRepository(session, self._context)
            ledger = await self._restore(repository)
            reservation = ledger.reserve(request)
            await repository.insert_reservation(
                reservation_id=reservation.reservation_id,
                run_id=reservation.run_id,
                amount_usd=reservation.amount_usd,
                price_source=reservation.price_source,
                price_confidence=reservation.price_confidence,
                actor_ref=actor_ref,
                schema_version=COST_PAYLOAD_SCHEMA_VERSION,
            )
            await self._append_event(
                session,
                run_id=request.run_id,
                event_type=COST_RESERVED_EVENT_TYPE,
                payload=reserved_payload(reservation),
                idempotency_key=f"{_RESERVE_IDEMPOTENCY_PREFIX}{reservation.reservation_id}",
                actor_ref=actor_ref,
            )
        return reservation

    async def reconcile(
        self,
        *,
        reservation_id: str,
        actual_usd: Decimal,
        retry_cost_usd: Decimal | None = None,
        child_run_cost_usd: Decimal | None = None,
        tool_external_cost_usd: Decimal | None = None,
        actor_ref: str = _RESERVED_ACTOR_REF,
    ) -> CostReconciliation:
        async with tenant_session(self._sessions, self._context) as session:
            repository = CostLedgerRepository(session, self._context)
            ledger = await self._restore(repository)
            reservation_row = await repository.get_reservation_row(reservation_id)
            if reservation_row is None:
                raise CostLedgerError(f"unknown reservation: {reservation_id!r}")
            reconciliation = ledger.reconcile(
                reservation_id=reservation_id,
                actual_usd=actual_usd,
                retry_cost_usd=retry_cost_usd,
                child_run_cost_usd=child_run_cost_usd,
                tool_external_cost_usd=tool_external_cost_usd,
            )
            await repository.insert_reconciliation(
                reservation_id=reconciliation.reservation_id,
                reserved_usd=reconciliation.reserved_usd,
                actual_usd=reconciliation.actual_usd,
                variance_usd=reconciliation.variance_usd,
                retry_cost_usd=reconciliation.retry_cost_usd,
                child_run_cost_usd=reconciliation.child_run_cost_usd,
                tool_external_cost_usd=reconciliation.tool_external_cost_usd,
                actor_ref=actor_ref,
                schema_version=COST_PAYLOAD_SCHEMA_VERSION,
            )
            await self._append_event(
                session,
                run_id=reservation_row.run_id,
                event_type=COST_RECONCILED_EVENT_TYPE,
                payload=reconciled_payload(reconciliation, run_id=reservation_row.run_id),
                idempotency_key=f"{_RECONCILE_IDEMPOTENCY_PREFIX}{reservation_id}",
                actor_ref=actor_ref,
            )
        return reconciliation

    async def _restore(self, repository: CostLedgerRepository) -> CostLedger:
        # 域状态每次操作从行重建：进程崩溃/重启与常驻进程行为一致，
        # 不存在「内存账本与 DB 真相分叉」的第二账本。
        return CostLedger.restore(
            reservations=[
                _reservation_from_row(row) for row in await repository.list_reservation_rows()
            ],
            reconciliations=[
                _reconciliation_from_row(row)
                for row in await repository.list_reconciliation_rows()
            ],
        )

    async def _append_event(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        event_type: str,
        payload: dict[str, str],
        idempotency_key: str,
        actor_ref: str,
    ) -> None:
        unit_of_work = CanonicalUnitOfWork(
            session, self._context, schema_registry=cost_schema_registry()
        )
        await unit_of_work.append_event(
            EventCommand(
                run_id=run_id,
                event_type=event_type,
                payload_schema_version=COST_PAYLOAD_SCHEMA_VERSION,
                payload=payload,
                actor_ref=actor_ref,
                idempotency_key=idempotency_key,
            )
        )
