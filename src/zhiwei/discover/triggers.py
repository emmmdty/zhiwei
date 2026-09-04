"""S8 Trigger types for the Discover pipeline.

Trigger → watermark/snapshot → DataQualityResult → Signal.

三种触发器：schedule（cron）、webhook（HTTP callback）、source_delta（数据源变更检测）。
每种触发器都有确定性的参数，不依赖 LLM 判断。

事实源：specs/s8-discover-actions.md §4。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.time import ensure_utc


class TriggerType(StrEnum):
    """Trigger type discriminator."""

    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    SOURCE_DELTA = "source_delta"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ScheduleTrigger(_FrozenModel):
    """Cron-based schedule trigger.

    Evaluates a cron expression to determine when the pipeline fires.
    The cron evaluation is deterministic — no LLM involved.
    """

    type: TriggerType = TriggerType.SCHEDULE
    cron_expression: str = Field(min_length=1, description="Standard 5-field cron expression")
    timezone: str = Field(default="UTC")
    max_missed_fires: int = Field(ge=0, default=3)

    @field_validator("cron_expression")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        parts = value.split()
        if len(parts) not in (5, 6):
            raise ValueError("cron expression must have 5 or 6 fields")
        return value


class WebhookTrigger(_FrozenModel):
    """HTTP webhook trigger.

    Receives an HTTP callback and validates it against a shared secret.
    The secret is never stored in plaintext — only a digest is retained.
    """

    type: TriggerType = TriggerType.WEBHOOK
    path: str = Field(min_length=1, description="URL path suffix for the webhook endpoint")
    secret_digest: str = Field(min_length=1, description="SHA-256 digest of the shared secret")
    allowed_ips: tuple[str, ...] = Field(default_factory=tuple)
    max_payload_bytes: int = Field(ge=0, default=1_048_576)


class SourceDeltaTrigger(_FrozenModel):
    """Source diff/watermark change trigger.

    Fires when a monitored data source produces new or changed records
    since the last watermark. The delta detection is deterministic.
    """

    type: TriggerType = TriggerType.SOURCE_DELTA
    source_id: UUID
    watermark_field: str = Field(min_length=1, description="Field name used for watermark comparison")
    min_change_threshold: float = Field(ge=0.0, default=0.0)
    lookback_window_hours: int = Field(ge=1, default=24)


Trigger = ScheduleTrigger | WebhookTrigger | SourceDeltaTrigger


class TriggerState(StrEnum):
    """Trigger runtime state."""

    IDLE = "idle"
    FIRING = "firing"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"


class TriggerRecord(_FrozenModel):
    """Persistent record of a trigger bound to a program version.

    Each trigger is identified by a UUID and bound to exactly one
    program version. Changing the trigger configuration produces
    a new TriggerRecord.
    """

    id: UUID
    program_version_id: UUID
    trigger: Trigger
    state: TriggerState = TriggerState.IDLE
    last_fired_at: datetime | None = None
    consecutive_failures: int = Field(ge=0, default=0)
    created_at: datetime

    @field_validator("last_fired_at", check_fields=False)
    @classmethod
    def _utc_last_fired(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            return ensure_utc(value)
        return value
