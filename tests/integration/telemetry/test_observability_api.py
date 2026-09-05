"""S9-T6 RED：Observability API 契约（B 档实现级测试，真实 PG）。

GET /api/v1/observability/costs：actor 租户内的 reservations + reconciliations
（authorize_read 读路径 PEP 前置，ADR-012 决策 4）；无 workspace / policy deny 拒绝。
GET /api/v1/observability/failures：failure taxonomy 的静态 machine code 清单。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fixtures.policy_fake import FakePolicyEnforcer
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from zhiwei.api.observability import create_observability_router
from zhiwei.telemetry.costs import PersistentCostLedger, ReserveRequest
from zhiwei.telemetry.failures import FailureCode

from tests.integration.telemetry.test_costs_persistence import (
    _insert_tenant_rows,
    _seed_run,
)
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.tenant import TenantContext

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
APP_SQLALCHEMY_URL = (
    "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    url = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


def _actor(organization_id: UUID | None, workspace_id: UUID | None) -> ActorContext:
    return ActorContext(
        principal_id=uuid4(), organization_id=organization_id, workspace_id=workspace_id
    )


def _router_app(
    actor: ActorContext, policy_enforcer=None
) -> tuple[FastAPI, async_sessionmaker[AsyncSession], AsyncEngine]:
    sessions = create_session_factory(create_database_engine(APP_SQLALCHEMY_URL))
    app = FastAPI()
    app.include_router(
        create_observability_router(
            actor_dependency=lambda: actor,
            sessions=sessions,
            policy_enforcer=policy_enforcer or FakePolicyEnforcer(),
        )
    )
    engine = sessions.kw["bind"]
    return app, sessions, engine


class TestCostSummaryEndpoint:
    @pytest.mark.asyncio
    async def test_lists_own_tenant_reservations_and_reconciliations(self) -> None:
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
        app, sessions, engine = _router_app(_actor(organization_id, workspace_id))
        try:
            run_id = await _seed_run(context)
            ledger = PersistentCostLedger(sessions, context)
            reservation = await ledger.reserve(
                ReserveRequest(
                    run_id=run_id,
                    amount_usd=Decimal("0.0200"),
                    price_source="provider-list-2026-09",
                    price_confidence="exact",
                )
            )
            await ledger.reconcile(
                reservation_id=reservation.reservation_id,
                actual_usd=Decimal("0.0300"),
            )

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/observability/costs")
                assert response.status_code == 200
                body = response.json()
                assert [row["reservation_id"] for row in body["reservations"]] == [
                    reservation.reservation_id
                ]
                assert body["reservations"][0]["price_source"] == "provider-list-2026-09"
                assert len(body["reconciliations"]) == 1
                assert body["reconciliations"][0]["variance_usd"] == "0.010000"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_actor_without_workspace_is_refused(self) -> None:
        app, _sessions, engine = _router_app(_actor(uuid4(), None))
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/observability/costs")
                assert response.status_code == 403
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_policy_deny_is_fail_closed_403(self) -> None:
        organization_id, workspace_id = uuid4(), uuid4()
        await _insert_tenant_rows(organization_id, workspace_id)
        app, _sessions, engine = _router_app(
            _actor(organization_id, workspace_id), policy_enforcer=FakePolicyEnforcer(allow=False)
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/observability/costs")
                assert response.status_code == 403
                assert response.json() == {"detail": "policy denied"}
        finally:
            await engine.dispose()


class TestFailureTaxonomyEndpoint:
    @pytest.mark.asyncio
    async def test_lists_machine_codes(self) -> None:
        app, _sessions, engine = _router_app(_actor(uuid4(), uuid4()))
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/observability/failures")
                assert response.status_code == 200
                codes = {entry["code"] for entry in response.json()["codes"]}
                assert {
                    FailureCode.MODEL_TIMEOUT.value,
                    FailureCode.POLICY_DENY.value,
                    FailureCode.UNKNOWN.value,
                } <= codes
        finally:
            await engine.dispose()
