"""S3-T3 Canonical state projection from events.

State is the deterministic projection of canonical events. The reducer
processes events in sequence order and produces a frozen snapshot.
"""

from __future__ import annotations

from typing import Any

from zhiwei.context.types import ContextCategory, ContextItem, SourceRef


class CanonicalState:
    """Immutable snapshot of canonical context state.

    Produced by the reducer after processing events in order.
    Tracks authoritative inventory and all context items with source refs.
    """

    __slots__ = ("_head_event_digest", "_items", "_sequence_no")

    def __init__(
        self,
        items: tuple[ContextItem, ...] = (),
        sequence_no: int = 0,
        head_event_digest: str | None = None,
    ) -> None:
        object.__setattr__(self, "_items", items)
        object.__setattr__(self, "_sequence_no", sequence_no)
        object.__setattr__(self, "_head_event_digest", head_event_digest)

    @property
    def items(self) -> tuple[ContextItem, ...]:
        return self._items

    @property
    def sequence_no(self) -> int:
        return self._sequence_no

    @property
    def head_event_digest(self) -> str | None:
        return self._head_event_digest

    def authoritative_items(self) -> tuple[ContextItem, ...]:
        """Return only authoritative context items."""
        return tuple(
            item for item in self._items if item.category == ContextCategory.AUTHORITATIVE
        )

    def conversational_items(self) -> tuple[ContextItem, ...]:
        return tuple(
            item for item in self._items if item.category == ContextCategory.CONVERSATIONAL
        )

    def recoverable_items(self) -> tuple[ContextItem, ...]:
        return tuple(
            item for item in self._items if item.category == ContextCategory.RECOVERABLE
        )

    def opaque_items(self) -> tuple[ContextItem, ...]:
        return tuple(
            item for item in self._items if item.category == ContextCategory.OPAQUE
        )

    def with_items(self, items: tuple[ContextItem, ...]) -> CanonicalState:
        """Return a new state with replaced items."""
        return CanonicalState(
            items=items,
            sequence_no=self._sequence_no,
            head_event_digest=self._head_event_digest,
        )

    def with_sequence(self, sequence_no: int, head_event_digest: str | None) -> CanonicalState:
        """Return a new state with updated sequence metadata."""
        return CanonicalState(
            items=self._items,
            sequence_no=sequence_no,
            head_event_digest=head_event_digest,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CanonicalState):
            return NotImplemented
        return (
            self._items == other._items
            and self._sequence_no == other._sequence_no
            and self._head_event_digest == other._head_event_digest
        )

    def __repr__(self) -> str:
        return (
            f"CanonicalState(items={len(self._items)}, "
            f"sequence_no={self._sequence_no}, "
            f"head_event_digest={self._head_event_digest!r})"
        )


def build_source_ref(event: dict[str, Any]) -> SourceRef:
    """Build a SourceRef from a canonical event dict."""
    return SourceRef(
        event_id=str(event.get("id", "")),
        sequence_no=int(event.get("sequence_no", 0)),
        event_type=str(event.get("event_type", "")),
        event_digest=str(event.get("event_digest", "")),
    )
