"""POSIX test adapter for tenant-scoped content-addressed objects."""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from zhiwei.contracts.time import ensure_utc
from zhiwei.object_store.ports import (
    ImmutableObjectConflict,
    ImmutableObjectMetadata,
    InvalidObjectKey,
    ObjectMetadata,
    ObjectNamespace,
    ObjectNotFound,
    PromotionResult,
)

_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{0,254}")
_DIGEST_PATTERN = re.compile(r"sha256:([0-9a-f]{64})")
_IMMUTABLE_KEY_PATTERN = re.compile(r"ws-[0-9a-f]{32}-sha256-([0-9a-f]{64})")
_CHUNK_SIZE = 1024 * 1024


class PosixObjectStore:
    """Filesystem adapter whose public keys never expose filesystem paths."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def write_temporary(self, namespace: ObjectNamespace, chunks: Iterable[bytes]) -> str:
        key = f"ws-{namespace.workspace_id.hex}-tmp-{uuid4().hex}"
        path = self._temporary_path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("object chunks must be bytes")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_directory(path.parent)
        except BaseException:
            path.unlink(missing_ok=True)
            self._fsync_directory(path.parent)
            raise
        return key

    def read_temporary(self, namespace: ObjectNamespace, key: str) -> Iterator[bytes]:
        return self._read(self._temporary_path(namespace, key), key)

    def stat_temporary(self, namespace: ObjectNamespace, key: str) -> ObjectMetadata:
        return self._stat(self._temporary_path(namespace, key), key)

    def temporary_exists(self, namespace: ObjectNamespace, key: str) -> bool:
        return self._temporary_path(namespace, key).is_file()

    def list_temporary_before(
        self, namespace: ObjectNamespace, cutoff: datetime
    ) -> list[ObjectMetadata]:
        cutoff_utc = ensure_utc(cutoff)
        directory = self._tenant_root(namespace) / "temporary"
        if not directory.exists():
            return []
        candidates: list[ObjectMetadata] = []
        for path in directory.iterdir():
            try:
                metadata = self._stat(path, path.name)
            except ObjectNotFound:
                continue
            if metadata.modified_at <= cutoff_utc:
                candidates.append(metadata)
        return sorted(candidates, key=lambda item: item.key)

    def delete_temporary(self, namespace: ObjectNamespace, key: str) -> None:
        path = self._temporary_path(namespace, key)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ObjectNotFound(f"temporary object not found: {key}") from exc
        self._fsync_directory(path.parent)

    def immutable_key(self, namespace: ObjectNamespace, content_digest: str) -> str:
        self._namespace_components(namespace)
        match = _DIGEST_PATTERN.fullmatch(content_digest)
        if match is None:
            raise InvalidObjectKey("content digest must be lowercase SHA-256")
        return f"ws-{namespace.workspace_id.hex}-sha256-{match.group(1)}"

    def promote_temporary(
        self,
        namespace: ObjectNamespace,
        temporary_key: str,
        immutable_key: str,
        *,
        expected_digest: str,
        expected_size: int,
    ) -> PromotionResult:
        source = self._temporary_path(namespace, temporary_key)
        if not source.is_file():
            raise ObjectNotFound(f"temporary object not found: {temporary_key}")
        expected_key = self.immutable_key(namespace, expected_digest)
        if immutable_key != expected_key:
            raise ImmutableObjectConflict("immutable key does not match expected digest")
        source_digest, source_size = self._digest_path(source)
        if source_digest != expected_digest or source_size != expected_size:
            raise ImmutableObjectConflict("temporary object digest or size mismatch")

        target = self._immutable_path(namespace, immutable_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        created = True
        try:
            os.link(source, target)
            promotion_time = time.time_ns()
            os.utime(target, ns=(promotion_time, promotion_time))
        except FileExistsError:
            created = False
            target_digest, target_size = self._digest_path(target)
            if target_digest != expected_digest or target_size != expected_size:
                raise ImmutableObjectConflict(
                    "existing immutable object does not match digest"
                ) from None
        source.unlink()
        self._fsync_directory(target.parent)
        return PromotionResult(
            key=immutable_key,
            created=created,
            size_bytes=expected_size,
            content_digest=expected_digest,
        )

    def read_immutable(self, namespace: ObjectNamespace, key: str) -> Iterator[bytes]:
        return self._read(self._immutable_path(namespace, key), key)

    def stat_immutable(
        self, namespace: ObjectNamespace, key: str
    ) -> ImmutableObjectMetadata:
        return self._immutable_metadata(self._immutable_path(namespace, key), key)

    def list_immutable_before(
        self, namespace: ObjectNamespace, cutoff: datetime
    ) -> list[ImmutableObjectMetadata]:
        cutoff_utc = ensure_utc(cutoff)
        directory = self._tenant_root(namespace) / "objects"
        if not directory.exists():
            return []
        candidates: list[ImmutableObjectMetadata] = []
        for path in directory.iterdir():
            try:
                metadata = self._immutable_metadata(path, path.name)
            except ObjectNotFound:
                continue
            if metadata.modified_at <= cutoff_utc:
                candidates.append(metadata)
        return sorted(candidates, key=lambda item: item.key)

    def delete_immutable(self, namespace: ObjectNamespace, key: str) -> None:
        path = self._immutable_path(namespace, key)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ObjectNotFound(f"immutable object not found: {key}") from exc
        self._fsync_directory(path.parent)

    def debug_replace_immutable(
        self, namespace: ObjectNamespace, key: str, content: bytes
    ) -> None:
        """Corrupt a test object without weakening the production port."""
        path = self._immutable_path(namespace, key)
        if not path.is_file():
            raise ObjectNotFound(f"immutable object not found: {key}")
        path.write_bytes(content)

    def debug_set_mtime(
        self, namespace: ObjectNamespace, key: str, value: datetime
    ) -> None:
        """Set a test object's mtime for deterministic orphan reconciliation tests."""
        timestamp = ensure_utc(value).timestamp()
        path = self._immutable_path(namespace, key)
        if not path.is_file():
            raise ObjectNotFound(f"immutable object not found: {key}")
        os.utime(path, times=(timestamp, timestamp))

    def _temporary_path(self, namespace: ObjectNamespace, key: str) -> Path:
        prefix = f"ws-{namespace.workspace_id.hex}-tmp-"
        self._validate_namespace_key(key, prefix=prefix)
        return self._tenant_root(namespace) / "temporary" / key

    def _immutable_path(self, namespace: ObjectNamespace, key: str) -> Path:
        prefix = f"ws-{namespace.workspace_id.hex}-sha256-"
        self._validate_namespace_key(key, prefix=prefix)
        if _IMMUTABLE_KEY_PATTERN.fullmatch(key) is None:
            raise InvalidObjectKey("immutable key must be a SHA-256 content key")
        return self._tenant_root(namespace) / "objects" / key

    def _tenant_root(self, namespace: ObjectNamespace) -> Path:
        organization, workspace = self._namespace_components(namespace)
        return self._root / organization / workspace

    @staticmethod
    def _namespace_components(namespace: ObjectNamespace) -> tuple[str, str]:
        if not isinstance(namespace, ObjectNamespace):
            raise InvalidObjectKey("object namespace must include organization and workspace UUIDs")
        return f"org-{namespace.organization_id.hex}", f"ws-{namespace.workspace_id.hex}"

    @staticmethod
    def _validate_namespace_key(key: str, *, prefix: str) -> None:
        if not isinstance(key, str) or _KEY_PATTERN.fullmatch(key) is None:
            raise InvalidObjectKey("object key is not a valid opaque key")
        if not key.startswith(prefix):
            raise ObjectNotFound("object is not in the requested tenant namespace")

    @staticmethod
    def _read(path: Path, key: str) -> Iterator[bytes]:
        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise ObjectNotFound(f"object not found: {key}") from exc
        with handle:
            while chunk := handle.read(_CHUNK_SIZE):
                yield chunk

    @staticmethod
    def _stat(path: Path, key: str) -> ObjectMetadata:
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise ObjectNotFound(f"object not found: {key}") from exc
        return ObjectMetadata(
            key=key,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        )

    @staticmethod
    def _immutable_metadata(path: Path, key: str) -> ImmutableObjectMetadata:
        match = _IMMUTABLE_KEY_PATTERN.fullmatch(key)
        if match is None:
            raise InvalidObjectKey("immutable key must be a SHA-256 content key")
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise ObjectNotFound(f"object not found: {key}") from exc
        return ImmutableObjectMetadata(
            key=key,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            content_digest=f"sha256:{match.group(1)}",
        )

    @staticmethod
    def _digest_path(path: Path) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                hasher.update(chunk)
                size += len(chunk)
        return f"sha256:{hasher.hexdigest()}", size

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
