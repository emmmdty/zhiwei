"""OpenAI /responses transport adapter."""

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
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


class OpenAIResponsesTransport(BaseTransport):
    """Transport for OpenAI-compatible /responses endpoints."""

    @property
    def wire_protocol(self) -> str:
        return "openai_responses"

    def build_wire_body(self, request: NormalizedRequest) -> dict[str, Any]:
        """Build /responses wire body from NormalizedRequest.

        The /responses API uses `input` instead of `messages` and
        `instructions` for system context.
        """
        body: dict[str, Any] = {
            "model": request.model,
            "input": request.messages,
        }
        if request.max_tokens is not None:
            body["max_output_tokens"] = request.max_tokens
        if request.stream:
            body["stream"] = True
        if request.tools is not None:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.stop is not None:
            body["stop"] = request.stop if isinstance(request.stop, list) else [request.stop]
        body.update(request.extra)
        return body

    def parse_wire_response(self, data: dict[str, Any]) -> NormalizedResponse:
        """Parse /responses wire response into NormalizedResponse.

        The response has `output` items with type-based roles.
        """
        output_items = data.get("output", [])
        content = ""
        tool_calls: list[ToolCall] = []
        finish_reason = FinishReason.UNKNOWN

        for item in output_items:
            item_type = item.get("type", "")
            if item_type == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content += part.get("text", "")
                finish_reason = _FINISH_REASON_MAP.get(
                    item.get("stop_reason", ""), FinishReason.UNKNOWN
                )
            elif item_type == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=item.get("call_id", ""),
                        name=item.get("name", ""),
                        arguments=item.get("arguments", ""),
                    )
                )
                finish_reason = FinishReason.TOOL_CALLS

        usage_raw = data.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_raw.get("input_tokens", 0),
            completion_tokens=usage_raw.get("output_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
        )

        return NormalizedResponse(
            id=data.get("id", ""),
            model=data.get("model", ""),
            content=content,
            finish_reason=finish_reason,
            tool_calls=tuple(tool_calls),
            usage=usage,
            raw=data,
        )

    def parse_stream_event(self, event_line: str, data_line: str) -> StreamDelta | None:
        """Parse an SSE event+data pair from /responses stream."""
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

        if event_type in ("response.output_text.delta", "response.content_block.delta"):
            delta_obj = data.get("delta", {})
            if isinstance(delta_obj, str):
                return StreamDelta(id=data.get("item_id", ""), delta_content=delta_obj)
            if isinstance(delta_obj, dict):
                delta_type = delta_obj.get("type", "")
                if delta_type == "output_text_delta":
                    return StreamDelta(
                        id=data.get("item_id", ""),
                        delta_content=delta_obj.get("text", ""),
                    )
                if delta_type == "input_json_delta":
                    return StreamDelta(
                        id=data.get("item_id", ""),
                        delta_tool_calls=(
                            ToolCall(
                                id=data.get("call_id", ""),
                                name="",
                                arguments=delta_obj.get("partial_json", ""),
                            ),
                        ),
                    )
            return None
        if event_type == "response.function_call_arguments.delta":
            return StreamDelta(
                id=data.get("item_id", ""),
                delta_tool_calls=(
                    ToolCall(
                        id=data.get("call_id", ""),
                        name="",
                        arguments=data.get("delta", ""),
                    ),
                ),
            )
        if event_type == "response.completed":
            resp_data = data.get("response", {})
            usage_raw = resp_data.get("usage", {})
            usage = Usage(
                prompt_tokens=usage_raw.get("input_tokens", 0),
                completion_tokens=usage_raw.get("output_tokens", 0),
                total_tokens=usage_raw.get("total_tokens", 0),
            )
            return StreamDelta(
                id=resp_data.get("id", ""),
                finish_reason=FinishReason.STOP,
                usage=usage,
            )
        if event_type == "response.done":
            return StreamDelta(finish_reason=FinishReason.STOP)

        return None

    async def send(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        request: NormalizedRequest,
    ) -> NormalizedResponse:
        wire_body = self.build_wire_body(request)
        url = f"{base_url.rstrip('/')}/responses"
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
        url = f"{base_url.rstrip('/')}/responses"
        async with client.stream("POST", url, json=wire_body) as resp:
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
