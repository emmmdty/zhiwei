"""Anthropic /messages transport adapter."""

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
    "end_turn": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    "stop_sequence": FinishReason.STOP,
}


class AnthropicMessagesTransport(BaseTransport):
    """Transport for Anthropic /messages endpoints."""

    @property
    def wire_protocol(self) -> str:
        return "anthropic_messages"

    def build_wire_body(self, request: NormalizedRequest) -> dict[str, Any]:
        """Build /messages wire body from NormalizedRequest.

        Anthropic separates system prompt, uses content blocks for tool results,
        and has different parameter names.
        """
        system_text = ""
        messages: list[dict[str, Any]] = []

        for msg in request.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                if isinstance(content, str):
                    system_text = content
                elif isinstance(content, list):
                    system_text = "\n".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                continue
            messages.append({"role": role, "content": content})

        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
        }
        if system_text:
            body["system"] = system_text
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stream:
            body["stream"] = True
        if request.tools is not None:
            body["tools"] = request.tools
        if request.stop is not None:
            body["stop_sequences"] = (
                request.stop if isinstance(request.stop, list) else [request.stop]
            )
        body.update(request.extra)
        return body

    def parse_wire_response(self, data: dict[str, Any]) -> NormalizedResponse:
        """Parse /messages wire response into NormalizedResponse.

        Anthropic uses content blocks with type discrimination.
        """
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in data.get("content", []):
            block_type = block.get("type", "")
            if block_type == "text":
                content_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=json.dumps(block.get("input", {})),
                    )
                )

        stop_reason = data.get("stop_reason", "end_turn")
        usage_raw = data.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_raw.get("input_tokens", 0),
            completion_tokens=usage_raw.get("output_tokens", 0),
            total_tokens=usage_raw.get("input_tokens", 0) + usage_raw.get("output_tokens", 0),
        )

        return NormalizedResponse(
            id=data.get("id", ""),
            model=data.get("model", ""),
            content="\n".join(content_parts),
            finish_reason=_FINISH_REASON_MAP.get(stop_reason, FinishReason.UNKNOWN),
            tool_calls=tuple(tool_calls),
            usage=usage,
            raw=data,
        )

    def parse_stream_event(self, event_line: str, data_line: str) -> StreamDelta | None:
        """Parse an SSE event+data pair from /messages stream."""
        if not event_line.startswith("event: "):
            return None
        event_type = event_line[7:].strip()

        if data_line.startswith("data: "):
            data_str = data_line[6:]
        else:
            return None

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return None

        if event_type == "content_block_delta":
            delta_block = data.get("delta", {})
            if delta_block.get("type") == "text_delta":
                return StreamDelta(
                    id=data.get("message_id", ""),
                    delta_content=delta_block.get("text", ""),
                )
            if delta_block.get("type") == "input_json_delta":
                return StreamDelta(
                    id=data.get("message_id", ""),
                    delta_tool_calls=(
                        ToolCall(id="", name="", arguments=delta_block.get("partial_json", "")),
                    ),
                )
        elif event_type == "message_start":
            msg = data.get("message", {})
            return StreamDelta(id=msg.get("id", ""))
        elif event_type == "message_delta":
            delta = data.get("delta", {})
            stop_reason = delta.get("stop_reason")
            usage_raw = data.get("usage", {})
            usage = None
            if usage_raw:
                usage = Usage(
                    prompt_tokens=usage_raw.get("input_tokens", 0),
                    completion_tokens=usage_raw.get("output_tokens", 0),
                    total_tokens=usage_raw.get("input_tokens", 0)
                    + usage_raw.get("output_tokens", 0),
                )
            return StreamDelta(
                finish_reason=_FINISH_REASON_MAP.get(stop_reason, FinishReason.UNKNOWN)
                if stop_reason
                else None,
                usage=usage,
            )
        elif event_type == "message_stop":
            return StreamDelta(finish_reason=FinishReason.STOP)

        return None

    async def send(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        request: NormalizedRequest,
    ) -> NormalizedResponse:
        wire_body = self.build_wire_body(request)
        url = f"{base_url.rstrip('/')}/messages"
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        resp = await client.post(url, json=wire_body, headers=headers)
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
        url = f"{base_url.rstrip('/')}/messages"
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        async with client.stream("POST", url, json=wire_body, headers=headers) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise self._to_http_error(resp.status_code, text.decode())
            event_line = ""
            async for line_bytes in resp.aiter_lines():
                line = line_bytes
                if line.startswith("event: "):
                    event_line = line
                elif line.startswith("data: "):
                    delta = self.parse_stream_event(event_line, line)
                    if delta is not None:
                        yield delta
                    event_line = ""

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
