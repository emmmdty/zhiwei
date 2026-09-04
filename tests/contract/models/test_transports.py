"""S3-T2: Transport contract tests — golden fixtures for all three protocols.

Tests run WITHOUT network: httpx.MockTransport provides canned wire responses.
Each transport is tested against its real provider wire format.
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest

from zhiwei.models.transports.anthropic_messages import AnthropicMessagesTransport
from zhiwei.models.transports.base import (
    FinishReason,
    NormalizedRequest,
    Usage,
)
from zhiwei.models.transports.openai_chat import OpenAIChatTransport
from zhiwei.models.transports.openai_responses import OpenAIResponsesTransport

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_request(**overrides: Any) -> NormalizedRequest:
    defaults: dict[str, Any] = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ],
    }
    defaults.update(overrides)
    return NormalizedRequest(**defaults)


def _make_tool_request() -> NormalizedRequest:
    return _make_request(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                },
            }
        ],
        tool_choice="auto",
    )


# ---------------------------------------------------------------------------
# OpenAI Chat fixtures
# ---------------------------------------------------------------------------

OPENAI_CHAT_RESPONSE = {
    "id": "chatcmpl-test-123",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello! How can I help?"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
}

OPENAI_CHAT_STREAM_LINES = [
    'data: {"id":"chatcmpl-test-456","object":"chat.completion.chunk","created":1700000000,"model":"test-model","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-test-456","object":"chat.completion.chunk","created":1700000000,"model":"test-model","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-test-456","object":"chat.completion.chunk","created":1700000000,"model":"test-model","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-test-456","object":"chat.completion.chunk","created":1700000000,"model":"test-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    "data: [DONE]",
]

OPENAI_CHAT_TOOL_RESPONSE = {
    "id": "chatcmpl-test-789",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_test_001",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Beijing"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65},
}

OPENAI_CHAT_MALFORMED_STREAM = [
    "data: {not valid json",
    'data: {"id":"x","object":"chat.completion.chunk","created":0,"model":"m","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}',
    "data: [DONE]",
]


# ---------------------------------------------------------------------------
# OpenAI Responses fixtures
# ---------------------------------------------------------------------------

OPENAI_RESPONSES_RESPONSE = {
    "id": "resp-test-123",
    "object": "response",
    "created_at": 1700000000,
    "model": "test-model",
    "output": [
        {
            "type": "message",
            "id": "msg_test_001",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hello from responses API!"}],
            "stop_reason": "stop",
        }
    ],
    "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
}

OPENAI_RESPONSES_STREAM_EVENTS = [
    "event: response.created",
    'data: {"type":"response.created","response":{"id":"resp-test-456","model":"test-model","output":[]}}',
    "event: response.output_item.added",
    'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_001","role":"assistant","content":[]}}',
    "event: response.content_block.delta",
    'data: {"type":"response.content_block.delta","output_index":0,"content_index":0,"delta":{"type":"output_text_delta","text":"Hello"}}',
    "event: response.content_block.delta",
    'data: {"type":"response.content_block.delta","output_index":0,"content_index":0,"delta":{"type":"output_text_delta","text":" world"}}',
    "event: response.completed",
    'data: {"type":"response.completed","response":{"id":"resp-test-456","model":"test-model","output":[{"type":"message","stop_reason":"stop"}],"usage":{"input_tokens":12,"output_tokens":8,"total_tokens":20}}}',
]

OPENAI_RESPONSES_TOOL_RESPONSE = {
    "id": "resp-test-789",
    "object": "response",
    "created_at": 1700000000,
    "model": "test-model",
    "output": [
        {
            "type": "function_call",
            "call_id": "fc_test_001",
            "name": "get_weather",
            "arguments": '{"location": "Tokyo"}',
        }
    ],
    "usage": {"input_tokens": 50, "output_tokens": 15, "total_tokens": 65},
}

OPENAI_RESPONSES_TOOL_STREAM_EVENTS = [
    "event: response.created",
    'data: {"type":"response.created","response":{"id":"resp-test-888","model":"test-model","output":[]}}',
    "event: response.output_item.added",
    'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","call_id":"fc_001","name":"get_weather"}}',
    "event: response.content_block.delta",
    'data: {"type":"response.content_block.delta","item_id":"fc_001","call_id":"fc_001","delta":{"type":"input_json_delta","partial_json":"{\\"loc"}}',
    "event: response.content_block.delta",
    'data: {"type":"response.content_block.delta","item_id":"fc_001","call_id":"fc_001","delta":{"type":"input_json_delta","partial_json":"ation\\": \\"Tokyo\\"}"}}',
    "event: response.completed",
    'data: {"type":"response.completed","response":{"id":"resp-test-888","model":"test-model","output":[{"type":"function_call","call_id":"fc_001","name":"get_weather"}],"usage":{"input_tokens":50,"output_tokens":15,"total_tokens":65}}}',
]


# ---------------------------------------------------------------------------
# Anthropic Messages fixtures
# ---------------------------------------------------------------------------

ANTHROPIC_MESSAGES_RESPONSE = {
    "id": "msg_test_123",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [{"type": "text", "text": "Hello from Anthropic!"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 8},
}

ANTHROPIC_MESSAGES_STREAM_EVENTS = [
    "event: message_start",
    'data: {"type":"message_start","message":{"id":"msg_test_456","type":"message","role":"assistant","model":"test-model","content":[],"usage":{"input_tokens":12,"output_tokens":0}}}',
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" Anthropic!"}}',
    "event: content_block_stop",
    'data: {"type":"content_block_stop","index":0}',
    "event: message_delta",
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":8}}',
    "event: message_stop",
    'data: {"type":"message_stop"}',
]

ANTHROPIC_MESSAGES_TOOL_RESPONSE = {
    "id": "msg_test_789",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [
        {
            "type": "tool_use",
            "id": "toolu_test_001",
            "name": "get_weather",
            "input": {"location": "Paris"},
        }
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 50, "output_tokens": 15},
}

ANTHROPIC_MESSAGES_TOOL_STREAM_EVENTS = [
    "event: message_start",
    'data: {"type":"message_start","message":{"id":"msg_test_888","type":"message","role":"assistant","model":"test-model","content":[],"usage":{"input_tokens":50,"output_tokens":0}}}',
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_001","name":"get_weather"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"loc"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"ation\\": \\"Paris\\"}"}}',
    "event: content_block_stop",
    'data: {"type":"content_block_stop","index":0}',
    "event: message_delta",
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":15}}',
    "event: message_stop",
    'data: {"type":"message_stop"}',
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_responder(wire_body: dict[str, Any]) -> httpx.MockTransport:
    """Create a MockTransport that always returns the given JSON response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=wire_body)

    return httpx.MockTransport(handler)


def _mock_error_responder(status_code: int, body: dict[str, Any]) -> httpx.MockTransport:
    """Create a MockTransport that always returns an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return httpx.MockTransport(handler)


def _mock_sse_responder(lines: list[str], status_code: int = 200) -> httpx.MockTransport:
    """Create a MockTransport that returns SSE lines."""

    def handler(request: httpx.Request) -> httpx.Response:
        content = "\n".join(lines)
        return httpx.Response(
            status_code,
            content=content.encode(),
            headers={"content-type": "text/event-stream"},
        )

    return httpx.MockTransport(handler)


async def _collect_stream(transport_class: type, lines: list[str], request: NormalizedRequest) -> list:
    """Helper to collect all stream deltas from a transport."""
    mock = _mock_sse_responder(lines)
    client = httpx.AsyncClient(transport=mock)
    t = transport_class()
    deltas = []
    async for delta in t.send_stream(client, "http://test.api", request):
        deltas.append(delta)
    await client.aclose()
    return deltas


# ===========================================================================
# OpenAI Chat tests
# ===========================================================================


class TestOpenAIChatTransport:
    def test_wire_protocol_value(self) -> None:
        assert OpenAIChatTransport().wire_protocol == "openai_chat"

    def test_build_wire_body_minimal(self) -> None:
        req = _make_request()
        body = OpenAIChatTransport().build_wire_body(req)
        assert body["model"] == "test-model"
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["role"] == "user"
        assert "temperature" not in body
        assert "stream" not in body

    def test_build_wire_body_with_optional_fields(self) -> None:
        req = _make_request(temperature=0.7, max_tokens=100, stream=True, n=2)
        body = OpenAIChatTransport().build_wire_body(req)
        assert body["temperature"] == 0.7
        assert body["max_tokens"] == 100
        assert body["stream"] is True
        assert body["n"] == 2

    def test_build_wire_body_with_tools(self) -> None:
        req = _make_tool_request()
        body = OpenAIChatTransport().build_wire_body(req)
        assert len(body["tools"]) == 1
        assert body["tools"][0]["function"]["name"] == "get_weather"
        assert body["tool_choice"] == "auto"

    def test_build_wire_body_with_response_format(self) -> None:
        req = _make_request(
            response_format={"type": "json_schema", "schema": {"type": "object"}}
        )
        body = OpenAIChatTransport().build_wire_body(req)
        assert body["response_format"]["type"] == "json_schema"

    def test_parse_response_normal(self) -> None:
        resp = OpenAIChatTransport().parse_wire_response(OPENAI_CHAT_RESPONSE)
        assert resp.id == "chatcmpl-test-123"
        assert resp.model == "test-model"
        assert resp.content == "Hello! How can I help?"
        assert resp.finish_reason == FinishReason.STOP
        assert len(resp.tool_calls) == 0
        assert resp.usage.prompt_tokens == 12
        assert resp.usage.completion_tokens == 6
        assert resp.usage.total_tokens == 18

    def test_parse_response_with_tool_calls(self) -> None:
        resp = OpenAIChatTransport().parse_wire_response(OPENAI_CHAT_TOOL_RESPONSE)
        assert resp.finish_reason == FinishReason.TOOL_CALLS
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc.id == "call_test_001"
        assert tc.name == "get_weather"
        assert tc.arguments == '{"location": "Beijing"}'

    def test_parse_response_empty_choices(self) -> None:
        resp = OpenAIChatTransport().parse_wire_response({"id": "x", "choices": []})
        assert resp.id == "x"
        assert resp.content == ""
        assert resp.finish_reason == FinishReason.UNKNOWN

    def test_stream_yields_deltas(self) -> None:
        t = OpenAIChatTransport()
        deltas = [t.parse_stream_line(line) for line in OPENAI_CHAT_STREAM_LINES]
        non_none = [d for d in deltas if d is not None]
        assert len(non_none) == 5  # 4 chunks + [DONE]
        assert non_none[0].delta_content == ""
        assert non_none[1].delta_content == "Hello"
        assert non_none[2].delta_content == " world"
        assert non_none[3].finish_reason == FinishReason.STOP
        assert non_none[4].finish_reason == FinishReason.STOP  # [DONE]

    def test_stream_malformed_json_skipped(self) -> None:
        t = OpenAIChatTransport()
        deltas = [t.parse_stream_line(line) for line in OPENAI_CHAT_MALFORMED_STREAM]
        non_none = [d for d in deltas if d is not None]
        assert len(non_none) == 2  # malformed skipped, valid + [DONE]

    def test_classify_error_429(self) -> None:
        err = OpenAIChatTransport().classify_error(429, {"error": {"message": "Rate limit"}})
        assert err.category == "rate_limit"
        assert err.retryable is True
        assert err.status_code == 429

    def test_classify_error_500(self) -> None:
        err = OpenAIChatTransport().classify_error(500, {"error": {"message": "Internal error"}})
        assert err.category == "server_error"
        assert err.retryable is True

    def test_classify_error_401(self) -> None:
        err = OpenAIChatTransport().classify_error(401)
        assert err.category == "auth"
        assert err.retryable is False

    def test_classify_error_408_timeout(self) -> None:
        err = OpenAIChatTransport().classify_error(408)
        assert err.category == "unknown"
        assert err.retryable is False

    @pytest.mark.anyio
    async def test_send_non_streaming(self) -> None:
        transport = OpenAIChatTransport()
        mock = _mock_responder(OPENAI_CHAT_RESPONSE)
        client = httpx.AsyncClient(transport=mock)
        req = _make_request()
        resp = await transport.send(client, "http://test.api/v1", req)
        assert resp.content == "Hello! How can I help?"
        assert resp.finish_reason == FinishReason.STOP
        await client.aclose()

    @pytest.mark.anyio
    async def test_send_streaming(self) -> None:
        deltas = await _collect_stream(
            OpenAIChatTransport, OPENAI_CHAT_STREAM_LINES, _make_request()
        )
        contents = [d.delta_content for d in deltas if d.delta_content]
        assert contents == ["Hello", " world"]

    @pytest.mark.anyio
    async def test_send_tool_streaming(self) -> None:
        tool_stream_lines = [
            'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\\"loc"}}]},"finish_reason":null}]}',
            'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"id":"call_1","type":"function","function":{"arguments":"ation\\": \\"Beijing\\"}"}}]},"finish_reason":null}]}',
            'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        deltas = await _collect_stream(OpenAIChatTransport, tool_stream_lines, _make_tool_request())
        tool_deltas = [d for d in deltas if d.delta_tool_calls]
        assert len(tool_deltas) >= 1
        assert tool_deltas[0].delta_tool_calls[0].name == "get_weather"

    @pytest.mark.anyio
    async def test_send_error_response(self) -> None:
        transport = OpenAIChatTransport()
        mock = _mock_error_responder(429, {"error": {"message": "Rate limited"}})
        client = httpx.AsyncClient(transport=mock)
        req = _make_request()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await transport.send(client, "http://test.api/v1", req)
        assert "rate_limit" in str(exc_info.value)
        await client.aclose()


# ===========================================================================
# OpenAI Responses tests
# ===========================================================================


class TestOpenAIResponsesTransport:
    def test_wire_protocol_value(self) -> None:
        assert OpenAIResponsesTransport().wire_protocol == "openai_responses"

    def test_build_wire_body_minimal(self) -> None:
        req = _make_request()
        body = OpenAIResponsesTransport().build_wire_body(req)
        assert body["model"] == "test-model"
        assert body["input"] == req.messages
        assert "max_output_tokens" not in body
        assert "stream" not in body

    def test_build_wire_body_with_optional_fields(self) -> None:
        req = _make_request(max_tokens=500, stream=True)
        body = OpenAIResponsesTransport().build_wire_body(req)
        assert body["max_output_tokens"] == 500
        assert body["stream"] is True

    def test_build_wire_body_with_tools(self) -> None:
        req = _make_tool_request()
        body = OpenAIResponsesTransport().build_wire_body(req)
        assert len(body["tools"]) == 1
        assert body["tool_choice"] == "auto"

    def test_build_wire_body_stop_as_string(self) -> None:
        req = _make_request(stop="END")
        body = OpenAIResponsesTransport().build_wire_body(req)
        assert body["stop"] == ["END"]

    def test_build_wire_body_stop_as_list(self) -> None:
        req = _make_request(stop=["END", "STOP"])
        body = OpenAIResponsesTransport().build_wire_body(req)
        assert body["stop"] == ["END", "STOP"]

    def test_parse_response_normal(self) -> None:
        resp = OpenAIResponsesTransport().parse_wire_response(OPENAI_RESPONSES_RESPONSE)
        assert resp.id == "resp-test-123"
        assert resp.model == "test-model"
        assert resp.content == "Hello from responses API!"
        assert resp.finish_reason == FinishReason.STOP
        assert resp.usage.prompt_tokens == 12
        assert resp.usage.completion_tokens == 8

    def test_parse_response_with_tool_calls(self) -> None:
        resp = OpenAIResponsesTransport().parse_wire_response(OPENAI_RESPONSES_TOOL_RESPONSE)
        assert resp.finish_reason == FinishReason.TOOL_CALLS
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc.id == "fc_test_001"
        assert tc.name == "get_weather"
        assert tc.arguments == '{"location": "Tokyo"}'

    def test_parse_response_empty_output(self) -> None:
        resp = OpenAIResponsesTransport().parse_wire_response({"id": "x", "output": []})
        assert resp.id == "x"
        assert resp.content == ""

    def test_stream_text_deltas(self) -> None:
        t = OpenAIResponsesTransport()
        deltas = []
        event_line = ""
        for line in OPENAI_RESPONSES_STREAM_EVENTS:
            if line.startswith("event: "):
                event_line = line
            elif line.startswith("data: "):
                d = t.parse_stream_event(event_line, line)
                if d is not None:
                    deltas.append(d)
                event_line = ""
        text_deltas = [d.delta_content for d in deltas if d.delta_content]
        assert text_deltas == ["Hello", " world"]
        # Last delta should be from response.completed
        completed = [d for d in deltas if d.finish_reason == FinishReason.STOP]
        assert len(completed) >= 1

    def test_stream_tool_calls(self) -> None:
        t = OpenAIResponsesTransport()
        deltas = []
        event_line = ""
        for line in OPENAI_RESPONSES_TOOL_STREAM_EVENTS:
            if line.startswith("event: "):
                event_line = line
            elif line.startswith("data: "):
                d = t.parse_stream_event(event_line, line)
                if d is not None:
                    deltas.append(d)
                event_line = ""
        tool_deltas = [d for d in deltas if d.delta_tool_calls]
        assert len(tool_deltas) == 2
        # Arguments should accumulate
        full_args = "".join(
            tc.arguments
            for d in tool_deltas
            for tc in d.delta_tool_calls
        )
        assert "location" in full_args
        assert "Tokyo" in full_args

    def test_classify_error_429(self) -> None:
        err = OpenAIResponsesTransport().classify_error(429, {"error": {"message": "Rate limit"}})
        assert err.category == "rate_limit"
        assert err.retryable is True

    def test_classify_error_500(self) -> None:
        err = OpenAIResponsesTransport().classify_error(500)
        assert err.category == "server_error"
        assert err.retryable is True

    def test_classify_error_400(self) -> None:
        err = OpenAIResponsesTransport().classify_error(400, {"error": {"message": "Bad request"}})
        assert err.category == "content_filter"
        assert err.retryable is False

    @pytest.mark.anyio
    async def test_send_non_streaming(self) -> None:
        transport = OpenAIResponsesTransport()
        mock = _mock_responder(OPENAI_RESPONSES_RESPONSE)
        client = httpx.AsyncClient(transport=mock)
        req = _make_request()
        resp = await transport.send(client, "http://test.api/v1", req)
        assert resp.content == "Hello from responses API!"
        await client.aclose()

    @pytest.mark.anyio
    async def test_send_streaming(self) -> None:
        deltas = await _collect_stream(
            OpenAIResponsesTransport, OPENAI_RESPONSES_STREAM_EVENTS, _make_request()
        )
        contents = [d.delta_content for d in deltas if d.delta_content]
        assert contents == ["Hello", " world"]

    @pytest.mark.anyio
    async def test_send_tool_streaming(self) -> None:
        deltas = await _collect_stream(
            OpenAIResponsesTransport, OPENAI_RESPONSES_TOOL_STREAM_EVENTS, _make_tool_request()
        )
        tool_deltas = [d for d in deltas if d.delta_tool_calls]
        assert len(tool_deltas) >= 1

    @pytest.mark.anyio
    async def test_send_error_response(self) -> None:
        transport = OpenAIResponsesTransport()
        mock = _mock_error_responder(429, {"error": {"message": "Rate limited"}})
        client = httpx.AsyncClient(transport=mock)
        req = _make_request()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await transport.send(client, "http://test.api/v1", req)
        assert "rate_limit" in str(exc_info.value)
        await client.aclose()


# ===========================================================================
# Anthropic Messages tests
# ===========================================================================


class TestAnthropicMessagesTransport:
    def test_wire_protocol_value(self) -> None:
        assert AnthropicMessagesTransport().wire_protocol == "anthropic_messages"

    def test_build_wire_body_minimal(self) -> None:
        req = _make_request()
        body = AnthropicMessagesTransport().build_wire_body(req)
        assert body["model"] == "test-model"
        assert body["system"] == "You are a helpful assistant."
        assert len(body["messages"]) == 1
        assert body["messages"][0]["role"] == "user"
        assert "max_tokens" not in body

    def test_build_wire_body_extracts_system(self) -> None:
        req = _make_request()
        body = AnthropicMessagesTransport().build_wire_body(req)
        # System is extracted from messages
        assert "system" in body
        assert body["messages"][0]["role"] != "system"

    def test_build_wire_body_with_optional_fields(self) -> None:
        req = _make_request(max_tokens=1024, temperature=0.5, stream=True)
        body = AnthropicMessagesTransport().build_wire_body(req)
        assert body["max_tokens"] == 1024
        assert body["temperature"] == 0.5
        assert body["stream"] is True

    def test_build_wire_body_with_tools(self) -> None:
        req = _make_tool_request()
        body = AnthropicMessagesTransport().build_wire_body(req)
        assert len(body["tools"]) == 1
        assert body["tools"][0]["function"]["name"] == "get_weather"

    def test_build_wire_body_stop_sequences(self) -> None:
        req = _make_request(stop="END")
        body = AnthropicMessagesTransport().build_wire_body(req)
        assert body["stop_sequences"] == ["END"]

    def test_parse_response_normal(self) -> None:
        resp = AnthropicMessagesTransport().parse_wire_response(ANTHROPIC_MESSAGES_RESPONSE)
        assert resp.id == "msg_test_123"
        assert resp.model == "test-model"
        assert resp.content == "Hello from Anthropic!"
        assert resp.finish_reason == FinishReason.STOP
        assert resp.usage.prompt_tokens == 12
        assert resp.usage.completion_tokens == 8

    def test_parse_response_with_tool_calls(self) -> None:
        resp = AnthropicMessagesTransport().parse_wire_response(ANTHROPIC_MESSAGES_TOOL_RESPONSE)
        assert resp.finish_reason == FinishReason.TOOL_CALLS
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc.id == "toolu_test_001"
        assert tc.name == "get_weather"
        assert "location" in tc.arguments
        assert "Paris" in tc.arguments

    def test_parse_response_empty_content(self) -> None:
        resp = AnthropicMessagesTransport().parse_wire_response(
            {"id": "x", "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 0, "output_tokens": 0}}
        )
        assert resp.content == ""
        assert resp.finish_reason == FinishReason.STOP

    def test_stream_text_deltas(self) -> None:
        t = AnthropicMessagesTransport()
        deltas = []
        event_line = ""
        for line in ANTHROPIC_MESSAGES_STREAM_EVENTS:
            if line.startswith("event: "):
                event_line = line
            elif line.startswith("data: "):
                d = t.parse_stream_event(event_line, line)
                if d is not None:
                    deltas.append(d)
                event_line = ""
        text_deltas = [d.delta_content for d in deltas if d.delta_content]
        assert text_deltas == ["Hello", " Anthropic!"]
        stop_deltas = [d for d in deltas if d.finish_reason == FinishReason.STOP]
        assert len(stop_deltas) >= 1

    def test_stream_tool_calls(self) -> None:
        t = AnthropicMessagesTransport()
        deltas = []
        event_line = ""
        for line in ANTHROPIC_MESSAGES_TOOL_STREAM_EVENTS:
            if line.startswith("event: "):
                event_line = line
            elif line.startswith("data: "):
                d = t.parse_stream_event(event_line, line)
                if d is not None:
                    deltas.append(d)
                event_line = ""
        tool_deltas = [d for d in deltas if d.delta_tool_calls]
        assert len(tool_deltas) == 2
        full_json = "".join(
            tc.arguments for d in tool_deltas for tc in d.delta_tool_calls
        )
        assert "Paris" in full_json

    def test_classify_error_429(self) -> None:
        err = AnthropicMessagesTransport().classify_error(
            429, {"error": {"message": "Rate limited"}}
        )
        assert err.category == "rate_limit"
        assert err.retryable is True

    def test_classify_error_529(self) -> None:
        err = AnthropicMessagesTransport().classify_error(
            529, {"error": {"message": "Overloaded"}}
        )
        assert err.category == "server_error"
        assert err.retryable is True

    def test_classify_error_401(self) -> None:
        err = AnthropicMessagesTransport().classify_error(401)
        assert err.category == "auth"
        assert err.retryable is False

    @pytest.mark.anyio
    async def test_send_non_streaming(self) -> None:
        transport = AnthropicMessagesTransport()
        mock = _mock_responder(ANTHROPIC_MESSAGES_RESPONSE)
        client = httpx.AsyncClient(transport=mock)
        req = _make_request()
        resp = await transport.send(client, "http://test.api/v1", req)
        assert resp.content == "Hello from Anthropic!"
        await client.aclose()

    @pytest.mark.anyio
    async def test_send_streaming(self) -> None:
        deltas = await _collect_stream(
            AnthropicMessagesTransport, ANTHROPIC_MESSAGES_STREAM_EVENTS, _make_request()
        )
        contents = [d.delta_content for d in deltas if d.delta_content]
        assert contents == ["Hello", " Anthropic!"]

    @pytest.mark.anyio
    async def test_send_tool_streaming(self) -> None:
        deltas = await _collect_stream(
            AnthropicMessagesTransport,
            ANTHROPIC_MESSAGES_TOOL_STREAM_EVENTS,
            _make_tool_request(),
        )
        tool_deltas = [d for d in deltas if d.delta_tool_calls]
        assert len(tool_deltas) >= 1

    @pytest.mark.anyio
    async def test_send_error_response(self) -> None:
        transport = AnthropicMessagesTransport()
        mock = _mock_error_responder(429, {"error": {"message": "Rate limited"}})
        client = httpx.AsyncClient(transport=mock)
        req = _make_request()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await transport.send(client, "http://test.api/v1", req)
        assert "rate_limit" in str(exc_info.value)
        await client.aclose()


# ===========================================================================
# Cross-transport contract tests
# ===========================================================================


class TestTransportContract:
    """Verify all transports implement the same normalized interface."""

    @pytest.fixture(params=[OpenAIChatTransport, OpenAIResponsesTransport, AnthropicMessagesTransport])
    def transport(self, request: pytest.FixtureRequest) -> Any:
        return request.param()

    def test_has_wire_protocol(self, transport: Any) -> None:
        assert isinstance(transport.wire_protocol, str)
        assert len(transport.wire_protocol) > 0

    def test_build_wire_body_returns_dict(self, transport: Any) -> None:
        req = _make_request()
        body = transport.build_wire_body(req)
        assert isinstance(body, dict)
        assert "model" in body

    def test_parse_response_returns_normalized(self, transport: Any) -> None:
        # Each transport's parse_wire_response should return NormalizedResponse
        fixtures = {
            "openai_chat": OPENAI_CHAT_RESPONSE,
            "openai_responses": OPENAI_RESPONSES_RESPONSE,
            "anthropic_messages": ANTHROPIC_MESSAGES_RESPONSE,
        }
        raw = fixtures[transport.wire_protocol]
        resp = transport.parse_wire_response(raw)
        assert hasattr(resp, "id")
        assert hasattr(resp, "model")
        assert hasattr(resp, "content")
        assert hasattr(resp, "finish_reason")
        assert hasattr(resp, "usage")
        assert isinstance(resp.usage, Usage)

    def test_classify_error_returns_classification(self, transport: Any) -> None:
        err = transport.classify_error(429, {"error": {"message": "test"}})
        assert hasattr(err, "category")
        assert hasattr(err, "retryable")
        assert err.category == "rate_limit"
        assert err.retryable is True

    def test_all_finish_reasons_represented(self, transport: Any) -> None:
        # Ensure FinishReason enum values are usable
        for fr in FinishReason:
            assert isinstance(fr.value, str)


# ===========================================================================
# Edge case and error handling tests
# ===========================================================================


class TestEdgeCases:
    def test_openai_chat_timeout_stream(self) -> None:
        t = OpenAIChatTransport()
        # Timeout simulation: data: line with only a space
        delta = t.parse_stream_line("data: ")
        assert delta is None

    def test_openai_chat_empty_data_line(self) -> None:
        t = OpenAIChatTransport()
        delta = t.parse_stream_line("")
        assert delta is None

    def test_anthropic_unknown_event_type(self) -> None:
        t = AnthropicMessagesTransport()
        delta = t.parse_stream_event("event: ping", "data: {}")
        assert delta is None

    def test_openai_responses_unknown_event_type(self) -> None:
        t = OpenAIResponsesTransport()
        delta = t.parse_stream_event("event: unknown.event", "data: {}")
        assert delta is None

    def test_normalize_request_frozen(self) -> None:
        from pydantic import ValidationError

        req = _make_request()
        with pytest.raises(ValidationError):
            req.model = "changed"  # type: ignore[misc]

    def test_usage_frozen(self) -> None:
        from pydantic import ValidationError

        u = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        with pytest.raises(ValidationError):
            u.prompt_tokens = 0  # type: ignore[misc]

    def test_error_string_body(self) -> None:
        err = OpenAIChatTransport().classify_error(500, "Internal Server Error")
        assert err.category == "server_error"
        assert err.message == "Internal Server Error"

    def test_error_none_body(self) -> None:
        err = OpenAIChatTransport().classify_error(429)
        assert err.category == "rate_limit"
        assert err.message == ""

    @pytest.mark.anyio
    async def test_openai_chat_stream_timeout_error(self) -> None:
        mock = _mock_error_responder(408, {"error": {"message": "Request timeout"}})
        client = httpx.AsyncClient(transport=mock)
        t = OpenAIChatTransport()
        req = _make_request()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            async for _ in t.send_stream(client, "http://test.api/v1", req):
                pass
        assert exc_info.value.response.status_code == 408
        await client.aclose()

    @pytest.mark.anyio
    async def test_anthropic_stream_429_error(self) -> None:
        mock = _mock_error_responder(429, {"error": {"message": "Rate limit exceeded"}})
        client = httpx.AsyncClient(transport=mock)
        t = AnthropicMessagesTransport()
        req = _make_request()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            async for _ in t.send_stream(client, "http://test.api/v1", req):
                pass
        assert exc_info.value.response.status_code == 429
        await client.aclose()

    @pytest.mark.anyio
    async def test_openai_responses_stream_server_error(self) -> None:
        mock = _mock_error_responder(503, {"error": {"message": "Service unavailable"}})
        client = httpx.AsyncClient(transport=mock)
        t = OpenAIResponsesTransport()
        req = _make_request()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            async for _ in t.send_stream(client, "http://test.api/v1", req):
                pass
        assert exc_info.value.response.status_code == 503
        await client.aclose()
