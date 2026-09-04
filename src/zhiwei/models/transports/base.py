"""Abstract base transport and provider-neutral normalized types."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any

import httpx2 as httpx
from pydantic import BaseModel, Field


class FinishReason(StrEnum):
    """Normalized finish reasons across all providers."""

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"


class ToolCall(BaseModel):
    """Normalized tool call in a response."""

    model_config = {"frozen": True}

    id: str
    name: str
    arguments: str = ""


class ToolCallResult(BaseModel):
    """A tool call result to send back in a follow-up request."""

    model_config = {"frozen": True}

    tool_call_id: str
    content: str
    is_error: bool = False


class Usage(BaseModel):
    """Normalized token usage counts."""

    model_config = {"frozen": True}

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class NormalizedRequest(BaseModel):
    """Provider-neutral request representation.

    Transports serialize this to wire format; the runtime never touches
    provider-specific request types.
    """

    model_config = {"frozen": True}

    model: str
    messages: list[dict[str, Any]]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    stop: list[str] | str | None = None
    n: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class NormalizedResponse(BaseModel):
    """Provider-neutral non-streaming response."""

    model_config = {"frozen": True}

    id: str = ""
    model: str = ""
    content: str = ""
    finish_reason: FinishReason = FinishReason.UNKNOWN
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Field(default_factory=Usage)
    raw: dict[str, Any] = Field(default_factory=dict)


class StreamDelta(BaseModel):
    """Provider-neutral streaming delta event."""

    model_config = {"frozen": True}

    id: str = ""
    delta_content: str = ""
    delta_tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason | None = None
    usage: Usage | None = None


class ErrorClassification(BaseModel):
    """Classified provider error for policy routing."""

    model_config = {"frozen": True}

    category: str  # "rate_limit" | "server_error" | "timeout" | "auth" | "content_filter" | "unknown"
    status_code: int | None = None
    message: str = ""
    retryable: bool = False


class BaseTransport(abc.ABC):
    """Abstract base for provider transports.

    Each concrete transport:
    - Builds provider-specific wire requests from NormalizedRequest
    - Parses provider-specific wire responses into NormalizedResponse / StreamDelta
    - Classifies provider-specific errors into ErrorClassification
    - Supports both streaming and non-streaming
    """

    @property
    @abc.abstractmethod
    def wire_protocol(self) -> str:
        """Return the WireProtocol value (e.g. 'openai_chat')."""

    @abc.abstractmethod
    async def send(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        request: NormalizedRequest,
    ) -> NormalizedResponse:
        """Send a non-streaming request and return a normalized response."""

    @abc.abstractmethod
    def send_stream(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        request: NormalizedRequest,
    ) -> AsyncIterator[StreamDelta]:
        """Send a streaming request and yield normalized deltas.

        Concrete implementations should be async generator functions decorated
        with @abc.abstractmethod or return an AsyncIterator from an async def.
        """

    @abc.abstractmethod
    def build_wire_body(self, request: NormalizedRequest) -> dict[str, Any]:
        """Build the provider-specific wire request body from a normalized request."""

    @abc.abstractmethod
    def parse_wire_response(self, data: dict[str, Any]) -> NormalizedResponse:
        """Parse a provider-specific wire response body into a normalized response."""

    @abc.abstractmethod
    def classify_error(
        self, status_code: int, body: dict[str, Any] | str | None = None
    ) -> ErrorClassification:
        """Classify a provider error response into an error category."""

    def _classify_http_status(self, status_code: int) -> str:
        """Shared HTTP status code to category mapping."""
        if status_code == 429:
            return "rate_limit"
        if status_code == 401 or status_code == 403:
            return "auth"
        if 400 <= status_code < 500:
            return "content_filter" if status_code == 400 else "unknown"
        if 500 <= status_code < 600:
            return "server_error"
        return "unknown"

    @staticmethod
    def _extract_sse_data(line: str) -> str | None:
        """Extract data payload from an SSE 'data: ' line."""
        if line.startswith("data: "):
            return line[6:]
        return None
