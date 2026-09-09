"""S0-T5 RED: POSIX object store port conformance."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.object_store.ports import (
    ImmutableObjectConflict,
    InvalidObjectKey,
    ObjectNamespace,
    ObjectNotFound,
)
from zhiwei.object_store.posix import PosixObjectStore

ORGANIZATION_ID = UUID("11111111-1111-4111-8111-111111111111")
WORKSPACE_ID = UUID("22222222-2222-4222-8222-222222222222")
NAMESPACE = ObjectNamespace(
    organization_id=ORGANIZATION_ID, workspace_id=WORKSPACE_ID
)


def test_temporary_write_read_promote_and_immutable_reuse(tmp_path: Path) -> None:
    store = PosixObjectStore(tmp_path)
    temporary = store.write_temporary(NAMESPACE, [b"hello", b" world"])
    metadata = store.stat_temporary(NAMESPACE, temporary)
    expected_digest = digest_bytes(b"hello world")

    assert b"".join(store.read_temporary(NAMESPACE, temporary)) == b"hello world"
    assert metadata.size_bytes == len(b"hello world")

    object_key = store.immutable_key(NAMESPACE, expected_digest)
    promoted = store.promote_temporary(
        NAMESPACE,
        temporary,
        object_key,
        expected_digest=expected_digest,
        expected_size=len(b"hello world"),
    )
    assert promoted.created is True
    assert b"".join(store.read_immutable(NAMESPACE, object_key)) == b"hello world"
    assert not store.temporary_exists(NAMESPACE, temporary)

    duplicate = store.write_temporary(NAMESPACE, [b"hello world"])
    reused = store.promote_temporary(
        NAMESPACE,
        duplicate,
        object_key,
        expected_digest=expected_digest,
        expected_size=len(b"hello world"),
    )
    assert reused.created is False
    assert not store.temporary_exists(NAMESPACE, duplicate)


def test_promote_rejects_digest_mismatch_and_immutable_collision(tmp_path: Path) -> None:
    store = PosixObjectStore(tmp_path)
    temporary = store.write_temporary(NAMESPACE, [b"actual"])
    expected_digest = digest_bytes(b"expected")
    object_key = store.immutable_key(NAMESPACE, expected_digest)

    with pytest.raises(ImmutableObjectConflict, match="digest"):
        store.promote_temporary(
            NAMESPACE,
            temporary,
            object_key,
            expected_digest=expected_digest,
            expected_size=len(b"expected"),
        )

    valid = store.write_temporary(NAMESPACE, [b"expected"])
    store.promote_temporary(
        NAMESPACE,
        valid,
        object_key,
        expected_digest=expected_digest,
        expected_size=len(b"expected"),
    )
    store.debug_replace_immutable(NAMESPACE, object_key, b"tampered")
    collision = store.write_temporary(NAMESPACE, [b"expected"])
    with pytest.raises(ImmutableObjectConflict, match="immutable"):
        store.promote_temporary(
            NAMESPACE,
            collision,
            object_key,
            expected_digest=expected_digest,
            expected_size=len(b"expected"),
        )


def test_keys_are_opaque_tenant_scoped_and_cannot_escape_root(tmp_path: Path) -> None:
    store = PosixObjectStore(tmp_path)
    other_namespace = ObjectNamespace(
        organization_id=UUID("33333333-3333-4333-8333-333333333333"),
        workspace_id=UUID("44444444-4444-4444-8444-444444444444"),
    )
    digest = digest_bytes(b"tenant object")
    object_key = store.immutable_key(NAMESPACE, digest)
    temporary = store.write_temporary(NAMESPACE, [b"tenant object"])
    store.promote_temporary(
        NAMESPACE,
        temporary,
        object_key,
        expected_digest=digest,
        expected_size=len(b"tenant object"),
    )

    with pytest.raises(ObjectNotFound):
        tuple(store.read_immutable(other_namespace, object_key))
    for invalid in ("../escape", "/absolute", "org/user-input", "", "."):
        with pytest.raises(InvalidObjectKey):
            tuple(store.read_immutable(NAMESPACE, invalid))
    assert not (tmp_path.parent / "escape").exists()


def test_orphan_listing_respects_cutoff_and_delete_is_scoped(tmp_path: Path) -> None:
    store = PosixObjectStore(tmp_path)
    old_digest = digest_bytes(b"old")
    recent_digest = digest_bytes(b"recent")
    old_key = store.immutable_key(NAMESPACE, old_digest)
    recent_key = store.immutable_key(NAMESPACE, recent_digest)
    for key, content, digest in (
        (old_key, b"old", old_digest),
        (recent_key, b"recent", recent_digest),
    ):
        temporary = store.write_temporary(NAMESPACE, [content])
        store.promote_temporary(
            NAMESPACE,
            temporary,
            key,
            expected_digest=digest,
            expected_size=len(content),
        )
    now = datetime.now(UTC)
    store.debug_set_mtime(NAMESPACE, old_key, now - timedelta(hours=2))

    candidates = store.list_immutable_before(NAMESPACE, now - timedelta(hours=1))
    assert [candidate.key for candidate in candidates] == [old_key]
    store.delete_immutable(NAMESPACE, old_key)
    with pytest.raises(ObjectNotFound):
        tuple(store.read_immutable(NAMESPACE, old_key))
    assert b"".join(store.read_immutable(NAMESPACE, recent_key)) == b"recent"


def test_promotion_time_starts_the_immutable_orphan_window(tmp_path: Path) -> None:
    store = PosixObjectStore(tmp_path)
    content = b"promoted after cutoff"
    digest = digest_bytes(content)
    temporary = store.write_temporary(NAMESPACE, [content])
    cutoff = datetime.now(UTC)

    object_key = store.immutable_key(NAMESPACE, digest)
    store.promote_temporary(
        NAMESPACE,
        temporary,
        object_key,
        expected_digest=digest,
        expected_size=len(content),
    )

    assert store.list_immutable_before(NAMESPACE, cutoff) == []


def test_completed_temporary_objects_can_be_listed_and_deleted(tmp_path: Path) -> None:
    store = PosixObjectStore(tmp_path)
    temporary = store.write_temporary(NAMESPACE, [b"abandoned"])

    candidates = store.list_temporary_before(NAMESPACE, datetime.now(UTC) + timedelta(seconds=1))
    assert [candidate.key for candidate in candidates] == [temporary]
    store.delete_temporary(NAMESPACE, temporary)
    with pytest.raises(ObjectNotFound):
        tuple(store.read_temporary(NAMESPACE, temporary))


def test_partial_temporary_write_is_removed_when_stream_fails(tmp_path: Path) -> None:
    store = PosixObjectStore(tmp_path)

    def failing_chunks() -> Iterator[bytes]:
        yield b"partial"
        raise RuntimeError("upload interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        store.write_temporary(NAMESPACE, failing_chunks())

    assert list(tmp_path.rglob("*-tmp-*")) == []
