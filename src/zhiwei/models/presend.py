"""S3-T5 Pre-send gate and wire capture transport.

CaptureTransport wraps httpx.AsyncBaseTransport, captures the body at the
transport layer (ADR-001), pins the stream to the materialized bytes, computes
a digest, runs the pre-send gate, and only then forwards to the inner transport.

The ordering is a hard constraint:
    aread() → PinnedBody(stream) → compute digest → gate → persist → send

If the gate raises PreSendRejected, the inner transport is never called.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx2 as httpx

from zhiwei.models.contracts import ClassificationCeiling, EndpointProfile

_SECRET_HEADERS = frozenset({
    "authorization",
    "api-key",
    "x-api-key",
    "cookie",
    "proxy-authorization",
})


class PreSendRejected(Exception):
    """Pre-send gate rejection. Raising means the request never left the process."""


class ClassificationViolation(PreSendRejected):
    """context 实际数据分类超过 endpoint 的 classification_ceiling（ADR-011 §4）。

    继承 PreSendRejected：与其它 gate 拒绝一样，抛出即请求未离开进程；独立子类让
    Runtime 能把它与普通 provider failure（429/5xx 等）区分归类，而不是混入失败计数。
    """


class PinnedBody(httpx.AsyncByteStream, httpx.SyncByteStream):
    """Pin a request body to an immutable bytes buffer.

    Uses only public httpx ABCs, not private internals.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __iter__(self) -> Iterator[bytes]:
        yield self._body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._body


@dataclass(frozen=True)
class WireCapture:
    """Immutable fact record for one wire send.

    body_sha256 is computed from the raw bytes before any SDK mutation.
    """

    seq: int
    method: str
    url: str
    body_sha256: str
    body_len: int
    content_length_header: int | None
    header_names: tuple[str, ...]
    redacted_headers: dict[str, str]
    captured_at: str

    @property
    def content_length_consistent(self) -> bool:
        return self.content_length_header is None or self.content_length_header == self.body_len


def digest_bytes(body: bytes) -> str:
    """Return algorithm-prefixed SHA-256 digest of bytes."""
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _redact(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: "<redacted>" if name.lower() in _SECRET_HEADERS else value
        for name, value in headers.items()
    }


GateFn = Callable[[WireCapture, bytes], None]


def classification_gate(endpoint: EndpointProfile, data_classification: str) -> GateFn:
    """ADR-011 §4 的 pre-send 分类门禁：实际分类 ≤ endpoint ceiling 才放行。

    endpoint 在组装 client 时绑定（transport 是唯一出网路径），本次 Attempt 的
    context 分类由调用方闭包捕获。未知分类一律拒绝（fail closed），不取「常见默认」。
    """
    ceiling = endpoint.classification_ceiling

    def gate(capture: WireCapture, body: bytes) -> None:
        try:
            actual = ClassificationCeiling(data_classification.lower())
        except ValueError:
            raise ClassificationViolation(
                f"unknown data classification {data_classification!r} for "
                f"endpoint '{endpoint.id}'"
            ) from None
        if actual > ceiling:
            raise ClassificationViolation(
                f"context classification '{actual.value}' exceeds endpoint "
                f"'{endpoint.id}' ceiling '{ceiling.value}'"
            )

    return gate


@dataclass
class CaptureTransport(httpx.AsyncBaseTransport):
    """httpx transport wrapper that captures the wire body.

    The capture happens at the transport layer, so it sees the final
    serialized body after SDK processing but before network send.
    max_retries=0 is enforced by the caller, not by this transport.
    """

    inner: httpx.AsyncBaseTransport
    gate: GateFn | None = None
    captures: list[WireCapture] = field(default_factory=list)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        request.stream = PinnedBody(body)

        capture = WireCapture(
            seq=len(self.captures),
            method=request.method,
            url=str(request.url),
            body_sha256=digest_bytes(body),
            body_len=len(body),
            content_length_header=(
                int(request.headers["content-length"])
                if "content-length" in request.headers
                else None
            ),
            header_names=tuple(sorted(k.lower() for k in request.headers)),
            redacted_headers=_redact(request.headers),
            captured_at=datetime.now(tz=UTC).isoformat(),
        )

        if self.gate is not None:
            self.gate(capture, body)

        self.captures.append(capture)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()
