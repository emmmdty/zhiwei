"""S3-T3 RED: Authoritative inventory tests."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from zhiwei.context.inventory import AuthoritativeInventory, InventoryEntry
from zhiwei.context.reducer import reduce_events
from zhiwei.context.state import CanonicalState
from zhiwei.context.types import AuthoritativeKind, ContextCategory, ContextItem

# ---- Helpers ----


def _event(
    event_id: str,
    seq: int,
    event_type: str,
    payload: dict | None = None,
    event_digest: str = "sha256:abc",
) -> dict:
    return {
        "id": event_id,
        "sequence_no": seq,
        "event_type": event_type,
        "payload": payload or {},
        "event_digest": event_digest,
    }


def _make_item(
    item_id: str,
    kind: str = "objective",
    category: ContextCategory = ContextCategory.AUTHORITATIVE,
    **extra: object,
) -> ContextItem:
    content = {"id": item_id, "kind": kind, **extra}
    return ContextItem(
        category=category,
        content=content,
        kind=AuthoritativeKind(kind) if kind in AuthoritativeKind.__members__.values() else None,
    )


# ---- InventoryEntry tests ----


class TestInventoryEntry:
    def test_add(self) -> None:
        entry = InventoryEntry(AuthoritativeKind.OBJECTIVE)
        entry.add("item-1", 100)
        assert entry.count == 1
        assert entry.total_size_bytes == 100
        assert entry.item_ids == ["item-1"]

    def test_add_multiple(self) -> None:
        entry = InventoryEntry(AuthoritativeKind.CONSTRAINT)
        entry.add("c1", 50)
        entry.add("c2", 75)
        assert entry.count == 2
        assert entry.total_size_bytes == 125
        assert entry.item_ids == ["c1", "c2"]

    def test_add_empty_id(self) -> None:
        entry = InventoryEntry(AuthoritativeKind.TASK)
        entry.add("", 30)
        assert entry.count == 1
        assert entry.item_ids == []


# ---- AuthoritativeInventory tests ----


class TestAuthoritativeInventory:
    def test_empty_inventory(self) -> None:
        inv = AuthoritativeInventory()
        assert inv.total_authoritative_items == 0
        assert inv.total_size_bytes == 0
        assert inv.kinds_present() == ()

    def test_track_single_item(self) -> None:
        inv = AuthoritativeInventory()
        inv.track(_make_item("o1", "objective"))
        assert inv.total_authoritative_items == 1
        assert inv.has_kind(AuthoritativeKind.OBJECTIVE)
        assert not inv.has_kind(AuthoritativeKind.CONSTRAINT)

    def test_track_non_authoritative_ignored(self) -> None:
        inv = AuthoritativeInventory()
        inv.track(_make_item("r1", "objective", category=ContextCategory.RECOVERABLE))
        assert inv.total_authoritative_items == 0

    def test_from_state(self) -> None:
        state = CanonicalState(
            items=(
                _make_item("o1", "objective"),
                _make_item("c1", "constraint"),
                _make_item("t1", "task"),
                _make_item("e1", "entity"),
            )
        )
        inv = AuthoritativeInventory.from_state(state)
        assert inv.total_authoritative_items == 4
        assert inv.has_kind(AuthoritativeKind.OBJECTIVE)
        assert inv.has_kind(AuthoritativeKind.CONSTRAINT)
        assert inv.has_kind(AuthoritativeKind.TASK)
        assert inv.has_kind(AuthoritativeKind.ENTITY)

    def test_kinds_present_sorted(self) -> None:
        state = CanonicalState(
            items=(
                _make_item("t1", "task"),
                _make_item("o1", "objective"),
                _make_item("c1", "constraint"),
            )
        )
        inv = AuthoritativeInventory.from_state(state)
        kinds = inv.kinds_present()
        assert kinds == tuple(sorted(kinds, key=lambda k: k.value))

    def test_is_complete_when_all_kinds_present(self) -> None:
        state = CanonicalState(
            items=tuple(
                _make_item(f"item-{k.value}", k.value) for k in AuthoritativeKind
            )
        )
        inv = AuthoritativeInventory.from_state(state)
        assert inv.is_complete()

    def test_is_incomplete_when_missing_kinds(self) -> None:
        state = CanonicalState(
            items=(_make_item("o1", "objective"),)
        )
        inv = AuthoritativeInventory.from_state(state)
        assert not inv.is_complete()
        missing = inv.missing_kinds()
        assert AuthoritativeKind.OBJECTIVE not in missing
        assert len(missing) == len(AuthoritativeKind) - 1

    def test_missing_kinds_sorted(self) -> None:
        state = CanonicalState(items=(_make_item("o1", "objective"),))
        inv = AuthoritativeInventory.from_state(state)
        missing = inv.missing_kinds()
        assert missing == tuple(sorted(missing, key=lambda k: k.value))

    def test_summary(self) -> None:
        state = CanonicalState(
            items=(
                _make_item("o1", "objective"),
                _make_item("c1", "constraint"),
            )
        )
        inv = AuthoritativeInventory.from_state(state)
        s = inv.summary()
        assert s["total_items"] == 2
        assert "objective" in s["kinds"]
        assert "constraint" in s["kinds"]
        assert s["kinds"]["objective"]["count"] == 1

    def test_get_entry(self) -> None:
        inv = AuthoritativeInventory()
        inv.track(_make_item("o1", "objective"))
        entry = inv.get_entry(AuthoritativeKind.OBJECTIVE)
        assert entry is not None
        assert entry.count == 1
        assert inv.get_entry(AuthoritativeKind.CONSTRAINT) is None

    def test_entries(self) -> None:
        inv = AuthoritativeInventory()
        inv.track(_make_item("o1", "objective"))
        inv.track(_make_item("c1", "constraint"))
        entries = inv.entries()
        assert AuthoritativeKind.OBJECTIVE in entries
        assert AuthoritativeKind.CONSTRAINT in entries

    def test_total_size_bytes(self) -> None:
        inv = AuthoritativeInventory()
        inv.track(_make_item("o1", "objective"))
        inv.track(_make_item("c1", "constraint"))
        assert inv.total_size_bytes > 0

    def test_repr(self) -> None:
        inv = AuthoritativeInventory()
        r = repr(inv)
        assert "AuthoritativeInventory" in r


# ---- Complete authoritative inventory from reducer ----


class TestCompleteInventory:
    def test_all_authoritative_kinds_inventory(self) -> None:
        events = [
            _event("e1", 1, "context.created", {"content": {"id": "o1", "kind": "objective"}}),
            _event("e2", 2, "context.created", {"content": {"id": "c1", "kind": "constraint"}}),
            _event("e3", 3, "context.created", {"content": {"id": "t1", "kind": "task"}}),
            _event("e4", 4, "context.created", {"content": {"id": "e1", "kind": "entity"}}),
            _event("e5", 5, "context.created", {"content": {"id": "d1", "kind": "decision"}}),
            _event("e6", 6, "context.created", {"content": {"id": "cf1", "kind": "conflict"}}),
            _event("e7", 7, "context.created", {"content": {"id": "ev1", "kind": "evidence"}}),
            _event("e8", 8, "context.created", {"content": {"id": "a1", "kind": "action"}}),
            _event("e9", 9, "context.created", {"content": {"id": "ap1", "kind": "approval"}}),
            _event("e10", 10, "context.created", {"content": {"id": "b1", "kind": "budget"}}),
            _event("e11", 11, "context.created", {"content": {"id": "ob1", "kind": "obligation"}}),
        ]
        state = reduce_events(events)
        inv = AuthoritativeInventory.from_state(state)
        assert inv.is_complete()
        assert inv.total_authoritative_items == 11
        assert inv.missing_kinds() == ()

    def test_inventory_survives_update_and_delete(self) -> None:
        events = [
            _event("e1", 1, "context.created", {"content": {"id": "o1", "kind": "objective"}}),
            _event("e2", 2, "context.created", {"content": {"id": "c1", "kind": "constraint"}}),
            _event("e3", 3, "context.updated", {"target_id": "o1", "updates": {"name": "updated"}}),
            _event("e4", 4, "context.deleted", {"target_id": "c1"}),
        ]
        state = reduce_events(events)
        inv = AuthoritativeInventory.from_state(state)
        assert inv.total_authoritative_items == 1
        assert inv.has_kind(AuthoritativeKind.OBJECTIVE)
        assert not inv.has_kind(AuthoritativeKind.CONSTRAINT)


# ---- Property tests ----


class TestPropertyInventory:
    @given(
        kind_values=st.lists(
            st.sampled_from([k.value for k in AuthoritativeKind]),
            min_size=1,
            max_size=11,
            unique=True,
        )
    )
    @settings(max_examples=50)
    def test_inventory_count_matches_authoritative_items(
        self, kind_values: list[str]
    ) -> None:
        state = CanonicalState(
            items=tuple(
                _make_item(f"item-{k}", k) for k in kind_values
            )
        )
        inv = AuthoritativeInventory.from_state(state)
        assert inv.total_authoritative_items == len(kind_values)
        assert len(inv.kinds_present()) == len(kind_values)

    @given(kind_value=st.sampled_from([k.value for k in AuthoritativeKind]))
    @settings(max_examples=20)
    def test_single_kind_inventory(self, kind_value: str) -> None:
        state = CanonicalState(items=(_make_item("item-1", kind_value),))
        inv = AuthoritativeInventory.from_state(state)
        assert inv.total_authoritative_items == 1
        expected_kind = AuthoritativeKind(kind_value)
        assert inv.has_kind(expected_kind)
        assert not inv.is_complete()

    @given(
        event_count=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=30)
    def test_reducer_inventory_consistency(self, event_count: int) -> None:
        events = [
            _event(
                f"e{i}", i + 1, "context.created",
                {"content": {"id": f"item-{i}", "kind": "objective"}},
            )
            for i in range(event_count)
        ]
        state = reduce_events(events)
        inv = AuthoritativeInventory.from_state(state)
        assert inv.total_authoritative_items == event_count
        assert inv.has_kind(AuthoritativeKind.OBJECTIVE)

    @given(
        creates=st.integers(min_value=1, max_value=10),
        deletes=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=30)
    def test_inventory_after_deletes(self, creates: int, deletes: int) -> None:
        events = [
            _event(f"e{i}", i + 1, "context.created",
                   {"content": {"id": f"item-{i}", "kind": "objective"}})
            for i in range(creates)
        ]
        for i in range(min(deletes, creates)):
            events.append(
                _event(f"d{i}", creates + i + 1, "context.deleted",
                       {"target_id": f"item-{i}"})
            )
        state = reduce_events(events)
        inv = AuthoritativeInventory.from_state(state)
        expected_count = creates - min(deletes, creates)
        assert inv.total_authoritative_items == expected_count
