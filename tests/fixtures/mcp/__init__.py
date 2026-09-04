"""MCP test fixtures: fake MCP servers and transport for testing.

All fixtures are in-memory and do not require network or live MCP servers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zhiwei.capabilities.mcp.capabilities import ServerCapabilities
from zhiwei.capabilities.mcp.transport import (
    JsonRpcRequest,
    JsonRpcResponse,
    McpTransport,
    TransportClosedError,
)


@dataclass
class FakeMcpServer:
    """In-memory fake MCP server for testing.

    Simulates an MCP server's response behavior without network.
    """

    name: str = "fake-server"
    version: str = "0.1.0"
    capabilities: ServerCapabilities = field(default_factory=ServerCapabilities)
    tools: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    roots: list[dict[str, Any]] = field(default_factory=list)
    call_results: dict[str, dict[str, Any]] = field(default_factory=dict)

    def handle_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Handle a JSON-RPC method call and return the result dict."""
        if method == "initialize":
            return self._handle_initialize(params or {})
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            return self._handle_tool_call(params or {})
        if method == "resources/list":
            return {"resources": self.resources}
        if method == "resources/read":
            return self._handle_resource_read(params or {})
        if method == "prompts/list":
            return {"prompts": self.prompts}
        if method == "prompts/get":
            return self._handle_prompt_get(params or {})
        if method == "roots/list":
            return {"roots": self.roots}
        if method == "sampling/createMessage":
            return {"model": "test", "role": "assistant", "content": {"type": "text", "text": "test"}}
        if method == "elicitation/create":
            return {"action": "accept", "content": {"input": "test"}}
        if method == "tasks/list":
            return {"tasks": []}
        if method == "tasks/get":
            return {"task": {"id": (params or {}).get("taskId", ""), "status": "completed"}}
        if method == "shutdown":
            return {}
        return {}

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": "2025-03-26",
            "capabilities": {
                k: v
                for k, v in {
                    "tools": {} if self.capabilities.tools else None,
                    "resources": {} if self.capabilities.resources else None,
                    "prompts": {} if self.capabilities.prompts else None,
                    "roots": {} if self.capabilities.roots else None,
                    "sampling": {} if self.capabilities.sampling else None,
                    "elicitation": {} if self.capabilities.elicitation else None,
                    "tasks": {} if self.capabilities.tasks else None,
                    "logging": {} if self.capabilities.logging else None,
                }.items()
                if v is not None
            },
            "serverInfo": {"name": self.name, "version": self.version},
        }

    def _handle_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        if name in self.call_results:
            result = self.call_results[name]
            return result
        return {
            "content": [{"type": "text", "text": f"Called {name}"}],
            "isError": False,
        }

    def _handle_resource_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri", "")
        return {
            "contents": [{"uri": uri, "mimeType": "text/plain", "text": f"Content of {uri}"}]
        }

    def _handle_prompt_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        return {
            "description": f"Prompt: {name}",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": f"Prompt {name} content"}}
            ],
        }


class FakeTransport(McpTransport):
    """In-memory fake MCP transport for testing.

    Routes requests through a FakeMcpServer without network.
    """

    def __init__(self, server: FakeMcpServer | None = None) -> None:
        self._server = server or FakeMcpServer()
        self._closed = False
        self._request_log: list[dict[str, Any]] = []
        self._request_id = 0

    @property
    def request_log(self) -> list[dict[str, Any]]:
        return list(self._request_log)

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def send_request(self, request: JsonRpcRequest) -> JsonRpcResponse:
        if self._closed:
            raise TransportClosedError("Transport is closed")

        self._request_log.append(request.to_dict())

        try:
            result = self._server.handle_request(request.method, request.params)
            return JsonRpcResponse(id=request.id, result=result)
        except Exception as exc:
            return JsonRpcResponse(
                id=request.id,
                error={"code": -32603, "message": str(exc)},
            )

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._closed:
            raise TransportClosedError("Transport is closed")
        self._request_log.append({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        self._closed = True


def make_mcp_tool(
    name: str,
    description: str = "",
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a fake MCP tool definition for testing."""
    return {
        "name": name,
        "description": description,
        "inputSchema": input_schema or {"type": "object", "properties": {}},
    }


def make_mcp_resource(
    uri: str,
    name: str = "",
    description: str = "",
    mime_type: str = "text/plain",
) -> dict[str, Any]:
    """Create a fake MCP resource definition for testing."""
    return {
        "uri": uri,
        "name": name or uri.split("/")[-1],
        "description": description,
        "mimeType": mime_type,
    }


def make_mcp_prompt(
    name: str,
    description: str = "",
    arguments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a fake MCP prompt definition for testing."""
    return {
        "name": name,
        "description": description,
        "arguments": arguments or [],
    }
