"""S3-T6 Token ROI metrics per ADR-002.

Per MODELS.md §7.2:
- Per Run/trajectory, not per call
- weighted_tokens: 1.0×new_input + 0.1×cache_read + 4.0×output
- authoritative_token_share, evidence_per_kilotoken, recoverable_reload_waste
- context_utilization, compression_ratio, cost_per_completed_task
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TokenWeights(BaseModel):
    """Configurable token cost weights per profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_input: float = 1.0
    cache_read: float = 0.1
    output: float = 4.0


class TokenUsage(BaseModel):
    """Raw token counts for a single LLM call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_input_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class RunUsageSnapshot(BaseModel):
    """Aggregated token usage for a complete Run/trajectory.

    Not per-call: accumulates across all attempts in the run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_new_input_tokens: int = Field(ge=0, default=0)
    total_cache_read_tokens: int = Field(ge=0, default=0)
    total_output_tokens: int = Field(ge=0, default=0)
    authoritative_tokens_sent: int = Field(ge=0, default=0)
    total_tokens_sent: int = Field(ge=0, default=0)
    verified_evidence_count: int = Field(ge=0, default=0)
    recoverable_reload_tokens: int = Field(ge=0, default=0)
    context_window: int = Field(ge=0, default=0)
    compression_input_tokens: int = Field(ge=0, default=0)
    compression_output_tokens: int = Field(ge=0, default=0)
    completed_task_count: int = Field(ge=0, default=0)
    weights: TokenWeights = Field(default_factory=TokenWeights)


class RunUsageMetrics(BaseModel):
    """Computed ROI metrics for a Run/trajectory.

    All derived from RunUsageSnapshot via compute_run_usage().
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    weighted_tokens: float = Field(ge=0.0)
    authoritative_token_share: float = Field(ge=0.0, le=1.0)
    evidence_per_kilotoken: float = Field(ge=0.0)
    recoverable_reload_waste: float = Field(ge=0.0)
    context_utilization: float = Field(ge=0.0, le=1.0)
    compression_ratio: float = Field(ge=0.0)
    cost_per_completed_task: float = Field(ge=0.0)


def compute_run_usage(snapshot: RunUsageSnapshot) -> RunUsageMetrics:
    """Compute all ROI metrics from a run-level usage snapshot.

    Per MODELS.md §7.2, these are trajectory-level metrics, not per-call.
    """
    w = snapshot.weights
    weighted = (
        w.new_input * snapshot.total_new_input_tokens
        + w.cache_read * snapshot.total_cache_read_tokens
        + w.output * snapshot.total_output_tokens
    )

    if snapshot.total_tokens_sent > 0:
        auth_share = snapshot.authoritative_tokens_sent / snapshot.total_tokens_sent
    else:
        auth_share = 0.0

    if weighted > 0:
        evidence_per_kt = (
            snapshot.verified_evidence_count / (weighted / 1000.0)
        )
    else:
        evidence_per_kt = 0.0

    if snapshot.total_tokens_sent > 0:
        reload_waste_ratio = (
            snapshot.recoverable_reload_tokens / snapshot.total_tokens_sent
        )
    else:
        reload_waste_ratio = 0.0

    if snapshot.context_window > 0:
        total_input = (
            snapshot.total_new_input_tokens + snapshot.total_cache_read_tokens
        )
        utilization = min(1.0, total_input / snapshot.context_window)
    else:
        utilization = 0.0

    if snapshot.compression_input_tokens > 0:
        comp_ratio = (
            snapshot.compression_output_tokens / snapshot.compression_input_tokens
        )
    else:
        comp_ratio = 0.0

    if snapshot.completed_task_count > 0:
        cost_per_task = weighted / snapshot.completed_task_count
    else:
        cost_per_task = 0.0

    return RunUsageMetrics(
        weighted_tokens=weighted,
        authoritative_token_share=min(1.0, auth_share),
        evidence_per_kilotoken=evidence_per_kt,
        recoverable_reload_waste=reload_waste_ratio,
        context_utilization=utilization,
        compression_ratio=comp_ratio,
        cost_per_completed_task=cost_per_task,
    )


def compute_weighted_tokens(
    usage: TokenUsage,
    weights: TokenWeights | None = None,
) -> float:
    """Compute weighted token cost for a single call.

    For trajectory-level aggregation, sum these across calls.
    """
    w = weights or TokenWeights()
    return (
        w.new_input * usage.new_input_tokens
        + w.cache_read * usage.cache_read_tokens
        + w.output * usage.output_tokens
    )
