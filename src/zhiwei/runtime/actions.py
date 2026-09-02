"""S2 runtime: ToolIntent, ActionReceipt, effect tracking。

事实源：design doc §4.3、S2-T5 plan。

Tool intent → approval → execution → receipt. Provider idempotency via
read-after-write. effect_unknown for uncertain results (never auto-retried).
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from zhiwei.contracts.identifiers import new_id


class EffectState(StrEnum):
    """Effect states for an action receipt."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class ToolIntent(BaseModel):
    """Describes an intent to invoke a tool with given parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    parameters: dict[str, object]
    run_id: UUID
    task_id: str
    approval_id: UUID


class ActionReceipt(BaseModel):
    """Tracks the full lifecycle of a tool action: intent → approval → execution → receipt.

    Provider idempotency is tracked via idempotency_key. effect_unknown is
    never auto-retried.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    intent: ToolIntent
    tool_name: str
    effect: EffectState = EffectState.PENDING
    provider_response: dict[str, object] | None = None
    idempotency_key: str | None = None
    auto_retry: bool = True
    retry_count: int = 0


class ActionReceiptManager:
    """Manages action receipts and effect tracking."""

    def __init__(self) -> None:
        self._receipts: dict[UUID, ActionReceipt] = {}

    def create_receipt(self, intent: ToolIntent) -> ActionReceipt:
        """Create a receipt from a tool intent."""
        receipt = ActionReceipt(
            id=new_id(),
            intent=intent,
            tool_name=intent.tool_name,
        )
        self._receipts[receipt.id] = receipt
        return receipt

    def get(self, receipt_id: UUID) -> ActionReceipt:
        """Get a receipt by ID."""
        receipt = self._receipts.get(receipt_id)
        if receipt is None:
            raise ValueError(f"Receipt {receipt_id} not found")
        return receipt

    def record_execution(
        self,
        receipt_id: UUID,
        *,
        effect: EffectState,
        provider_response: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> ActionReceipt:
        """Record the result of executing a tool action."""
        receipt = self.get(receipt_id)
        auto_retry = effect != EffectState.UNKNOWN
        updated = receipt.model_copy(update={
            "effect": effect,
            "provider_response": provider_response,
            "idempotency_key": idempotency_key,
            "auto_retry": auto_retry,
        })
        self._receipts[receipt_id] = updated
        return updated

    def retry(self, receipt_id: UUID) -> ActionReceipt:
        """Retry a failed receipt. effect_unknown receipts cannot be retried."""
        receipt = self.get(receipt_id)
        if receipt.effect == EffectState.UNKNOWN:
            raise ValueError(
                "auto-retry for effect_unknown is forbidden"
            )
        updated = receipt.model_copy(update={
            "effect": EffectState.PENDING,
            "provider_response": None,
            "retry_count": receipt.retry_count + 1,
        })
        self._receipts[receipt_id] = updated
        return updated

    def cancel(self, receipt_id: UUID) -> ActionReceipt:
        """Cancel a receipt, recording in-flight effect state."""
        receipt = self.get(receipt_id)
        updated = receipt.model_copy(update={
            "effect": EffectState.CANCELLED,
            "auto_retry": False,
        })
        self._receipts[receipt_id] = updated
        return updated
