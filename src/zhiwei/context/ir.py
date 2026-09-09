"""S3-T4 ContextIR: provider-neutral intermediate representation.

Deterministic IR with source/transform map and token estimate confidence.
Output of the Context Compiler pipeline, consumed by transport serializers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from zhiwei.context.types import AuthoritativeKind, ContextCategory, ContextItem


class TransformKind(StrEnum):
    """Track what transformation was applied to produce an IR item."""

    ORIGINAL = "original"
    ARTIFACTIZED = "artifactized"
    RECOVERABLE_REMOVED = "recoverable_removed"
    CONVERSATION_SUMMARIZED = "conversation_summarized"
    TASK_SPLIT = "task_split"


class TokenCountingLevel(StrEnum):
    """ADR-002: three-level token counting.

    level 1: provider official count_tokens API → authoritative_count
    level 2: official local tokenizer implementation → verified_local_count
    level 3: calibrated estimator + conservative margin → calibrated_estimate
    """

    AUTHORITATIVE = "authoritative_count"
    VERIFIED_LOCAL = "verified_local_count"
    CALIBRATED = "calibrated_estimate"


class ContextRefusalKind(StrEnum):
    """Two exit paths from context_refusal (ADR-007)."""

    AUTHORITATIVE_WAIVED = "authoritative_waived"
    EPOCH_ROLLBACK = "epoch_rollback"


class SourceTransform:
    """Maps an IR item back to its source ContextItem and records the applied transform."""

    __slots__ = ("source_item", "transform_detail", "transform_kind")

    def __init__(
        self,
        source_item: ContextItem,
        transform_kind: TransformKind = TransformKind.ORIGINAL,
        transform_detail: str = "",
    ) -> None:
        self.source_item = source_item
        self.transform_kind = transform_kind
        self.transform_detail = transform_detail

    def __repr__(self) -> str:
        return (
            f"SourceTransform(transform={self.transform_kind.value!r}, "
            f"category={self.source_item.category.value!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SourceTransform):
            return NotImplemented
        return (
            self.source_item == other.source_item
            and self.transform_kind == other.transform_kind
            and self.transform_detail == other.transform_detail
        )

    def __hash__(self) -> int:
        return hash((self.source_item, self.transform_kind, self.transform_detail))


class TokenEstimate:
    """Token count at a specific counting level with optional confidence bounds.

    level 1: authoritative_count — provider API returned count
    level 2: verified_local_count — official tokenizer local result
    level 3: calibrated_estimate — calibrated estimator + margin
    """

    __slots__ = ("count", "level", "margin")

    def __init__(
        self,
        count: int,
        level: TokenCountingLevel,
        margin: int = 0,
    ) -> None:
        if count < 0:
            raise ValueError("token count must be non-negative")
        if margin < 0:
            raise ValueError("margin must be non-negative")
        self.count = count
        self.level = level
        self.margin = margin

    @property
    def upper_bound(self) -> int:
        return self.count + self.margin

    def fits_in(self, context_window: int) -> bool:
        """Check if tokens fit in the given context window using upper bound."""
        return self.upper_bound <= context_window

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TokenEstimate):
            return NotImplemented
        return (
            self.count == other.count
            and self.level == other.level
            and self.margin == other.margin
        )

    def __repr__(self) -> str:
        return (
            f"TokenEstimate(count={self.count}, level={self.level.value!r}, "
            f"margin={self.margin})"
        )


class ContextIRItem:
    """A single item in the provider-neutral intermediate representation."""

    __slots__ = (
        "category",
        "content",
        "kind",
        "source_transform",
        "token_estimate",
    )

    def __init__(
        self,
        category: ContextCategory,
        content: dict[str, Any],
        source_transform: SourceTransform,
        token_estimate: TokenEstimate,
        kind: AuthoritativeKind | None = None,
    ) -> None:
        self.category = category
        self.kind = kind
        self.content = content
        self.source_transform = source_transform
        self.token_estimate = token_estimate

    def __repr__(self) -> str:
        return (
            f"ContextIRItem(category={self.category.value!r}, "
            f"transform={self.source_transform.transform_kind.value!r}, "
            f"tokens={self.token_estimate.count})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContextIRItem):
            return NotImplemented
        return (
            self.category == other.category
            and self.kind == other.kind
            and self.content == other.content
            and self.source_transform == other.source_transform
            and self.token_estimate == other.token_estimate
        )

    def __hash__(self) -> int:
        return hash((self.category, self.kind, self.source_transform))


class ContextIR:
    """Deterministic provider-neutral intermediate representation.

    Produced by the Context Compiler. Contains IR items with source/transform
    map for traceability. Each item carries a token estimate at a specific
    counting level.
    """

    __slots__ = (
        "_head_event_digest",
        "_items",
        "_refusal",
        "_sequence_no",
        "_total_token_estimate",
    )

    def __init__(
        self,
        items: tuple[ContextIRItem, ...] = (),
        sequence_no: int = 0,
        head_event_digest: str | None = None,
        refusal: ContextRefusalKind | None = None,
    ) -> None:
        object.__setattr__(self, "_items", items)
        object.__setattr__(self, "_sequence_no", sequence_no)
        object.__setattr__(self, "_head_event_digest", head_event_digest)
        object.__setattr__(self, "_refusal", refusal)
        total = sum(item.token_estimate.upper_bound for item in items)
        object.__setattr__(self, "_total_token_estimate", total)

    @property
    def items(self) -> tuple[ContextIRItem, ...]:
        return self._items

    @property
    def sequence_no(self) -> int:
        return self._sequence_no

    @property
    def head_event_digest(self) -> str | None:
        return self._head_event_digest

    @property
    def total_token_estimate(self) -> int:
        """Total estimated tokens (upper bound) across all items."""
        return self._total_token_estimate

    @property
    def refusal(self) -> ContextRefusalKind | None:
        """Non-None when the compiler could not produce a valid IR."""
        return self._refusal

    @property
    def is_refusal(self) -> bool:
        return self._refusal is not None

    def authoritative_items(self) -> tuple[ContextIRItem, ...]:
        return tuple(i for i in self._items if i.category == ContextCategory.AUTHORITATIVE)

    def conversational_items(self) -> tuple[ContextIRItem, ...]:
        return tuple(i for i in self._items if i.category == ContextCategory.CONVERSATIONAL)

    def recoverable_items(self) -> tuple[ContextIRItem, ...]:
        return tuple(i for i in self._items if i.category == ContextCategory.RECOVERABLE)

    def items_by_transform(self, transform: TransformKind) -> tuple[ContextIRItem, ...]:
        return tuple(
            i for i in self._items if i.source_transform.transform_kind == transform
        )

    def source_map(self) -> list[SourceTransform]:
        """Return the source/transform map for all items."""
        return [item.source_transform for item in self._items]

    def token_estimate_by_category(
        self,
    ) -> dict[ContextCategory, TokenEstimate]:
        """Aggregate token estimates per category."""
        result: dict[ContextCategory, dict[str, int]] = {}
        for item in self._items:
            cat = item.category
            if cat not in result:
                result[cat] = {"count": 0, "margin": 0}
            result[cat]["count"] += item.token_estimate.count
            result[cat]["margin"] += item.token_estimate.margin

        return {
            cat: TokenEstimate(
                count=vals["count"],
                level=TokenCountingLevel.CALIBRATED,
                margin=vals["margin"],
            )
            for cat, vals in result.items()
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContextIR):
            return NotImplemented
        return (
            self._items == other._items
            and self._sequence_no == other._sequence_no
            and self._head_event_digest == other._head_event_digest
            and self._refusal == other._refusal
        )

    def __repr__(self) -> str:
        refusal_str = f", refusal={self._refusal.value!r}" if self._refusal else ""
        return (
            f"ContextIR(items={len(self._items)}, "
            f"total_tokens={self._total_token_estimate}, "
            f"sequence_no={self._sequence_no}{refusal_str})"
        )
