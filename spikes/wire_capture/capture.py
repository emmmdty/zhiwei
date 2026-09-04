"""spike-01 原型：在 httpx transport 层捕获最终序列化 request body。

对应 ADR-001 方案 C。这里只验证捕获点本身是否成立，不含 Context IR / inventory / 策略门禁——
那些是 S3-T5 的实现内容。

设计要点见 `README.md`。核心一条：**wire 的真相是 `request.stream`，不是 `request.content`**
（httpx `_transports/default.py` 把 `content=request.stream` 交给 httpcore），所以捕获后必须把
stream 重新钉在我们 hash 过的那份 bytes 上，否则 digest 与实际发送可以分叉。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field

import httpx2 as httpx

# 不进 manifest、也不落盘的请求头。凭据与会随重试变化的遥测头分开列，便于在证据里区分二者。
_SECRET_HEADERS = frozenset({"authorization", "api-key", "x-api-key", "cookie", "proxy-authorization"})


class PreSendRejected(Exception):
    """pre-send gate 拒绝发送。抛出即代表请求从未离开进程（fail closed）。"""


class PinnedBody(httpx.AsyncByteStream, httpx.SyncByteStream):
    """把 request body 钉成一份不可替换的 bytes。

    只用 httpx 的公开 ABC，不碰 `httpx._content.ByteStream` 这类私有符号——transport 是长期
    依赖点，私有 API 会随 httpx 小版本漂移。
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __iter__(self) -> Iterator[bytes]:
        yield self._body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._body


@dataclass(frozen=True)
class WireCapture:
    """一次实际发送的 wire 事实。S3 的 ContextManifest 会绑定 `body_sha256`。"""

    seq: int
    method: str
    url: str
    body_sha256: str
    body_len: int
    content_length_header: int | None
    header_names: list[str]
    redacted_headers: dict[str, str]
    stream_flag_in_body: bool | None
    retry_count_header: str | None

    @property
    def content_length_consistent(self) -> bool:
        """Content-Length 与实际 hash 的字节数是否一致——截断类篡改的第一道交叉校验。"""
        return self.content_length_header is None or self.content_length_header == self.body_len


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _stream_flag(body: bytes) -> bool | None:
    """从已序列化的 body 里读回 `stream` 标志。

    刻意从 bytes 反解而不是从调用参数取——要证明的正是「捕到的就是发出去的那份」。
    """
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed.get("stream") if isinstance(parsed, dict) else None


def _redact(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: "<redacted>" if name.lower() in _SECRET_HEADERS else value
        for name, value in headers.items()
    }


@dataclass
class CaptureTransport(httpx.AsyncBaseTransport):
    """包住真实 transport 的捕获层。

    顺序是硬约束：materialize → pin → digest → gate → 放行。gate 抛异常时 `inner` 从未被调用，
    所以「未通过门禁的请求已经发出去了」在结构上不可能发生。
    """

    inner: httpx.AsyncBaseTransport
    gate: Callable[[WireCapture, bytes], None] | None = None
    captures: list[WireCapture] = field(default_factory=list)
    # spike 用：留一份原始 bytes 以便证据里展开 body 结构。真实实现走 ObjectStore + 脱敏，
    # 不在内存里留全量 body。
    keep_bodies: bool = False
    bodies: list[bytes] = field(default_factory=list)
    # 仅 spike 用：模拟「捕获之后、发送之前」有人换掉 body。真实实现里 capture 是最后一层，
    # 这个钩子不存在。
    _post_capture_hook: Callable[[httpx.Request, bytes], None] | None = None
    pin_body: bool = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        if self.pin_body:
            request.stream = PinnedBody(body)

        capture = WireCapture(
            seq=len(self.captures),
            method=request.method,
            url=str(request.url),
            body_sha256=_digest(body),
            body_len=len(body),
            content_length_header=(
                int(request.headers["content-length"]) if "content-length" in request.headers else None
            ),
            header_names=sorted(k.lower() for k in request.headers),
            redacted_headers=_redact(request.headers),
            stream_flag_in_body=_stream_flag(body),
            retry_count_header=request.headers.get("x-stainless-retry-count"),
        )

        if self.gate is not None:
            self.gate(capture, body)  # 抛出即 fail closed

        self.captures.append(capture)
        if self.keep_bodies:
            self.bodies.append(body)

        if self._post_capture_hook is not None:
            self._post_capture_hook(request, body)

        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()

