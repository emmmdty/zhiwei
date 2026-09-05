"""S9-T6 GREEN 实现级验证：Cost Ledger 的 canonical 持久化与租户隔离（真实 PG）。

specs/s9 §6 + plan Task 6：reserve/reconcile 经 CanonicalUnitOfWork 落 cost.reserved /
cost.reconciled canonical event（幂等键），同时写 cost_reservations / cost_reconciliations
行（0014 迁移，FORCE RLS）——账本跨进程重启可恢复：重启后 double reconcile 仍被拒绝。

事实源：ADR-002（token 支出是 ROI 指标不是门禁）、tests/unit/telemetry/
test_cost_ledger_frozen.py（域语义）、tests/security/tenancy/test_idor.py（fixture 模式）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from zhiwei.persistence.costs import CostLedgerRepository
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import (
    AuditEvent,
    CanonicalEvent,
    CostReconciliationRow,
    CostReservationRow,
    OutboxMessage,
)
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.telemetry.costs import (
    COST_RESERVED_EVENT_TYPE,
    CostLedgerError,
    PersistentCostLedger,
    ReserveRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_SQLALCHEMY_URL = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)


@dataclass(frozen=True)
class _Tenant:
    organization_id: UUID
    workspace_id: UUID

    @property
    def context(self) -> TenantContext:
        return TenantContext(
            organization_id=self.organization_id, workspace_id=self.workspace_id
        )


async def _insert_tenant_rows(organization_id: UUID, workspace_id: UUID) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) "
            "VALUES ($1, 'active', 1) ON CONFLICT DO NOTHING",
            organization_id,
        )
        await connection.execute(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) "
            "VALUES ($1, $2, 'cost-ledger-test', 1) ON CONFLICT DO NOTHING",
            workspace_id,
            organization_id,
        )
    finally:
        await connection.close()


async def _seed_run(tenant: _Tenant) -> UUID:
    run_id = uuid4()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO runs (id, organization_id, workspace_id, status, schema_version) "
            "VALUES ($1, $2, $3, 'running', 1)",
            run_id,
            tenant.organization_id,
            tenant.workspace_id,
        )
    finally:
        await connection.close()
    return run_id


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    url = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


# pytest-asyncio：async fixture 需显式装饰器（纯 @pytest.fixture 不处理协程）。
@pytest_asyncio.fixture
async def tenant() -> _Tenant:
    instance = _Tenant(organization_id=uuid4(), workspace_id=uuid4())
    await _insert_tenant_rows(instance.organization_id, instance.workspace_id)
    return instance


class TestReservePersistence:
    @pytest.mark.asyncio
    async def test_reserve_persists_row_event_audit_and_outbox(self, tenant: _Tenant) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            run_id = await _seed_run(tenant)
            ledger = PersistentCostLedger(sessions, tenant.context)
            reservation = await ledger.reserve(
                ReserveRequest(
                    run_id=run_id,
                    amount_usd=Decimal("0.0200"),
                    price_source="provider-list-2026-09",
                    price_confidence="exact",
                )
            )
            assert reservation.reservation_id

            async with tenant_session(sessions, tenant.context) as session:
                repository = CostLedgerRepository(session, tenant.context)
                rows = await repository.list_reservations()
                assert [str(row.id) for row in rows] == [reservation.reservation_id]
                assert rows[0].amount_usd == Decimal("0.0200")
                assert rows[0].run_id == run_id

                event = await session.scalar(
                    select(CanonicalEvent).where(
                        CanonicalEvent.event_type == COST_RESERVED_EVENT_TYPE,
                        CanonicalEvent.run_id == run_id,
                    )
                )
                assert event is not None
                assert event.payload["reservation_id"] == reservation.reservation_id
                assert event.payload["price_confidence"] == "exact"

                assert await session.scalar(
                    select(OutboxMessage.id).where(OutboxMessage.event_key == str(event.id))
                )
                assert await session.scalar(
                    select(AuditEvent.id).where(AuditEvent.resource_id == run_id)
                )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_reserve_rejects_invalid_price_metadata(self, tenant: _Tenant) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            run_id = await _seed_run(tenant)
            ledger = PersistentCostLedger(sessions, tenant.context)
            with pytest.raises(CostLedgerError):
                await ledger.reserve(
                    ReserveRequest(
                        run_id=run_id,
                        amount_usd=Decimal("0.02"),
                        price_source="",
                        price_confidence="exact",
                    )
                )
            async with tenant_session(sessions, tenant.context) as session:
                assert await session.scalar(select(CostReservationRow.id)) is None
        finally:
            await engine.dispose()


class TestReconcilePersistence:
    @pytest.mark.asyncio
    async def test_reconcile_persists_components_and_refuses_double(
        self, tenant: _Tenant
    ) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            run_id = await _seed_run(tenant)
            ledger = PersistentCostLedger(sessions, tenant.context)
            reservation = await ledger.reserve(
                ReserveRequest(
                    run_id=run_id,
                    amount_usd=Decimal("0.0200"),
                    price_source="provider-list-2026-09",
                    price_confidence="exact",
                )
            )
            reconciliation = await ledger.reconcile(
                reservation_id=reservation.reservation_id,
                actual_usd=Decimal("0.0300"),
                retry_cost_usd=Decimal("0.0050"),
                tool_external_cost_usd=Decimal("0.0020"),
            )
            assert reconciliation.variance_usd == Decimal("0.0100")

            async with tenant_session(sessions, tenant.context) as session:
                rows = list(
                    (
                        await session.scalars(
                            select(CostReconciliationRow).where(
                                CostReconciliationRow.reservation_id
                                == UUID(reservation.reservation_id)
                            )
                        )
                    ).all()
                )
                assert len(rows) == 1
                assert rows[0].variance_usd == Decimal("0.010000")
                assert rows[0].retry_cost_usd == Decimal("0.005000")
                assert rows[0].tool_external_cost_usd == Decimal("0.002000")
                assert rows[0].child_run_cost_usd == Decimal("0.000000")

            # 同进程 double reconcile：域状态拒绝
            with pytest.raises(CostLedgerError):
                await ledger.reconcile(
                    reservation_id=reservation.reservation_id,
                    actual_usd=Decimal("0.0300"),
                )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_reconcile_survives_restart(self, tenant: _Tenant) -> None:
        # 重启 = 全新 PersistentCostLedger 从 DB 行重建域状态：
        # reconcile 过的预订仍然拒绝再次 reconcile（幂等由数据面保证）。
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            run_id = await _seed_run(tenant)
            first = PersistentCostLedger(sessions, tenant.context)
            reservation = await first.reserve(
                ReserveRequest(
                    run_id=run_id,
                    amount_usd=Decimal("0.0200"),
                    price_source="provider-list-2026-09",
                    price_confidence="exact",
                )
            )
            await first.reconcile(
                reservation_id=reservation.reservation_id,
                actual_usd=Decimal("0.0300"),
            )

            restarted = PersistentCostLedger(sessions, tenant.context)
            with pytest.raises(CostLedgerError):
                await restarted.reconcile(
                    reservation_id=reservation.reservation_id,
                    actual_usd=Decimal("0.0300"),
                )
        finally:
            await engine.dispose()


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_other_tenant_cannot_read_reservations(self, tenant: _Tenant) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            run_id = await _seed_run(tenant)
            ledger = PersistentCostLedger(sessions, tenant.context)
            reservation = await ledger.reserve(
                ReserveRequest(
                    run_id=run_id,
                    amount_usd=Decimal("0.0200"),
                    price_source="provider-list-2026-09",
                    price_confidence="exact",
                )
            )

            other = _Tenant(organization_id=uuid4(), workspace_id=uuid4())
            await _insert_tenant_rows(other.organization_id, other.workspace_id)
            await _seed_run(other)
            async with tenant_session(sessions, other.context) as session:
                repository = CostLedgerRepository(session, other.context)
                assert await repository.list_reservations() == []
                assert await repository.get_reservation(reservation.reservation_id) is None
                # RLS 纵深：app GUC 下裸 SQL 也看不到他租户的行
                assert await session.scalar(select(CostReservationRow.id)) is None
        finally:
            await engine.dispose()
