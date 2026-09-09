"""S9 冻结契约：Cost Ledger reserve/reconcile 语义（A 档，S9-T6）。

Ledger 只记账不做门禁（token 支出是 ROI 指标，ADR-002）：超额不抛错，variance 如实记录；
price source/confidence 必填；retry/child/tool 外部成本分项归集；重复 reconcile 拒绝。
spend guard 是组织可选的独立机制，不在本 ledger 内。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from zhiwei.telemetry.costs import (
    CostLedger,
    CostLedgerError,
    CostReconciliation,
    ReserveRequest,
)


class TestReserve:
    def test_reserve_records_price_source_and_confidence(self) -> None:
        ledger = CostLedger()
        reservation = ledger.reserve(
            ReserveRequest(
                run_id=uuid4(),
                amount_usd=Decimal("0.0200"),
                price_source="provider-list-2026-09",
                price_confidence="exact",
            )
        )
        assert reservation.reservation_id
        assert reservation.price_source == "provider-list-2026-09"
        assert reservation.price_confidence == "exact"

    def test_missing_price_source_refused(self) -> None:
        ledger = CostLedger()
        with pytest.raises(CostLedgerError):
            ledger.reserve(
                ReserveRequest(
                    run_id=uuid4(),
                    amount_usd=Decimal("0.02"),
                    price_source="",
                    price_confidence="exact",
                )
            )

    def test_unknown_confidence_refused(self) -> None:
        ledger = CostLedger()
        with pytest.raises(CostLedgerError):
            ledger.reserve(
                ReserveRequest(
                    run_id=uuid4(),
                    amount_usd=Decimal("0.02"),
                    price_source="list",
                    price_confidence="vibes",
                )
            )

    def test_negative_amount_refused(self) -> None:
        ledger = CostLedger()
        with pytest.raises(CostLedgerError):
            ledger.reserve(
                ReserveRequest(
                    run_id=uuid4(),
                    amount_usd=Decimal("-0.01"),
                    price_source="list",
                    price_confidence="exact",
                )
            )


class TestReconcile:
    def _reserve(self, ledger: CostLedger, amount: str = "0.02") -> str:
        return ledger.reserve(
            ReserveRequest(
                run_id=uuid4(),
                amount_usd=Decimal(amount),
                price_source="list",
                price_confidence="exact",
            )
        ).reservation_id

    def test_reconcile_without_reserve_refused(self) -> None:
        ledger = CostLedger()
        with pytest.raises(CostLedgerError):
            ledger.reconcile(reservation_id=str(uuid4()), actual_usd=Decimal("0.01"))

    def test_reconcile_records_components_and_variance(self) -> None:
        ledger = CostLedger()
        reservation_id = self._reserve(ledger)
        reconciliation: CostReconciliation = ledger.reconcile(
            reservation_id=reservation_id,
            actual_usd=Decimal("0.0300"),
            retry_cost_usd=Decimal("0.0050"),
            child_run_cost_usd=Decimal("0.0030"),
            tool_external_cost_usd=Decimal("0.0020"),
        )
        # 分项成本如实归集（retry/child/tool external），不并入主消耗口径。
        assert reconciliation.retry_cost_usd == Decimal("0.0050")
        assert reconciliation.child_run_cost_usd == Decimal("0.0030")
        assert reconciliation.tool_external_cost_usd == Decimal("0.0020")
        # 超额不是错误：variance 如实记录（ROI 指标不是门禁）。
        assert reconciliation.variance_usd == Decimal("0.0100")

    def test_double_reconcile_refused(self) -> None:
        ledger = CostLedger()
        reservation_id = self._reserve(ledger)
        ledger.reconcile(reservation_id=reservation_id, actual_usd=Decimal("0.02"))
        with pytest.raises(CostLedgerError):
            ledger.reconcile(reservation_id=reservation_id, actual_usd=Decimal("0.02"))

    def test_ledger_never_gates_on_amount(self) -> None:
        # ledger 不设支出上限；spend guard 属于独立的可选机制。
        ledger = CostLedger()
        reservation_id = self._reserve(ledger, amount="0.0001")
        reconciliation = ledger.reconcile(
            reservation_id=reservation_id, actual_usd=Decimal("1000.00")
        )
        assert reconciliation.variance_usd == Decimal("999.9999")
