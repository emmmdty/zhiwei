"""Content freshness tracking: determine if a SourceVersion is still fresh.

Freshness is used to decide whether Evidence derived from a version
should be surfaced for new queries.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.time import utc_now
from zhiwei.knowledge.contracts import SourceVersion, SourceVersionState


class FreshnessState(StrEnum):
    """Freshness classification for a SourceVersion."""

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    EXPIRED = "expired"


class FreshnessPolicy(BaseModel):
    """Configurable freshness thresholds for source content.

    Each connector type can have its own policy. Default thresholds
    are conservative and suitable for most enterprise sources.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector: str = Field(min_length=1)
    max_age: timedelta = Field(default=timedelta(days=30))
    aging_threshold: timedelta = Field(default=timedelta(days=7))
    expire_after: timedelta | None = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FreshnessResult(BaseModel):
    """Result of a freshness evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: FreshnessState
    version_id: str
    observed_at: datetime
    age: timedelta
    policy_connector: str


_DEFAULT_POLICY = FreshnessPolicy(connector="default")


def evaluate_freshness(
    version: SourceVersion,
    policy: FreshnessPolicy | None = None,
    *,
    reference_time: datetime | None = None,
) -> FreshnessResult:
    """Evaluate the freshness of a SourceVersion against a policy.

    Args:
        version: The version to evaluate.
        policy: Freshness policy for the connector type. Uses default if None.
        reference_time: Time to evaluate against. Defaults to utc_now().

    Returns:
        FreshnessResult with the evaluated state.
    """
    effective_policy = policy or _DEFAULT_POLICY
    now = reference_time or utc_now()
    age = now - version.observed_at

    # Revoked/tombstone versions are always expired
    if version.state in (SourceVersionState.REVOKED, SourceVersionState.STALE):
        state = FreshnessState.STALE
    elif effective_policy.expire_after and age > effective_policy.expire_after:
        state = FreshnessState.EXPIRED
    elif age > effective_policy.max_age:
        state = FreshnessState.STALE
    elif age > effective_policy.aging_threshold:
        state = FreshnessState.AGING
    else:
        state = FreshnessState.FRESH

    return FreshnessResult(
        state=state,
        version_id=str(version.id),
        observed_at=version.observed_at,
        age=age,
        policy_connector=effective_policy.connector,
    )
