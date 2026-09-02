"""S2-T5 RED: ActionReceipt and effect tracking tests."""

from __future__ import annotations

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.actions import (
    ActionReceiptManager,
    EffectState,
    ToolIntent,
)


def _make_manager() -> ActionReceiptManager:
    return ActionReceiptManager()


def _make_intent(tool_name: str = "create_ticket") -> ToolIntent:
    return ToolIntent(
        tool_name=tool_name,
        parameters={"title": "test ticket"},
        run_id=new_id(),
        task_id="t1",
        approval_id=new_id(),
    )


class TestToolIntentToReceipt:
    """Tool intent → approval → execution → receipt flow."""

    def test_create_receipt_from_intent(self) -> None:
        mgr = _make_manager()
        intent = _make_intent()
        receipt = mgr.create_receipt(intent)
        assert receipt.tool_name == "create_ticket"
        assert receipt.intent == intent
        assert receipt.effect == EffectState.PENDING

    def test_receipt_has_unique_id(self) -> None:
        mgr = _make_manager()
        r1 = mgr.create_receipt(_make_intent("tool_a"))
        r2 = mgr.create_receipt(_make_intent("tool_b"))
        assert r1.id != r2.id


class TestProviderIdempotency:
    """Provider idempotency (read-after-write)."""

    def test_execute_records_provider_response(self) -> None:
        mgr = _make_manager()
        receipt = mgr.create_receipt(_make_intent())
        executed = mgr.record_execution(
            receipt.id,
            effect=EffectState.SUCCESS,
            provider_response={"ticket_id": "TK-001"},
            idempotency_key="idem-123",
        )
        assert executed.provider_response == {"ticket_id": "TK-001"}
        assert executed.idempotency_key == "idem-123"
        assert executed.effect == EffectState.SUCCESS


class TestEffectUnknown:
    """effect_unknown for uncertain results (never auto-retried)."""

    def test_effect_unknown_on_uncertain_result(self) -> None:
        mgr = _make_manager()
        receipt = mgr.create_receipt(_make_intent())
        uncertain = mgr.record_execution(
            receipt.id,
            effect=EffectState.UNKNOWN,
            provider_response=None,
            idempotency_key="idem-456",
        )
        assert uncertain.effect == EffectState.UNKNOWN
        assert uncertain.auto_retry is False

    def test_effect_unknown_must_not_be_retried(self) -> None:
        mgr = _make_manager()
        receipt = mgr.create_receipt(_make_intent())
        mgr.record_execution(
            receipt.id,
            effect=EffectState.UNKNOWN,
            provider_response=None,
            idempotency_key="idem-789",
        )
        with pytest.raises(ValueError, match=r"auto-retry.*forbidden"):
            mgr.retry(receipt.id)


class TestEffectStates:
    """effect_success, effect_failure, effect_unknown states."""

    def test_success_state(self) -> None:
        mgr = _make_manager()
        receipt = mgr.create_receipt(_make_intent())
        mgr.record_execution(receipt.id, effect=EffectState.SUCCESS, provider_response={"ok": True})
        assert mgr.get(receipt.id).effect == EffectState.SUCCESS

    def test_failure_state(self) -> None:
        mgr = _make_manager()
        receipt = mgr.create_receipt(_make_intent())
        mgr.record_execution(receipt.id, effect=EffectState.FAILURE, provider_response={"error": "bad"})
        assert mgr.get(receipt.id).effect == EffectState.FAILURE

    def test_failure_can_be_retried(self) -> None:
        mgr = _make_manager()
        receipt = mgr.create_receipt(_make_intent())
        mgr.record_execution(receipt.id, effect=EffectState.FAILURE, provider_response={"error": "bad"})
        retried = mgr.retry(receipt.id)
        assert retried.effect == EffectState.PENDING


class TestCancellationEffectState:
    """Cancellation stops new tasks, records in-flight effect state."""

    def test_cancel_sets_cancelled_effect(self) -> None:
        mgr = _make_manager()
        receipt = mgr.create_receipt(_make_intent())
        cancelled = mgr.cancel(receipt.id)
        assert cancelled.effect == EffectState.CANCELLED

    def test_cancel_does_not_affect_other_receipts(self) -> None:
        mgr = _make_manager()
        r1 = mgr.create_receipt(_make_intent("tool_a"))
        r2 = mgr.create_receipt(_make_intent("tool_b"))
        mgr.cancel(r1.id)
        assert mgr.get(r2.id).effect == EffectState.PENDING
