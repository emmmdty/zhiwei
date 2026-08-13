"""spike-01 用的 loopback OpenAI 兼容 endpoint。

只绑 127.0.0.1，不出网、不读 `.env`、不需要任何凭据。它存在的唯一理由是：**给出独立于客户端的
第二份 digest**。只看 `request.content` 只能证明「httpx 对象里有这些字节」，要证明「socket 上流过
的就是这些字节」，必须由接收端再 hash 一次。

支持三件事：非流式 JSON 响应、SSE 分块流式响应（chunked transfer-encoding）、按脚本返回状态码
（用于触发 SDK 内部重试）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType

_SECRET_HEADERS = frozenset({"authorization", "api-key", "x-api-key", "cookie", "proxy-authorization"})
_READ_CHUNK = 1 << 20


@dataclass(frozen=True)
class Receipt:
    """服务端侧记录的一次实际接收。"""

    seq: int
    method: str
    path: str
    body_sha256: str
    body_len: int
    content_length_header: int | None
    transfer_encoding: str | None
    redacted_headers: dict[str, str]
    retry_count_header: str | None
    status_returned: int
    streamed: bool


@dataclass
class EndpointConfig:
    """按序消费的响应脚本。`status_plan` 空了就一直返回 200。"""

    status_plan: deque[int] = field(default_factory=deque)
    stream_chunks: int = 4
    stream_chunk_delay_s: float = 0.05


class MockEndpoint:
    def __init__(self, config: EndpointConfig | None = None) -> None:
        self.config = config or EndpointConfig()
        self.receipts: list[Receipt] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}/v1"

    def next_status(self) -> int:
        with self._lock:
            return self.config.status_plan.popleft() if self.config.status_plan else 200

    def record(self, receipt: Receipt) -> None:
        with self._lock:
            self.receipts.append(receipt)

    def next_seq(self) -> int:
        with self._lock:
            return len(self.receipts)

    def reset(self, config: EndpointConfig | None = None) -> None:
        with self._lock:
            self.receipts.clear()
            if config is not None:
                self.config = config

    def __enter__(self) -> MockEndpoint:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _make_handler(endpoint: MockEndpoint) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            raw_len_header = self.headers.get("Content-Length")
            content_length = int(raw_len_header) if raw_len_header is not None else None
            body = self._read_body(content_length)

            parsed: object = None
            try:
                parsed = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                parsed = None
            wants_stream = bool(parsed.get("stream")) if isinstance(parsed, dict) else False

            status = endpoint.next_status()
            streamed = status == 200 and wants_stream

            endpoint.record(
                Receipt(
                    seq=endpoint.next_seq(),
                    method="POST",
                    path=self.path,
                    body_sha256="sha256:" + hashlib.sha256(body).hexdigest(),
                    body_len=len(body),
                    content_length_header=content_length,
                    transfer_encoding=self.headers.get("Transfer-Encoding"),
                    redacted_headers={
                        k: "<redacted>" if k.lower() in _SECRET_HEADERS else v
                        for k, v in self.headers.items()
                    },
                    retry_count_header=self.headers.get("x-stainless-retry-count"),
                    status_returned=status,
                    streamed=streamed,
                )
            )

            if status != 200:
                self._send_json(status, {"error": {"message": "spike forced status", "type": "spike"}})
            elif wants_stream:
                self._send_sse()
            else:
                self._send_json(200, _completion_body())

        def _read_body(self, content_length: int | None) -> bytes:
            if not content_length:
                return b""
            buf = bytearray()
            remaining = content_length
            while remaining > 0:
                chunk = self.rfile.read(min(_READ_CHUNK, remaining))
                if not chunk:
                    break
                buf.extend(chunk)
                remaining -= len(chunk)
            return bytes(buf)

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()

        def _send_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(endpoint.config.stream_chunks):
                self._write_chunk(f"data: {json.dumps(_delta_body(i))}\n\n".encode())
                time.sleep(endpoint.config.stream_chunk_delay_s)
            self._write_chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def _write_chunk(self, data: bytes) -> None:
            self.wfile.write(f"{len(data):X}\r\n".encode())
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

    return Handler


def _completion_body() -> dict[str, object]:
    return {
        "id": "chatcmpl-spike",
        "object": "chat.completion",
        "created": 0,
        "model": "spike-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _delta_body(i: int) -> dict[str, object]:
    return {
        "id": "chatcmpl-spike",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "spike-model",
        "choices": [{"index": 0, "delta": {"content": f"d{i}"}, "finish_reason": None}],
    }

