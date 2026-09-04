"""S3-T3 Authoritative content inventory.

Tracks what authoritative content is present in a canonical state projection.
Used by the Context Compiler to determine compression priority and refusal conditions.
"""

from __future__ import annotations

from typing import Any

from zhiwei.context.state import CanonicalState
from zhiwei.context.types import AuthoritativeKind, ContextCategory, ContextItem


class InventoryEntry:
    """A single tracked authoritative content entry."""

    __slots__ = ("count", "item_ids", "kind", "total_size_bytes")

    def __init__(self, kind: AuthoritativeKind) -> None:
        self.kind = kind
        self.count = 0
        self.total_size_bytes = 0
        self.item_ids: list[str] = []

    def add(self, item_id: str, size_bytes: int) -> None:
        self.count += 1
        self.total_size_bytes += size_bytes
        if item_id:
            self.item_ids.append(item_id)

    def __repr__(self) -> str:
        return (
            f"InventoryEntry(kind={self.kind.value!r}, count={self.count}, "
            f"total_size_bytes={self.total_size_bytes})"
        )


class AuthoritativeInventory:
    """Inventory of all authoritative content in a canonical state.

    Per MODELS.md §4: inventory is step 1 of the Context Compiler.
    Tracks by kind: objective/constraint/task/entity/decision/conflict/
    evidence/action/approval/budget/obligation.
    """

    __slots__ = ("_entries", "_total_authoritative_items")

    def __init__(self) -> None:
        self._entries: dict[AuthoritativeKind, InventoryEntry] = {}
        self._total_authoritative_items = 0

    @classmethod
    def from_state(cls, state: CanonicalState) -> AuthoritativeInventory:
        """Build an inventory from a canonical state."""
        inv = cls()
        for item in state.authoritative_items():
            inv.track(item)
        return inv

    def track(self, item: ContextItem) -> None:
        """Track a single context item in the inventory."""
        if item.category != ContextCategory.AUTHORITATIVE:
            return

        kind = item.kind or AuthoritativeKind.OBJECTIVE
        if kind not in self._entries:
            self._entries[kind] = InventoryEntry(kind)

        item_id = item.content.get("id", "")
        size_bytes = _estimate_size(item.content)
        self._entries[kind].add(str(item_id), size_bytes)
        self._total_authoritative_items += 1

    def get_entry(self, kind: AuthoritativeKind) -> InventoryEntry | None:
        return self._entries.get(kind)

    def entries(self) -> dict[AuthoritativeKind, InventoryEntry]:
        return dict(self._entries)

    @property
    def total_authoritative_items(self) -> int:
        return self._total_authoritative_items

    @property
    def total_size_bytes(self) -> int:
        return sum(e.total_size_bytes for e in self._entries.values())

    def has_kind(self, kind: AuthoritativeKind) -> bool:
        return kind in self._entries and self._entries[kind].count > 0

    def kinds_present(self) -> tuple[AuthoritativeKind, ...]:
        return tuple(sorted(self._entries.keys(), key=lambda k: k.value))

    def is_complete(self) -> bool:
        """Check if all authoritative kinds are represented."""
        return len(self._entries) == len(AuthoritativeKind)

    def missing_kinds(self) -> tuple[AuthoritativeKind, ...]:
        """Return kinds not yet present in the inventory."""
        present = set(self._entries.keys())
        return tuple(
            sorted(
                (k for k in AuthoritativeKind if k not in present),
                key=lambda k: k.value,
            )
        )

    def summary(self) -> dict[str, Any]:
        """Return a serializable summary of the inventory."""
        return {
            "total_items": self._total_authoritative_items,
            "total_size_bytes": self.total_size_bytes,
            "kinds": {
                kind.value: {"count": entry.count, "size_bytes": entry.total_size_bytes}
                for kind, entry in sorted(self._entries.items(), key=lambda x: x[0].value)
            },
            "missing_kinds": [k.value for k in self.missing_kinds()],
        }

    def __repr__(self) -> str:
        return (
            f"AuthoritativeInventory(total_items={self._total_authoritative_items}, "
            f"kinds={len(self._entries)})"
        )


def _estimate_size(content: dict[str, Any]) -> int:
    """Estimate the byte size of a content dict for inventory tracking."""
    return len(str(content).encode("utf-8"))
