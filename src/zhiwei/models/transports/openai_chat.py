"""OpenAI /chat/completions transport adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx

from zhiwei.models.transports.base import (
    BaseTransport,
    ErrorClassification,
    FinishReason,
    NormalizedRequest,
    NormalizedResponse,
    StreamDelta,
    ToolCall,
    Usage,
)

_FINISH_REASON_MAP: dict[str, FinishReason] = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


class OpenAIChatTransport(BaseTransport):
    """Transport for OpenAI-compatible /chat/completions endpoints."""

    @property
    def wire_protocol(self) -> str:
        return "openai_chat"

    def build_wire_body(self, request: NormalizedRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.stream:
            body["stream"] = True
        if request.tools is not None:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.response_format is not None:
            body["response_format"] = request.response_format
        if request.stop is not None:
            body["stop"] = request.stop
        if request.n is not None:
            body["n"] = request.n
        body.update(request.extra)
        return body

    def parse_wire_response(self, data: dict[str, Any]) -> NormalizedResponse:
        choices = data.get("choices", [])
        if not choices:
            return NormalizedResponse(
                id=data.get("id", ""),
                model=data.get("model", ""),
                raw=data,
            )

        first = choices[0]
        message = first.get("message", {})
        content = message.get("content") or ""
        raw_finish = first.get("finish_reason", "stop")

        tool_calls_raw = message.get("tool_calls", [])
        tool_calls = tuple(
            ToolCall(
                id=tc.get("id", ""),
                name=tc.get("function", {}).get("name", ""),
                arguments=tc.get("function", {}).get("arguments", ""),
            )
            for tc in tool_calls_raw
        )

        usage_raw = data.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
        )

        return NormalizedResponse(
            id=data.get("id", ""),
            model=data.get("model", ""),
            content=content,
            finish_reason=_FINISH_REASON_MAP.get(raw_finish, FinishReason.UNKNOWN),
            tool_calls=tool_calls,
            usage=usage,
            raw=data,
        )

    def parse_stream_line(self, line: str) -> StreamDelta | None:
        """Parse a single SSE data line from /chat/completions stream."""
        raw = self._extract_sse_data(line)
        if raw is None or raw.strip() == "[DONE]":
            if raw is not None and raw.strip() == "[DONE]":
                return StreamDelta(finish_reason=FinishReason.STOP)
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        choices = data.get("choices", [])
        if not choices:
            return None

        first = choices[0]
        delta = first.get("delta", {})
        content = delta.get("content") or ""

        tool_calls_raw = delta.get("tool_calls", [])
        tool_calls = tuple(
            ToolCall(
                id=tc.get("id", ""),
                name=tc.get("function", {}).get("name", ""),
                arguments=tc.get("function", {}).get("arguments", ""),
            )
            for tc in tool_calls_raw
        )

        raw_finish = first.get("finish_reason")
        finish_reason = (
            _FINISH_REASON_MAP.get(raw_finish, FinishReason.UNKNOWN) if raw_finish else None
        )

        usage_raw = data.get("usage")
        usage = None
        if usage_raw:
            usage = Usage(
                prompt_tokens=usage_raw.get("prompt_tokens", 0),
                completion_tokens=usage_raw.get("completion_tokens", 0),
                total_tokens=usage_raw.get("total_tokens", 0),
            )

        return StreamDelta(
            id=data.get("id", ""),
            delta_content=content,
            delta_tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def send(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        request: NormalizedRequest,
    ) -> NormalizedResponse:
        wire_body = self.build_wire_body(request)
        url = f"{base_url.rstrip('/')}/chat/completions"
        resp = await client.post(url, json=wire_body)
        if resp.status_code != 200:
            raise self._to_http_error(resp.status_code, resp.text)
        return self.parse_wire_response(resp.json())

    def send_stream(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        request: NormalizedRequest,
    ) -> AsyncIterator[StreamDelta]:
        return self._send_stream_impl(client, base_url, request)

    async def _send_stream_impl(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        request: NormalizedRequest,
    ) -> AsyncIterator[StreamDelta]:
        wire_body = self.build_wire_body(request)
        wire_body["stream"] = True
        url = f"{base_url.rstrip('/')}/chat/completions"
        async with client.stream("POST", url, json=wire_body) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise self._to_http_error(resp.status_code, text.decode())
            async for line_bytes in resp.aiter_lines():
                delta = self.parse_stream_line(line_bytes)
                if delta is not None:
                    yield delta

    def classify_error(
        self, status_code: int, body: dict[str, Any] | str | None = None
    ) -> ErrorClassification:
        category = self._classify_http_status(status_code)
        message = ""
        if isinstance(body, dict):
            err = body.get("error", {})
            message = err.get("message", "") if isinstance(err, dict) else str(err)
        elif isinstance(body, str):
            message = body

        retryable = category in ("rate_limit", "server_error")
        return ErrorClassification(
            category=category,
            status_code=status_code,
            message=message,
            retryable=retryable,
        )

    def _to_http_error(self, status_code: int, body: str) -> httpx.HTTPStatusError:
        classification = self.classify_error(status_code, body)
        return httpx.HTTPStatusError(
            f"[{classification.category}] {classification.message}",
            request=httpx.Request("POST", ""),
            response=httpx.Response(status_code, text=body),
        )
