"""MCP transport layer: stdio and Streamable HTTP.

Provides the low-level transport abstraction for MCP JSON-RPC 2.0 communication.
Stdio transport communicates via stdin/stdout of a child process.
Streamable HTTP transport uses HTTP POST to a single endpoint with SSE streaming.

Process/session isolation key: org/workspace/ProviderVersion/Connection subject/Run.
First version forbids cross-key reuse (S4 spec §5). 隔离键必填非空（F-R2-03）：
空键不构成隔离域，构造期即拒绝。

Server notification（无 id 报文）经 on_notification 注册的分发器送达消费方
（F-R9-05）——读循环与 SSE 解析都不再静默丢弃。
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zhiwei.capabilities.inspection.network import check_url_safety

logger = logging.getLogger(__name__)

# server → host 方向的 notification 处理器：(method, params)。
NotificationHandler = Callable[[str, dict[str, Any]], None]


class TransportError(Exception):
    """Transport-level failure (connection refused, timeout, malformed stream)."""


class TransportClosedError(TransportError):
    """Transport was closed before response was received."""


class JsonRpcError(Exception):
    """JSON-RPC 2.0 error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC error {code}: {message}")


@dataclass(frozen=True)
class JsonRpcRequest:
    """JSON-RPC 2.0 request."""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": self.method,
        }
        if self.params:
            msg["params"] = self.params
        if self.id is not None:
            msg["id"] = self.id
        return msg

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_dict()).encode("utf-8")


@dataclass(frozen=True)
class JsonRpcResponse:
    """JSON-RPC 2.0 response or notification."""

    id: int | str | None = None
    result: Any = None
    error: dict[str, Any] | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JsonRpcResponse:
        if "error" in data and data["error"] is not None:
            return cls(id=data.get("id"), error=data["error"])
        return cls(id=data.get("id"), result=data.get("result"))


class McpTransport(ABC):
    """Abstract MCP transport."""

    @abstractmethod
    async def send_request(self, request: JsonRpcRequest) -> JsonRpcResponse:
        """Send a JSON-RPC request and wait for the response."""
        ...

    @abstractmethod
    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the transport and release resources."""
        ...

    def on_notification(self, handler: NotificationHandler) -> None:
        """Register a handler for server-initiated notifications."""
        self._notif_handlers.append(handler)

    def _dispatch_notification(self, method: str, params: dict[str, Any]) -> None:
        """Fan out a notification to registered handlers.

        handler 异常不中断传输（读循环存活优先），记录后继续。
        """
        for handler in list(self._notif_handlers):
            try:
                handler(method, params)
            except Exception:
                logger.exception("MCP notification handler failed for %s", method)

    @property
    def _notif_handlers(self) -> list[NotificationHandler]:
        # 惰性存储：子类 __init__ 各异且未必调用 super().__init__()。
        if not hasattr(self, "_notif_handler_store"):
            self._notif_handler_store: list[NotificationHandler] = []
        return self._notif_handler_store

    # 子类构造期经 _require_isolation_key 赋值；基类声明供共享的 check_isolation 使用。
    _isolation_key: tuple[str, ...]

    def check_isolation(self, expected_key: tuple[str, ...]) -> None:
        """Verify the transport is used only within its isolation key scope (F-R2-03)."""
        if not self._isolation_key:
            raise TransportError("Transport has no isolation key (fail closed)")
        if self._isolation_key != expected_key:
            raise TransportError(
                "Cross-key reuse forbidden: transport isolation key mismatch "
                f"(expected {expected_key}, got {self._isolation_key})"
            )

    @staticmethod
    def _require_isolation_key(isolation_key: tuple[str, ...]) -> tuple[str, ...]:
        """隔离键必填非空（F-R2-03）：空键 = 无隔离域，fail closed。"""
        if not isolation_key:
            raise TransportError(
                "isolation key is required and must be non-empty "
                "(org/workspace/ProviderVersion/subject/Run per S4 spec §5)"
            )
        return isolation_key


class StdioTransport(McpTransport):
    """MCP stdio transport: communicates via stdin/stdout of a child process.

    Isolation: each process is scoped to a unique
    (org, workspace, provider_version, connection_subject, run) tuple.
    Cross-key reuse is forbidden (S4 spec §5).
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        isolation_key: tuple[str, ...] = (),
    ) -> None:
        self._command = command
        self._args = args or []
        self._env = env
        self._isolation_key = self._require_isolation_key(isolation_key)
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int | str, asyncio.Future[JsonRpcResponse]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._request_id = 0

    @property
    def isolation_key(self) -> tuple[str, ...]:
        return self._isolation_key

    async def start(self) -> None:
        """Start the child process."""
        if self._process is not None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise TransportError(f"Command not found: {self._command}") from exc
        except OSError as exc:
            raise TransportError(f"Failed to start process: {exc}") from exc
        if self._process.stdout is not None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.process_message(data)
        except asyncio.CancelledError:
            pass

    def process_message(self, data: dict[str, Any]) -> None:
        """Route one decoded JSON-RPC message: notification → handlers, response → pending."""
        if "method" in data and data.get("id") is None:
            self._dispatch_notification(data["method"], data.get("params") or {})
            return
        resp = JsonRpcResponse.from_dict(data)
        if resp.id is not None and resp.id in self._pending:
            fut = self._pending.pop(resp.id)
            if not fut.done():
                fut.set_result(resp)

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def send_request(self, request: JsonRpcRequest) -> JsonRpcResponse:
        if self._closed:
            raise TransportClosedError("Transport is closed")
        if self._process is None or self._process.stdin is None:
            raise TransportError("Process not started")

        req_id = request.id if request.id is not None else self._next_id()
        msg = JsonRpcRequest(method=request.method, params=request.params, id=req_id)

        future: asyncio.Future[JsonRpcResponse] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            self._process.stdin.write(msg.to_bytes() + b"\n")
            await self._process.stdin.drain()
        except (BrokenPipeError, OSError) as exc:
            self._pending.pop(req_id, None)
            raise TransportError(f"Failed to write to process: {exc}") from exc

        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise TransportError("Request timed out") from None

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._closed:
            raise TransportClosedError("Transport is closed")
        if self._process is None or self._process.stdin is None:
            raise TransportError("Process not started")

        msg = JsonRpcRequest(method=method, params=params or {})
        try:
            self._process.stdin.write(msg.to_bytes() + b"\n")
            await self._process.stdin.drain()
        except (BrokenPipeError, OSError) as exc:
            raise TransportError(f"Failed to write notification: {exc}") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        import contextlib

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()
            self._process = None


class StreamableHttpTransport(McpTransport):
    """MCP Streamable HTTP transport: JSON-RPC over HTTP POST with SSE responses.

    Uses httpx for HTTP; no additional dependencies.
    """

    def __init__(
        self,
        endpoint_url: str,
        headers: dict[str, str] | None = None,
        isolation_key: tuple[str, ...] = (),
        http_client: Any | None = None,
    ) -> None:
        ssrf_report = check_url_safety(endpoint_url)
        if not ssrf_report.passed:
            blocking = [f.message for f in ssrf_report.findings if f.is_blocking()]
            raise TransportError(
                f"URL safety check failed for {endpoint_url}: {'; '.join(blocking)}"
            )
        self._endpoint_url = endpoint_url
        self._headers = headers or {}
        self._isolation_key = self._require_isolation_key(isolation_key)
        self._http_client = http_client
        self._closed = False
        self._request_id = 0

    @property
    def isolation_key(self) -> tuple[str, ...]:
        return self._isolation_key

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _get_client(self) -> Any:
        if self._http_client is not None:
            return self._http_client
        import httpx2 as httpx

        self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def send_request(self, request: JsonRpcRequest) -> JsonRpcResponse:
        if self._closed:
            raise TransportClosedError("Transport is closed")

        req_id = request.id if request.id is not None else self._next_id()
        msg = JsonRpcRequest(method=request.method, params=request.params, id=req_id)
        client = await self._get_client()

        try:
            resp = await client.post(
                self._endpoint_url,
                content=msg.to_bytes(),
                headers={**self._headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
            resp.raise_for_status()
        except Exception as exc:
            raise TransportError(f"HTTP request failed: {exc}") from exc

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse_response(resp.text)
        data = resp.json()
        return JsonRpcResponse.from_dict(data)

    def _parse_sse_response(self, text: str) -> JsonRpcResponse:
        """Parse SSE stream: notifications dispatch to handlers, last response returns.

        server 可在响应帧之间插入 notification 帧——逐行分发（F-R9-05），
        不再 last-wins 覆盖丢弃。
        """
        last_data: dict[str, Any] | None = None
        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if "method" in data and data.get("id") is None:
                self._dispatch_notification(data["method"], data.get("params") or {})
                continue
            last_data = data
        if last_data is not None:
            return JsonRpcResponse.from_dict(last_data)
        raise TransportError("No JSON-RPC response found in SSE stream")

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._closed:
            raise TransportClosedError("Transport is closed")

        msg = JsonRpcRequest(method=method, params=params or {})
        client = await self._get_client()

        try:
            resp = await client.post(
                self._endpoint_url,
                content=msg.to_bytes(),
                headers={**self._headers, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except Exception as exc:
            raise TransportError(f"HTTP notification failed: {exc}") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


class TransportMode(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"
