"""S0-T4 RED: canonical event digest chain and deterministic projection reducer."""

from __future__ import annotations

from uuid import UUID

import pytest

from zhiwei.persistence.events import (
    CanonicalEventData,
    EventChainError,
    EventCommand,
    build_event_digest,
    reduce_projection,
    verify_event_chain,
)

ORGANIZATION_ID = UUID("11111111-1111-4111-8111-111111111111")
WORKSPACE_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")


def _event(
    sequence_no: int,
    *,
    previous_event_digest: str | None,
    payload: dict[str, object],
) -> CanonicalEventData:
    command = EventCommand(
        run_id=RUN_ID,
        event_type="run.note.added",
        payload_schema_version=1,
        payload=payload,
        actor_ref="test:operator",
        idempotency_key=f"note-{sequence_no}",
    )
    event_digest = build_event_digest(
        organization_id=ORGANIZATION_ID,
        workspace_id=WORKSPACE_ID,
        command=command,
        sequence_no=sequence_no,
        previous_event_digest=previous_event_digest,
    )
    return CanonicalEventData(
        organization_id=ORGANIZATION_ID,
        workspace_id=WORKSPACE_ID,
        sequence_no=sequence_no,
        previous_event_digest=previous_event_digest,
        event_digest=event_digest,
        **command.model_dump(),
    )


def test_event_digest_is_canonical_and_covers_chain_identity() -> None:
    first = _event(1, previous_event_digest=None, payload={"b": 2, "a": "é"})
    equivalent = _event(1, previous_event_digest=None, payload={"a": "e\u0301", "b": 2})
    next_event = _event(2, previous_event_digest=first.event_digest, payload={"a": "é", "b": 2})

    assert first.event_digest == equivalent.event_digest
    assert next_event.event_digest != first.event_digest


def test_event_models_reject_unknown_fields_and_invalid_versions() -> None:
    with pytest.raises(ValueError):
        EventCommand(
            run_id=RUN_ID,
            event_type="run.note.added",
            payload_schema_version=0,
            payload={},
            actor_ref="test:operator",
            idempotency_key="bad-version",
        )
    with pytest.raises(ValueError):
        EventCommand.model_validate(
            {
                "run_id": RUN_ID,
                "event_type": "run.note.added",
                "payload_schema_version": 1,
                "payload": {},
                "actor_ref": "test:operator",
                "idempotency_key": "unknown-field",
                "unexpected": True,
            }
        )


def test_verify_chain_and_projection_rebuild_are_deterministic() -> None:
    first = _event(1, previous_event_digest=None, payload={"note": "first"})
    second = _event(
        2,
        previous_event_digest=first.event_digest,
        payload={"note": "second"},
    )

    verify_event_chain([first, second])
    rebuilt = reduce_projection({}, first)
    rebuilt = reduce_projection(rebuilt, second)

    assert rebuilt == {
        "events": [
            {
                "event_digest": first.event_digest,
                "event_type": "run.note.added",
                "payload": {"note": "first"},
                "payload_schema_version": 1,
                "sequence_no": 1,
            },
            {
                "event_digest": second.event_digest,
                "event_type": "run.note.added",
                "payload": {"note": "second"},
                "payload_schema_version": 1,
                "sequence_no": 2,
            },
        ]
    }


def test_chain_verification_rejects_gaps_and_digest_tampering() -> None:
    first = _event(1, previous_event_digest=None, payload={"note": "first"})
    gap = _event(3, previous_event_digest=first.event_digest, payload={"note": "gap"})
    with pytest.raises(EventChainError, match="sequence"):
        verify_event_chain([first, gap])

    tampered = first.model_copy(update={"payload": {"note": "changed"}})
    with pytest.raises(EventChainError, match="digest"):
        verify_event_chain([tampered])


def test_chain_verification_rejects_cross_run_splicing() -> None:
    first = _event(1, previous_event_digest=None, payload={"note": "first"})
    other_run = UUID("44444444-4444-4444-8444-444444444444")
    command = EventCommand(
        run_id=other_run,
        event_type="run.note.added",
        payload_schema_version=1,
        payload={"note": "spliced"},
        actor_ref="test:operator",
        idempotency_key="spliced",
    )
    second = CanonicalEventData(
        organization_id=ORGANIZATION_ID,
        workspace_id=WORKSPACE_ID,
        sequence_no=2,
        previous_event_digest=first.event_digest,
        event_digest=build_event_digest(
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            command=command,
            sequence_no=2,
            previous_event_digest=first.event_digest,
        ),
        **command.model_dump(),
    )

    with pytest.raises(EventChainError, match="scope"):
        verify_event_chain([first, second])
