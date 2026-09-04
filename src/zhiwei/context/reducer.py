"""S3-T3 Pure reducer for context state.

Same events in same order = same state (deterministic).
Handles: append, last_write_wins, conflict_preserving merge strategies (ADR-005).
Never persists hidden reasoning body.
"""

from __future__ import annotations

from typing import Any

from zhiwei.context.state import CanonicalState, build_source_ref
from zhiwei.context.types import (
    AuthoritativeKind,
    ContextCategory,
    ContextItem,
    classify_content,
)


def reduce_events(events: list[dict[str, Any]]) -> CanonicalState:
    """Pure reducer: same events in same order = same state.

    Processes canonical events and produces a deterministic CanonicalState.
    No side effects, no I/O — purely functional.
    """
    state = CanonicalState()
    for event in events:
        state = apply_event(state, event)
    return state


def apply_event(state: CanonicalState, event: dict[str, Any]) -> CanonicalState:
    """Apply a single canonical event to produce a new state.

    The reducer is pure: same input state + event = same output state.
    """
    event_type = event.get("event_type", "")
    payload = event.get("payload", {})
    source_ref = build_source_ref(event)

    items = list(state.items)
    seq = event.get("sequence_no", state.sequence_no + 1)
    head_digest = event.get("event_digest", state.head_event_digest)

    if event_type == "context.created":
        new_item = _create_context_item(payload, source_ref)
        if new_item is not None:
            items.append(new_item)

    elif event_type == "context.updated":
        items = _apply_update(items, payload, source_ref)

    elif event_type == "context.deleted":
        items = _apply_delete(items, payload)

    elif event_type == "context.conflict":
        items = _apply_conflict(items, payload, source_ref)

    elif event_type == "context.entity.update":
        items = _apply_entity_update(items, payload, source_ref)

    elif event_type == "context.approval.update":
        items = _apply_approval_update(items, payload, source_ref)

    elif event_type == "context.evidence.update":
        items = _apply_evidence_update(items, payload, source_ref)

    elif event_type == "context.merge.append":
        items = _apply_append_merge(items, payload, source_ref)

    elif event_type == "context.merge.last_write_wins":
        items = _apply_lww_merge(items, payload, source_ref)

    elif event_type == "context.merge.conflict_preserving":
        items = _apply_conflict_preserving_merge(items, payload, source_ref)

    elif event_type == "context.opaque.terminal":
        items = _apply_opaque_terminal(items, payload)

    return CanonicalState(
        items=tuple(items),
        sequence_no=int(seq),
        head_event_digest=str(head_digest) if head_digest else None,
    )


def _create_context_item(
    payload: dict[str, Any], source_ref: Any
) -> ContextItem | None:
    """Create a new context item from a context.created event."""
    content = payload.get("content", {})
    if not content:
        return None

    category = classify_content(content)
    kind = None
    if category == ContextCategory.AUTHORITATIVE:
        raw_kind = content.get("kind")
        if raw_kind:
            try:
                kind = AuthoritativeKind(raw_kind)
            except ValueError:
                kind = None

    return ContextItem(
        category=category,
        content=content,
        source_refs=(source_ref,),
        kind=kind,
    )


def _apply_update(
    items: list[ContextItem], payload: dict[str, Any], source_ref: Any
) -> list[ContextItem]:
    """Apply a context.updated event."""
    target_id = payload.get("target_id", "")
    updates = payload.get("updates", {})
    result = []
    for item in items:
        if item.content.get("id") == target_id:
            new_content = {**item.content, **updates}
            result.append(
                ContextItem(
                    category=item.category,
                    content=new_content,
                    source_refs=(*item.source_refs, source_ref),
                    kind=item.kind,
                )
            )
        else:
            result.append(item)
    return result


def _apply_delete(items: list[ContextItem], payload: dict[str, Any]) -> list[ContextItem]:
    """Apply a context.deleted event — removes the target item."""
    target_id = payload.get("target_id", "")
    return [item for item in items if item.content.get("id") != target_id]


def _apply_conflict(
    items: list[ContextItem], payload: dict[str, Any], source_ref: Any
) -> list[ContextItem]:
    """Apply a context.conflict event — records a ConflictRecord."""
    conflict_record = {
        "id": payload.get("conflict_id", ""),
        "field": payload.get("field", ""),
        "values": payload.get("values", []),
        "source_refs": [source_ref],
    }
    items.append(
        ContextItem(
            category=ContextCategory.AUTHORITATIVE,
            content={"kind": "conflict", "conflict_record": conflict_record},
            source_refs=(source_ref,),
            kind=AuthoritativeKind.CONFLICT,
        )
    )
    return items


def _apply_entity_update(
    items: list[ContextItem], payload: dict[str, Any], source_ref: Any
) -> list[ContextItem]:
    """Apply a context.entity.update event — upserts entity by id."""
    entity = payload.get("entity", {})
    entity_id = entity.get("id", "")
    result = []
    found = False
    for item in items:
        if item.content.get("id") == entity_id and item.kind == AuthoritativeKind.ENTITY:
            result.append(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={**item.content, **entity},
                    source_refs=(*item.source_refs, source_ref),
                    kind=AuthoritativeKind.ENTITY,
                )
            )
            found = True
        else:
            result.append(item)
    if not found and entity:
        result.append(
            ContextItem(
                category=ContextCategory.AUTHORITATIVE,
                content=entity,
                source_refs=(source_ref,),
                kind=AuthoritativeKind.ENTITY,
            )
        )
    return result


def _apply_approval_update(
    items: list[ContextItem], payload: dict[str, Any], source_ref: Any
) -> list[ContextItem]:
    """Apply a context.approval.update event — upserts approval by id."""
    approval = payload.get("approval", {})
    approval_id = approval.get("id", "")
    result = []
    found = False
    for item in items:
        if item.content.get("id") == approval_id and item.kind == AuthoritativeKind.APPROVAL:
            result.append(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={**item.content, **approval},
                    source_refs=(*item.source_refs, source_ref),
                    kind=AuthoritativeKind.APPROVAL,
                )
            )
            found = True
        else:
            result.append(item)
    if not found and approval:
        result.append(
            ContextItem(
                category=ContextCategory.AUTHORITATIVE,
                content=approval,
                source_refs=(source_ref,),
                kind=AuthoritativeKind.APPROVAL,
            )
        )
    return result


def _apply_evidence_update(
    items: list[ContextItem], payload: dict[str, Any], source_ref: Any
) -> list[ContextItem]:
    """Apply a context.evidence.update event — upserts evidence by id."""
    evidence = payload.get("evidence", {})
    evidence_id = evidence.get("id", "")
    result = []
    found = False
    for item in items:
        if item.content.get("id") == evidence_id and item.kind == AuthoritativeKind.EVIDENCE:
            result.append(
                ContextItem(
                    category=ContextCategory.AUTHORITATIVE,
                    content={**item.content, **evidence},
                    source_refs=(*item.source_refs, source_ref),
                    kind=AuthoritativeKind.EVIDENCE,
                )
            )
            found = True
        else:
            result.append(item)
    if not found and evidence:
        result.append(
            ContextItem(
                category=ContextCategory.AUTHORITATIVE,
                content=evidence,
                source_refs=(source_ref,),
                kind=AuthoritativeKind.EVIDENCE,
            )
        )
    return result


def _apply_append_merge(
    items: list[ContextItem], payload: dict[str, Any], source_ref: Any
) -> list[ContextItem]:
    """ADR-005 append merge: ordered by (task_id, attempt_no)."""
    new_entries = payload.get("entries", [])
    task_id = payload.get("task_id", "")
    attempt_no = payload.get("attempt_no", 0)

    for entry in new_entries:
        content = {**entry, "_merge_task_id": task_id, "_merge_attempt_no": attempt_no}
        items.append(
            ContextItem(
                category=classify_content(content),
                content=content,
                source_refs=(source_ref,),
            )
        )

    items.sort(key=lambda it: (
        it.content.get("_merge_task_id", ""),
        it.content.get("_merge_attempt_no", 0),
    ))
    return items


def _apply_lww_merge(
    items: list[ContextItem], payload: dict[str, Any], source_ref: Any
) -> list[ContextItem]:
    """ADR-005 last_write_wins merge: ordered by task_id, latest wins."""
    field = payload.get("field", "")
    new_value = payload.get("value")
    task_id = payload.get("task_id", "")

    result = []
    replaced = False
    for item in items:
        if (
            item.content.get("field") == field
            and item.content.get("_merge_task_id") == task_id
        ):
            result.append(
                ContextItem(
                    category=item.category,
                    content={**item.content, "value": new_value},
                    source_refs=(*item.source_refs, source_ref),
                    kind=item.kind,
                )
            )
            replaced = True
        else:
            result.append(item)

    if not replaced:
        content = {"field": field, "value": new_value, "_merge_task_id": task_id}
        result.append(
            ContextItem(
                category=classify_content(content),
                content=content,
                source_refs=(source_ref,),
            )
        )
    return result


def _apply_conflict_preserving_merge(
    items: list[ContextItem], payload: dict[str, Any], source_ref: Any
) -> list[ContextItem]:
    """ADR-005 conflict_preserving merge: writes ConflictRecord, no arbitration."""
    conflict_id = payload.get("conflict_id", "")
    field = payload.get("field", "")
    values = payload.get("values", [])

    conflict_record = {
        "id": conflict_id,
        "field": field,
        "values": values,
        "task_id": payload.get("task_id", ""),
    }

    items.append(
        ContextItem(
            category=ContextCategory.AUTHORITATIVE,
            content={"kind": "conflict", "conflict_record": conflict_record},
            source_refs=(source_ref,),
            kind=AuthoritativeKind.CONFLICT,
        )
    )
    return items


def _apply_opaque_terminal(
    items: list[ContextItem], payload: dict[str, Any]
) -> list[ContextItem]:
    """Terminal opaque deletion: remove all opaque items.

    Per S3 spec: opaque hidden reasoning body only exists within current Attempt;
    after terminal state, destroy the body. No Event/Memory/Evidence/TransitionManifest.
    """
    target_id = payload.get("target_id")
    if target_id:
        return [
            item
            for item in items
            if not (
                item.category == ContextCategory.OPAQUE
                and item.content.get("id") == target_id
            )
        ]
    return [item for item in items if item.category != ContextCategory.OPAQUE]
