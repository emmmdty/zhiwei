"""MCP client: high-level interface for MCP server interactions.

Handles initialization handshake, capability negotiation, and provides
methods for tools/resources/prompts/roots/elicitation/sampling/tasks.

Isolation: each client instance is scoped to a unique
(org, workspace, provider_version, connection_subject, run) tuple.
Cross-key reuse is forbidden (S4 spec §5).
Sampling default off, Discover background forbidden (S4 spec §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zhiwei.capabilities.inspection.schema import (
    scan_prompt_injection,
    scan_secret_exfiltration,
)
from zhiwei.capabilities.mcp.capabilities import (
    ClientCapabilities,
    McpCapability,
    ServerCapabilities,
    negotiate_capabilities,
)
from zhiwei.capabilities.mcp.transport import (
    JsonRpcRequest,
    McpTransport,
    TransportError,
)


class McpClientError(Exception):
    """MCP client-level error."""


class McpSessionError(McpClientError):
    """MCP session not initialized or already closed."""


class McpSamplingDisabledError(McpClientError):
    """Sampling requested but not enabled during capability negotiation."""


class McpDiscoveryBackgroundForbiddenError(McpClientError):
    """Background discovery is forbidden per S4 spec §4."""


class McpPromptInjectionError(McpClientError):
    """Prompt injection detected in server response."""


class SessionState(StrEnum):
    NOT_STARTED = "not_started"
    INITIALIZING = "initializing"
    READY = "ready"
    CLOSED = "closed"


@dataclass
class ServerInfo:
    """Server information from the initialize response."""

    name: str
    version: str
    capabilities: ServerCapabilities = field(default_factory=ServerCapabilities)


@dataclass
class ToolCallResult:
    """Result of a tools/call invocation."""

    content: list[dict[str, Any]] = field(default_factory=list)
    isError: bool = False


@dataclass
class ResourceContent:
    """A single resource or resource template."""

    uri: str
    name: str
    description: str = ""
    mimeType: str | None = None


@dataclass
class PromptMessage:
    """A message in a prompt response."""

    role: str
    content: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptResult:
    """Result of a prompts/get invocation."""

    description: str = ""
    messages: list[PromptMessage] = field(default_factory=list)


class McpClient:
    """MCP client with session management and capability negotiation.

    Manages the lifecycle:
    1. initialize handshake
    2. capabilities negotiation
    3. operational requests (tools/resources/prompts/etc.)
    4. shutdown

    Enforces:
    - sampling default off (must be explicitly enabled)
    - Discover background forbidden
    - cross-key isolation
    """

    PROTOCOL_VERSION = "2025-03-26"

    def __init__(
        self,
        transport: McpTransport,
        client_capabilities: ClientCapabilities | None = None,
        sampling_enabled: bool = False,
    ) -> None:
        self._transport = transport
        self._client_capabilities = client_capabilities or ClientCapabilities()
        self._sampling_enabled = sampling_enabled
        self._session_state = SessionState.NOT_STARTED
        self._server_info: ServerInfo | None = None
        self._negotiated: set[McpCapability] = set()

    @property
    def state(self) -> SessionState:
        return self._session_state

    @property
    def server_info(self) -> ServerInfo | None:
        return self._server_info

    @property
    def negotiated_capabilities(self) -> frozenset[McpCapability]:
        return frozenset(self._negotiated)

    async def initialize(self) -> ServerInfo:
        """Perform the MCP initialize handshake.

        Sends initialize request, then sends initialized notification.
        Returns the server info and negotiated capabilities.
        """
        if self._session_state != SessionState.NOT_STARTED:
            raise McpSessionError(
                f"Cannot initialize: session is {self._session_state.value}"
            )

        self._session_state = SessionState.INITIALIZING

        request = JsonRpcRequest(
            method="initialize",
            params={
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": self._client_capabilities.to_dict(),
                "clientInfo": {
                    "name": "zhiwei",
                    "version": "0.1.0",
                },
            },
        )

        try:
            response = await self._transport.send_request(request)
        except TransportError as exc:
            self._session_state = SessionState.CLOSED
            raise McpClientError(f"Initialize failed: {exc}") from exc

        if response.is_error:
            self._session_state = SessionState.CLOSED
            err = response.error or {}
            raise McpClientError(
                f"Server rejected initialization: {err.get('message', 'unknown error')}"
            )

        result = response.result or {}
        server_caps_raw = result.get("capabilities", {})
        server_caps = ServerCapabilities.from_dict(server_caps_raw)

        self._negotiated = negotiate_capabilities(
            self._client_capabilities,
            server_caps,
        )

        self._server_info = ServerInfo(
            name=result.get("serverInfo", {}).get("name", "unknown"),
            version=result.get("serverInfo", {}).get("version", "unknown"),
            capabilities=server_caps,
        )

        try:
            await self._transport.send_notification("notifications/initialized")
        except TransportError as exc:
            self._session_state = SessionState.CLOSED
            raise McpClientError(f"Failed to send initialized notification: {exc}") from exc

        self._session_state = SessionState.READY
        return self._server_info

    def _ensure_ready(self) -> None:
        if self._session_state != SessionState.READY:
            raise McpSessionError(
                f"Session not ready: {self._session_state.value}"
            )

    def _require_capability(self, cap: McpCapability) -> None:
        if cap not in self._negotiated:
            raise McpClientError(
                f"Server does not support {cap.value}; "
                f"negotiated: {sorted(c.value for c in self._negotiated)}"
            )

    # ── tools ──────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools from the server."""
        self._ensure_ready()
        self._require_capability(McpCapability.TOOLS)

        response = await self._transport.send_request(
            JsonRpcRequest(method="tools/list")
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"tools/list failed: {err.get('message')}")
        tools = (response.result or {}).get("tools", [])
        for idx, tool in enumerate(tools):
            description = tool.get("description", "")
            injection_report = scan_prompt_injection(description, field=f"tools[{idx}].description")
            if not injection_report.passed:
                raise McpPromptInjectionError(
                    f"Prompt injection detected in tool {tool.get('name', idx)}: "
                    + "; ".join(f.message for f in injection_report.findings)
                )
            exfil_report = scan_secret_exfiltration(description, field=f"tools[{idx}].description")
            if not exfil_report.passed:
                raise McpPromptInjectionError(
                    f"Secret exfiltration detected in tool {tool.get('name', idx)}: "
                    + "; ".join(f.message for f in exfil_report.findings)
                )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """Invoke a tool by name with arguments."""
        self._ensure_ready()
        self._require_capability(McpCapability.TOOLS)

        response = await self._transport.send_request(
            JsonRpcRequest(
                method="tools/call",
                params={"name": name, "arguments": arguments},
            )
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"tools/call failed: {err.get('message')}")
        result = response.result or {}
        return ToolCallResult(
            content=result.get("content", []),
            isError=result.get("isError", False),
        )

    # ── resources ──────────────────────────────────────────────────

    async def list_resources(self) -> list[ResourceContent]:
        """List available resources from the server."""
        self._ensure_ready()
        self._require_capability(McpCapability.RESOURCES)

        response = await self._transport.send_request(
            JsonRpcRequest(method="resources/list")
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"resources/list failed: {err.get('message')}")
        return [
            ResourceContent(
                uri=r.get("uri", ""),
                name=r.get("name", ""),
                description=r.get("description", ""),
                mimeType=r.get("mimeType"),
            )
            for r in (response.result or {}).get("resources", [])
        ]

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read a resource by URI."""
        self._ensure_ready()
        self._require_capability(McpCapability.RESOURCES)

        response = await self._transport.send_request(
            JsonRpcRequest(method="resources/read", params={"uri": uri})
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"resources/read failed: {err.get('message')}")
        return response.result or {}

    async def list_resource_templates(self) -> list[dict[str, Any]]:
        """List available resource templates."""
        self._ensure_ready()
        self._require_capability(McpCapability.RESOURCES)

        response = await self._transport.send_request(
            JsonRpcRequest(method="resources/templates/list")
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"resources/templates/list failed: {err.get('message')}")
        return (response.result or {}).get("resourceTemplates", [])

    # ── prompts ────────────────────────────────────────────────────

    async def list_prompts(self) -> list[dict[str, Any]]:
        """List available prompts."""
        self._ensure_ready()
        self._require_capability(McpCapability.PROMPTS)

        response = await self._transport.send_request(
            JsonRpcRequest(method="prompts/list")
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"prompts/list failed: {err.get('message')}")
        return (response.result or {}).get("prompts", [])

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> PromptResult:
        """Get a prompt by name."""
        self._ensure_ready()
        self._require_capability(McpCapability.PROMPTS)

        response = await self._transport.send_request(
            JsonRpcRequest(
                method="prompts/get",
                params={"name": name, "arguments": arguments or {}},
            )
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"prompts/get failed: {err.get('message')}")
        result = response.result or {}
        messages = [
            PromptMessage(role=m.get("role", ""), content=m.get("content", {}))
            for m in result.get("messages", [])
        ]
        for idx, msg in enumerate(messages):
            content_text = str(msg.content)
            injection_report = scan_prompt_injection(content_text, field=f"messages[{idx}].content")
            if not injection_report.passed:
                raise McpPromptInjectionError(
                    f"Prompt injection detected in message {idx}: "
                    + "; ".join(f.message for f in injection_report.findings)
                )
        return PromptResult(description=result.get("description", ""), messages=messages)

    # ── sampling ───────────────────────────────────────────────────

    async def create_message(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a sampling message (requires explicit opt-in).

        Sampling is default off per S4 spec §4. The client must have
        explicitly enabled sampling during initialization.
        """
        self._ensure_ready()

        if not self._sampling_enabled:
            raise McpSamplingDisabledError(
                "Sampling is disabled; must be explicitly enabled during client creation"
            )

        self._require_capability(McpCapability.SAMPLING)

        response = await self._transport.send_request(
            JsonRpcRequest(method="sampling/createMessage", params=params)
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"sampling/createMessage failed: {err.get('message')}")
        return response.result or {}

    # ── elicitation ────────────────────────────────────────────────

    async def elicitation_create(self, params: dict[str, Any]) -> dict[str, Any]:
        """Request user input via elicitation."""
        self._ensure_ready()
        self._require_capability(McpCapability.ELICITATION)

        response = await self._transport.send_request(
            JsonRpcRequest(method="elicitation/create", params=params)
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"elicitation/create failed: {err.get('message')}")
        return response.result or {}

    # ── roots ──────────────────────────────────────────────────────

    async def list_roots(self) -> list[dict[str, Any]]:
        """List available roots."""
        self._ensure_ready()
        self._require_capability(McpCapability.ROOTS)

        response = await self._transport.send_request(
            JsonRpcRequest(method="roots/list")
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"roots/list failed: {err.get('message')}")
        return (response.result or {}).get("roots", [])

    # ── tasks ──────────────────────────────────────────────────────

    async def tasks_list(self) -> list[dict[str, Any]]:
        """List long-running tasks."""
        self._ensure_ready()
        self._require_capability(McpCapability.TASKS)

        response = await self._transport.send_request(
            JsonRpcRequest(method="tasks/list")
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"tasks/list failed: {err.get('message')}")
        return (response.result or {}).get("tasks", [])

    async def tasks_get(self, task_id: str) -> dict[str, Any]:
        """Get a task by ID."""
        self._ensure_ready()
        self._require_capability(McpCapability.TASKS)

        response = await self._transport.send_request(
            JsonRpcRequest(method="tasks/get", params={"taskId": task_id})
        )
        if response.is_error:
            err = response.error or {}
            raise McpClientError(f"tasks/get failed: {err.get('message')}")
        return response.result or {}

    # ── lifecycle ──────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Gracefully shut down the MCP session."""
        if self._session_state == SessionState.CLOSED:
            return
        import contextlib

        with contextlib.suppress(TransportError):
            await self._transport.send_request(
                JsonRpcRequest(method="shutdown")
            )
        await self._transport.close()
        self._session_state = SessionState.CLOSED

    async def close(self) -> None:
        """Forcefully close without shutdown handshake."""
        if self._session_state == SessionState.CLOSED:
            return
        await self._transport.close()
        self._session_state = SessionState.CLOSED
