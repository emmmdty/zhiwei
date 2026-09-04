"""S3-T4 Token budget estimation and context fit checking.

ADR-002: context fit is hard constraint, token budget is ROI metric not gate.
Three-level counting: authoritative_count / verified_local_count / calibrated_estimate.
"""

from __future__ import annotations

import math
from typing import Any

from zhiwei.context.ir import TokenCountingLevel, TokenEstimate
from zhiwei.context.types import ContextCategory, ContextItem


def estimate_tokens_text(text: str) -> int:
    """Conservative text token estimate.

    Uses ~4 chars per token heuristic (calibrated for English/code mix).
    This is level 3 (calibrated_estimate) baseline.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def estimate_tokens_content(content: dict[str, Any]) -> TokenEstimate:
    """Estimate tokens for a single content dict using calibrated estimation.

    Produces level 3 (calibrated_estimate) with conservative margin.
    """
    text = _content_to_text(content)
    count = estimate_tokens_text(text)
    # 15% margin for calibrated estimation
    margin = max(1, math.ceil(count * 0.15))
    return TokenEstimate(count=count, level=TokenCountingLevel.CALIBRATED, margin=margin)


def estimate_tokens_item(item: ContextItem) -> TokenEstimate:
    """Estimate tokens for a ContextItem."""
    return estimate_tokens_content(item.content)


def estimate_tokens_items(items: tuple[ContextItem, ...]) -> TokenEstimate:
    """Estimate total tokens for a collection of items.

    Aggregates counts and margins across all items.
    """
    total_count = 0
    total_margin = 0
    for item in items:
        est = estimate_tokens_item(item)
        total_count += est.count
        total_margin += est.margin
    return TokenEstimate(
        count=total_count,
        level=TokenCountingLevel.CALIBRATED,
        margin=total_margin,
    )


def count_by_category(
    items: tuple[ContextItem, ...],
) -> dict[ContextCategory, TokenEstimate]:
    """Estimate tokens grouped by context category."""
    buckets: dict[ContextCategory, list[TokenEstimate]] = {
        cat: [] for cat in ContextCategory
    }
    for item in items:
        buckets[item.category].append(estimate_tokens_item(item))

    result: dict[ContextCategory, TokenEstimate] = {}
    for cat, estimates in buckets.items():
        if estimates:
            total_count = sum(e.count for e in estimates)
            total_margin = sum(e.margin for e in estimates)
            result[cat] = TokenEstimate(
                count=total_count,
                level=TokenCountingLevel.CALIBRATED,
                margin=total_margin,
            )
    return result


class ContextFitCheck:
    """Validate that context items fit within a model's context window.

    context fit is hard constraint (ADR-002). Token budget is ROI metric not gate.
    """

    __slots__ = ("context_window", "output_reserve", "system_reserve")

    def __init__(
        self,
        context_window: int,
        system_reserve: int = 0,
        output_reserve: int = 0,
    ) -> None:
        if context_window <= 0:
            raise ValueError("context_window must be positive")
        self.context_window = context_window
        self.system_reserve = system_reserve
        self.output_reserve = output_reserve

    @property
    def available_tokens(self) -> int:
        """Tokens available for context after reserves."""
        return max(0, self.context_window - self.system_reserve - self.output_reserve)

    def fits(
        self,
        items: tuple[ContextItem, ...],
    ) -> tuple[bool, TokenEstimate]:
        """Check if items fit within the available context budget.

        Returns (fits, estimate) where estimate is the total token estimate
        including margins.
        """
        estimate = estimate_tokens_items(items)
        return estimate.upper_bound <= self.available_tokens, estimate

    def fits_after_compression(
        self,
        compressed_items: tuple[ContextItem, ...],
        removed_authoritative: tuple[ContextItem, ...] = (),
    ) -> tuple[bool, TokenEstimate, bool]:
        """Check fit after compression, tracking if authoritative was dropped.

        Returns (fits, estimate, authoritative_dropped).
        """
        estimate = estimate_tokens_items(compressed_items)
        has_authoritative_dropped = any(
            item.category == ContextCategory.AUTHORITATIVE for item in removed_authoritative
        )
        fits = estimate.upper_bound <= self.available_tokens
        return fits, estimate, has_authoritative_dropped


def _content_to_text(content: dict[str, Any]) -> str:
    """Convert content dict to text for token estimation."""
    parts: list[str] = []
    for key, value in content.items():
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}: {value}")
        elif isinstance(value, (list, dict)) or value is not None:
            parts.append(f"{key}: {value!s}")
    return " ".join(parts)
