"""S3-T3 RED: Pure reducer and context classification tests."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from zhiwei.context.reducer import apply_event, reduce_events
from zhiwei.context.state import CanonicalState, build_source_ref
from zhiwei.context.types import (
    AuthoritativeKind,
    ContextCategory,
    ContextItem,
    SourceRef,
    classify_content,
)

# ---- Helpers ----


def _event(
    event_id: str,
    seq: int,
    event_type: str,
    payload: dict | None = None,
    event_digest: str = "sha256:abc123",
) -> dict:
    return {
        "id": event_id,
        "sequence_no": seq,
        "event_type": event_type,
        "payload": payload or {},
        "event_digest": event_digest,
    }


def _make_content(kind: str = "objective", **extra: object) -> dict:
    return {"id": "item-1", "kind": kind, **extra}


# ---- Classification tests ----


class TestClassifyContent:
    def test_explicit_category(self) -> None:
        result = classify_content({"category": "opaque"})
        assert result == ContextCategory.OPAQUE

    def test_hidden_reasoning_is_opaque(self) -> None:
        result = classify_content({"hidden_reasoning": "some reasoning"})
        assert result == ContextCategory.OPAQUE

    def test_artifact_ref_only_is_recoverable(self) -> None:
        result = classify_content({"artifact_ref": "s3://bucket/key"})
        assert result == ContextCategory.RECOVERABLE

    def test_artifact_ref_with_other_fields_is_not_recoverable(self) -> None:
        result = classify_content({"artifact_ref": "s3://bucket/key", "extra": "data"})
        assert result != ContextCategory.RECOVERABLE

    def test_summary_with_source_events_is_conversational(self) -> None:
        result = classify_content(
            {"summary": "user asked about X", "source_event_ids": ["e1", "e2"]}
        )
        assert result == ContextCategory.CONVERSATIONAL

    def test_default_is_authoritative(self) -> None:
        result = classify_content({"id": "item-1", "kind": "objective"})
        assert result == ContextCategory.AUTHORITATIVE

    def test_explicit_invalid_category_falls_through(self) -> None:
        result = classify_content({"category": "invalid_category"})
        assert result == ContextCategory.AUTHORITATIVE


# ---- SourceRef tests ----


class TestSourceRef:
    def test_equality(self) -> None:
        a = SourceRef("e1", 1, "context.created", "sha256:aaa")
        b = SourceRef("e1", 1, "context.created", "sha256:aaa")
        assert a == b

    def test_inequality(self) -> None:
        a = SourceRef("e1", 1, "context.created", "sha256:aaa")
        b = SourceRef("e2", 2, "context.updated", "sha256:bbb")
        assert a != b

    def test_hashable(self) -> None:
        a = SourceRef("e1", 1, "context.created", "sha256:aaa")
        b = SourceRef("e1", 1, "context.created", "sha256:aaa")
        assert hash(a) == hash(b)

    def test_repr(self) -> None:
        ref = SourceRef("e1", 1, "context.created", "sha256:aaa")
        r = repr(ref)
        assert "e1" in r
        assert "context.created" in r


# ---- build_source_ref tests ----


class TestBuildSourceRef:
    def test_from_event(self) -> None:
        event = _event("e1", 5, "context.created", event_digest="sha256:ddd")
        ref = build_source_ref(event)
        assert ref.event_id == "e1"
        assert ref.sequence_no == 5
        assert ref.event_type == "context.created"
        assert ref.event_digest == "sha256:ddd"

    def test_missing_fields_default(self) -> None:
        ref = build_source_ref({})
        assert ref.event_id == ""
        assert ref.sequence_no == 0
        assert ref.event_type == ""
        assert ref.event_digest == ""


# ---- Reducer determinism tests ----


class TestReducerDeterminism:
    def test_empty_events_produce_empty_state(self) -> None:
        state = reduce_events([])
        assert state.items == ()
        assert state.sequence_no == 0

    def test_single_event(self) -> None:
        event = _event(
            "e1", 1, "context.created",
            payload={"content": _make_content("objective")},
        )
        state = reduce_events([event])
        assert len(state.items) == 1
        assert state.items[0].category == ContextCategory.AUTHORITATIVE
        assert state.sequence_no == 1

    def test_same_events_same_order_same_state(self) -> None:
        events = [
            _event("e1", 1, "context.created", {"content": _make_content("objective")}),
            _event("e2", 2, "context.created", {"content": _make_content("constraint")}),
            _event("e3", 3, "context.created", {"content": _make_content("task")}),
        ]
        state_a = reduce_events(events)
        state_b = reduce_events(events)
        assert state_a == state_b
        assert state_a.items == state_b.items

    def test_different_order_different_state(self) -> None:
        events_a = [
            _event("e1", 1, "context.created", {"content": _make_content("objective")}),
            _event("e2", 2, "context.created", {"content": _make_content("constraint")}),
        ]
        events_b = [
            _event("e2", 1, "context.created", {"content": _make_content("constraint")}),
            _event("e1", 2, "context.created", {"content": _make_content("objective")}),
        ]
        state_a = reduce_events(events_a)
        state_b = reduce_events(events_b)
        assert state_a != state_b


# ---- Event type handling tests ----


class TestApplyEvent:
    def test_context_created(self) -> None:
        state = CanonicalState()
        event = _event(
            "e1", 1, "context.created",
            {"content": _make_content("objective", name="test-obj")},
        )
        new_state = apply_event(state, event)
        assert len(new_state.items) == 1
        assert new_state.items[0].content["name"] == "test-obj"
        assert new_state.items[0].kind == AuthoritativeKind.OBJECTIVE

    def test_context_created_empty_payload_no_item(self) -> None:
        state = CanonicalState()
        event = _event("e1", 1, "context.created", {"content": {}})
        new_state = apply_event(state, event)
        assert len(new_state.items) == 0

    def test_context_updated(self) -> None:
        state = CanonicalState(
            items=(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={"id": "item-1", "kind": "objective", "name": "old"},
                    kind=AuthoritativeKind.OBJECTIVE,
                ),
            )
        )
        event = _event(
            "e2", 2, "context.updated",
            {"target_id": "item-1", "updates": {"name": "new"}},
        )
        new_state = apply_event(state, event)
        assert new_state.items[0].content["name"] == "new"
        assert len(new_state.items[0].source_refs) == 1

    def test_context_updated_no_match(self) -> None:
        state = CanonicalState(
            items=(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={"id": "item-1", "kind": "objective", "name": "old"},
                    kind=AuthoritativeKind.OBJECTIVE,
                ),
            )
        )
        event = _event(
            "e2", 2, "context.updated",
            {"target_id": "item-999", "updates": {"name": "new"}},
        )
        new_state = apply_event(state, event)
        assert new_state.items[0].content["name"] == "old"

    def test_context_deleted(self) -> None:
        state = CanonicalState(
            items=(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={"id": "item-1"},
                ),
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={"id": "item-2"},
                ),
            )
        )
        event = _event("e2", 2, "context.deleted", {"target_id": "item-1"})
        new_state = apply_event(state, event)
        assert len(new_state.items) == 1
        assert new_state.items[0].content["id"] == "item-2"

    def test_context_conflict_creates_record(self) -> None:
        state = CanonicalState()
        event = _event(
            "e1", 1, "context.conflict",
            {
                "conflict_id": "c1",
                "field": "priority",
                "values": ["high", "low"],
            },
        )
        new_state = apply_event(state, event)
        assert len(new_state.items) == 1
        assert new_state.items[0].kind == AuthoritativeKind.CONFLICT

    def test_opaque_terminal_removes_all_opaque(self) -> None:
        state = CanonicalState(
            items=(
                ContextItem(
                    category=ContextCategory.OPAQUE,
                    content={"id": "o1", "hidden_reasoning": "thinking..."},
                ),
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={"id": "a1", "kind": "objective"},
                    kind=AuthoritativeKind.OBJECTIVE,
                ),
                ContextItem(
                    category=ContextCategory.OPAQUE,
                    content={"id": "o2", "hidden_reasoning": "more thinking..."},
                ),
            )
        )
        event = _event("e1", 1, "context.opaque.terminal", {})
        new_state = apply_event(state, event)
        assert len(new_state.items) == 1
        assert new_state.items[0].content["id"] == "a1"

    def test_opaque_terminal_removes_specific_by_id(self) -> None:
        state = CanonicalState(
            items=(
                ContextItem(
                    category=ContextCategory.OPAQUE,
                    content={"id": "o1", "hidden_reasoning": "thinking..."},
                ),
                ContextItem(
                    category=ContextCategory.OPAQUE,
                    content={"id": "o2", "hidden_reasoning": "more thinking..."},
                ),
            )
        )
        event = _event("e1", 1, "context.opaque.terminal", {"target_id": "o1"})
        new_state = apply_event(state, event)
        assert len(new_state.items) == 1
        assert new_state.items[0].content["id"] == "o2"


# ---- Entity/Approval/Evidence upsert tests ----


class TestUpsertEvents:
    def _make_state_with_entity(self) -> CanonicalState:
        return CanonicalState(
            items=(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={"id": "ent-1", "kind": "entity", "name": "Alice"},
                    kind=AuthoritativeKind.ENTITY,
                ),
            )
        )

    def test_entity_update_upserts_existing(self) -> None:
        state = self._make_state_with_entity()
        event = _event(
            "e1", 1, "context.entity.update",
            {"entity": {"id": "ent-1", "name": "Alice Smith"}},
        )
        new_state = apply_event(state, event)
        assert len(new_state.items) == 1
        assert new_state.items[0].content["name"] == "Alice Smith"

    def test_entity_update_inserts_new(self) -> None:
        state = self._make_state_with_entity()
        event = _event(
            "e1", 1, "context.entity.update",
            {"entity": {"id": "ent-2", "name": "Bob"}},
        )
        new_state = apply_event(state, event)
        assert len(new_state.items) == 2

    def _make_state_with_approval(self) -> CanonicalState:
        return CanonicalState(
            items=(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={"id": "ap-1", "kind": "approval", "status": "pending"},
                    kind=AuthoritativeKind.APPROVAL,
                ),
            )
        )

    def test_approval_update_upserts_existing(self) -> None:
        state = self._make_state_with_approval()
        event = _event(
            "e1", 1, "context.approval.update",
            {"approval": {"id": "ap-1", "status": "approved"}},
        )
        new_state = apply_event(state, event)
        assert len(new_state.items) == 1
        assert new_state.items[0].content["status"] == "approved"

    def test_approval_update_inserts_new(self) -> None:
        state = self._make_state_with_approval()
        event = _event(
            "e1", 1, "context.approval.update",
            {"approval": {"id": "ap-2", "status": "pending"}},
        )
        new_state = apply_event(state, event)
        assert len(new_state.items) == 2

    def _make_state_with_evidence(self) -> CanonicalState:
        return CanonicalState(
            items=(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={"id": "ev-1", "kind": "evidence", "claim": "x = 1"},
                    kind=AuthoritativeKind.EVIDENCE,
                ),
            )
        )

    def test_evidence_update_upserts_existing(self) -> None:
        state = self._make_state_with_evidence()
        event = _event(
            "e1", 1, "context.evidence.update",
            {"evidence": {"id": "ev-1", "claim": "x = 2"}},
        )
        new_state = apply_event(state, event)
        assert len(new_state.items) == 1
        assert new_state.items[0].content["claim"] == "x = 2"

    def test_evidence_update_inserts_new(self) -> None:
        state = self._make_state_with_evidence()
        event = _event(
            "e1", 1, "context.evidence.update",
            {"evidence": {"id": "ev-2", "claim": "y = 3"}},
        )
        new_state = apply_event(state, event)
        assert len(new_state.items) == 2


# ---- Merge strategy tests (ADR-005) ----


class TestMergeStrategies:
    def test_append_merge_orders_by_task_attempt(self) -> None:
        events = [
            _event(
                "e1", 1, "context.merge.append",
                {"task_id": "t2", "attempt_no": 2, "entries": [{"value": "b"}]},
            ),
            _event(
                "e2", 2, "context.merge.append",
                {"task_id": "t1", "attempt_no": 1, "entries": [{"value": "a"}]},
            ),
            _event(
                "e3", 3, "context.merge.append",
                {"task_id": "t1", "attempt_no": 2, "entries": [{"value": "c"}]},
            ),
        ]
        state = reduce_events(events)
        assert len(state.items) == 3
        values = [item.content["value"] for item in state.items]
        assert values == ["a", "c", "b"]

    def test_lww_merge_replaces_value(self) -> None:
        events = [
            _event(
                "e1", 1, "context.merge.last_write_wins",
                {"field": "priority", "value": "low", "task_id": "t1"},
            ),
            _event(
                "e2", 2, "context.merge.last_write_wins",
                {"field": "priority", "value": "high", "task_id": "t1"},
            ),
        ]
        state = reduce_events(events)
        assert len(state.items) == 1
        assert state.items[0].content["value"] == "high"

    def test_conflict_preserving_creates_record(self) -> None:
        events = [
            _event(
                "e1", 1, "context.merge.conflict_preserving",
                {
                    "conflict_id": "c1",
                    "field": "priority",
                    "values": ["high", "low"],
                    "task_id": "t1",
                },
            ),
        ]
        state = reduce_events(events)
        assert len(state.items) == 1
        assert state.items[0].kind == AuthoritativeKind.CONFLICT
        record = state.items[0].content["conflict_record"]
        assert record["values"] == ["high", "low"]


# ---- Property tests for event replay ----


class TestPropertyEventReplay:
    @given(
        events=st.lists(
            st.fixed_dictionaries(
                {
                    "id": st.text(min_size=1, max_size=10, alphabet="abcdef0123456789"),
                    "sequence_no": st.integers(min_value=1, max_value=100),
                    "event_type": st.sampled_from([
                        "context.created",
                        "context.deleted",
                    ]),
                    "event_digest": st.text(
                        min_size=7, max_size=20, alphabet="abcdef0123456789"
                    ),
                }
            ),
            max_size=20,
        )
    )
    @settings(max_examples=50)
    def test_replay_determinism(self, events: list[dict]) -> None:
        for _i, e in enumerate(events):
            e.setdefault("payload", {})
            if e["event_type"] == "context.created":
                e["payload"]["content"] = _make_content("objective", id=e["id"])
            elif e["event_type"] == "context.deleted":
                e["payload"]["target_id"] = e["id"]

        state_a = reduce_events(events)
        state_b = reduce_events(events)
        assert state_a == state_b

    @given(
        events=st.lists(
            st.fixed_dictionaries(
                {
                    "id": st.text(min_size=1, max_size=8, alphabet="abcdef01"),
                    "sequence_no": st.integers(min_value=1, max_value=50),
                    "event_type": st.just("context.created"),
                    "event_digest": st.text(
                        min_size=7, max_size=15, alphabet="abcdef0123456789"
                    ),
                }
            ),
            max_size=15,
        )
    )
    @settings(max_examples=50)
    def test_created_events_preserve_source_refs(self, events: list[dict]) -> None:
        for _i, e in enumerate(events):
            e["payload"] = {"content": _make_content("objective", id=e["id"])}

        state = reduce_events(events)
        for item in state.items:
            assert len(item.source_refs) >= 1

    @given(
        events=st.lists(
            st.fixed_dictionaries(
                {
                    "id": st.text(min_size=1, max_size=8, alphabet="abcdef01"),
                    "sequence_no": st.integers(min_value=1, max_value=50),
                    "event_type": st.just("context.opaque.terminal"),
                    "event_digest": st.text(
                        min_size=7, max_size=15, alphabet="abcdef0123456789"
                    ),
                }
            ),
            max_size=10,
        )
    )
    @settings(max_examples=50)
    def test_opaque_terminal_removes_opaque(self, events: list[dict]) -> None:
        for e in events:
            e["payload"] = {}

        state = reduce_events(events)
        assert len(state.opaque_items()) == 0


# ---- Hidden reasoning sentinel scan ----


class TestHiddenReasoningSentinel:
    SENTINEL = "HIDDEN_REASONING_SENTINEL_abc123"

    def test_sentinel_not_in_authoritative_items(self) -> None:
        event = _event(
            "e1", 1, "context.created",
            {"content": {"id": "a1", "kind": "objective", "name": "test"}},
        )
        state = reduce_events([event])
        for item in state.authoritative_items():
            assert self.SENTINEL not in str(item.content)

    def test_sentinel_not_in_any_persisted_state(self) -> None:
        """Sentinel must not survive terminal opaque deletion.

        Per S3 spec: opaque hidden reasoning body is destroyed after terminal state;
        no Event/Memory/Evidence/TransitionManifest persists it.
        """
        events = [
            _event(
                "e1", 1, "context.created",
                {"content": {"id": "o1", "hidden_reasoning": self.SENTINEL}},
            ),
            _event("e2", 2, "context.opaque.terminal", {}),
        ]
        state = reduce_events(events)
        for item in state.items:
            assert self.SENTINEL not in str(item.content)

    def test_opaque_terminal_destroys_sentinel(self) -> None:
        events = [
            _event(
                "e1", 1, "context.created",
                {"content": {"id": "o1", "hidden_reasoning": self.SENTINEL}},
            ),
            _event("e2", 2, "context.opaque.terminal", {"target_id": "o1"}),
        ]
        state = reduce_events(events)
        assert len(state.items) == 0
        assert self.SENTINEL not in str(state.items)


# ---- State filtering tests ----


class TestStateFiltering:
    def _mixed_state(self) -> CanonicalState:
        events = [
            _event("e1", 1, "context.created", {"content": _make_content("objective")}),
            _event(
                "e2", 2, "context.created",
                {"content": {"summary": "chat summary", "source_event_ids": ["e0"]}},
            ),
            _event(
                "e3", 3, "context.created",
                {"content": {"artifact_ref": "s3://bucket/doc.pdf"}},
            ),
            _event(
                "e4", 4, "context.created",
                {"content": {"hidden_reasoning": "internal thinking"}},
            ),
        ]
        return reduce_events(events)

    def test_authoritative_filter(self) -> None:
        state = self._mixed_state()
        auth = state.authoritative_items()
        assert all(i.category == ContextCategory.AUTHORITATIVE for i in auth)

    def test_conversational_filter(self) -> None:
        state = self._mixed_state()
        conv = state.conversational_items()
        assert all(i.category == ContextCategory.CONVERSATIONAL for i in conv)

    def test_recoverable_filter(self) -> None:
        state = self._mixed_state()
        rec = state.recoverable_items()
        assert all(i.category == ContextCategory.RECOVERABLE for i in rec)

    def test_opaque_filter(self) -> None:
        state = self._mixed_state()
        opq = state.opaque_items()
        assert all(i.category == ContextCategory.OPAQUE for i in opq)

    def test_mixed_state_has_all_categories(self) -> None:
        state = self._mixed_state()
        assert len(state.authoritative_items()) >= 1
        assert len(state.conversational_items()) >= 1
        assert len(state.recoverable_items()) >= 1
        assert len(state.opaque_items()) >= 1
