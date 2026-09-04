"""S4-T4 Contract: MCP client, transport, mapping, and capability negotiation.

验证:
- JsonRpcRequest/Response 序列化正确
- StdioTransport isolation key enforcement
- StreamableHttpTransport isolation key enforcement
- McpClient initialize/handshake flow
- McpClient tools/resources/prompts/roots/sampling/tasks operations
- McpClient sampling disabled by default
- McpClient state transitions
- mapping: MCP tool → ToolDefinitionVersion
- mapping: MCP resource → ResourceDefinition
- mapping: MCP prompt → PromptDefinition
- mapping: batch operations
- mapping: rejects missing required fields
- capability negotiation: client+server intersection
- capability negotiation: sampling only when both opt-in
- FakeTransport/FakeMcpServer fixture correctness
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fixtures.mcp import (
    FakeMcpServer,
    FakeTransport,
    make_mcp_prompt,
    make_mcp_resource,
    make_mcp_tool,
)

from zhiwei.capabilities.mcp.capabilities import (
    ClientCapabilities,
    McpCapability,
    ServerCapabilities,
    negotiate_capabilities,
)
from zhiwei.capabilities.mcp.client import (
    McpClient,
    McpClientError,
    McpSamplingDisabledError,
    McpSessionError,
    SessionState,
)
from zhiwei.capabilities.mcp.mapping import (
    MappingError,
    map_mcp_prompt_to_prompt_definition,
    map_mcp_resource_to_resource_definition,
    map_mcp_tool_to_tool_definition,
    map_mcp_tools_batch,
)
from zhiwei.capabilities.mcp.transport import (
    JsonRpcRequest,
    JsonRpcResponse,
    TransportError,
)

# ── JSON-RPC ──────────────────────────────────────────────────────


class TestJsonRpc:
    def test_request_serialization(self) -> None:
        req = JsonRpcRequest(method="tools/list", id=1)
        d = req.to_dict()
        assert d["jsonrpc"] == "2.0"
        assert d["method"] == "tools/list"
        assert d["id"] == 1
        assert "params" not in d

    def test_request_with_params(self) -> None:
        req = JsonRpcRequest(
            method="tools/call",
            params={"name": "foo", "arguments": {"x": 1}},
            id=42,
        )
        d = req.to_dict()
        assert d["params"]["name"] == "foo"
        assert d["params"]["arguments"] == {"x": 1}

    def test_notification_has_no_id(self) -> None:
        req = JsonRpcRequest(method="notifications/initialized")
        d = req.to_dict()
        assert "id" not in d

    def test_response_from_dict_success(self) -> None:
        resp = JsonRpcResponse.from_dict({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
        assert not resp.is_error
        assert resp.result == {"tools": []}
        assert resp.id == 1

    def test_response_from_dict_error(self) -> None:
        resp = JsonRpcResponse.from_dict(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "not found"}}
        )
        assert resp.is_error
        assert resp.error is not None
        assert resp.error["code"] == -32601

    def test_request_to_bytes(self) -> None:
        req = JsonRpcRequest(method="ping", id=1)
        raw = req.to_bytes()
        assert b'"method": "ping"' in raw
        assert b'"jsonrpc": "2.0"' in raw


# ── Transport isolation ──────────────────────────────────────────


class TestTransportIsolation:
    def test_stdio_transport_rejects_cross_key(self) -> None:
        from zhiwei.capabilities.mcp.transport import StdioTransport

        transport = StdioTransport(
            command="echo",
            isolation_key=("org1", "ws1", "pv1", "conn1", "run1"),
        )
        with pytest.raises(TransportError, match="Cross-key reuse"):
            transport.check_isolation(("org1", "ws1", "pv2", "conn1", "run1"))

    def test_stdio_transport_accepts_matching_key(self) -> None:
        from zhiwei.capabilities.mcp.transport import StdioTransport

        key = ("org1", "ws1", "pv1", "conn1", "run1")
        transport = StdioTransport(command="echo", isolation_key=key)
        transport.check_isolation(key)

    def test_http_transport_rejects_cross_key(self) -> None:
        from zhiwei.capabilities.mcp.transport import StreamableHttpTransport

        transport = StreamableHttpTransport(
            endpoint_url="https://example.com/mcp",
            isolation_key=("org1", "ws1", "pv1"),
        )
        with pytest.raises(TransportError, match="Cross-key reuse"):
            transport.check_isolation(("org2", "ws1", "pv1"))

    def test_empty_isolation_key_always_passes(self) -> None:
        from zhiwei.capabilities.mcp.transport import StdioTransport

        transport = StdioTransport(command="echo", isolation_key=())
        transport.check_isolation(("anything",))


# ── Client session lifecycle ─────────────────────────────────────


class TestMcpClientLifecycle:
    @pytest.fixture
    def client_transport(self) -> FakeTransport:
        return FakeTransport(
            FakeMcpServer(
                capabilities=ServerCapabilities(
                    tools=True, resources=True, prompts=True, roots=True, tasks=True
                )
            )
        )

    @pytest.mark.asyncio
    async def test_initial_state(self, client_transport: FakeTransport) -> None:
        client = McpClient(client_transport)
        assert client.state == SessionState.NOT_STARTED
        assert client.server_info is None

    @pytest.mark.asyncio
    async def test_initialize_sets_ready(self, client_transport: FakeTransport) -> None:
        client = McpClient(client_transport)
        info = await client.initialize()
        assert client.state == SessionState.READY
        assert info.name == "fake-server"
        assert McpCapability.TOOLS in client.negotiated_capabilities

    @pytest.mark.asyncio
    async def test_initialize_cannot_double_init(self, client_transport: FakeTransport) -> None:
        client = McpClient(client_transport)
        await client.initialize()
        with pytest.raises(McpSessionError, match="Cannot initialize"):
            await client.initialize()

    @pytest.mark.asyncio
    async def test_operations_require_ready_state(self, client_transport: FakeTransport) -> None:
        client = McpClient(client_transport)
        with pytest.raises(McpSessionError, match="not ready"):
            await client.list_tools()

    @pytest.mark.asyncio
    async def test_shutdown_transitions_to_closed(self, client_transport: FakeTransport) -> None:
        client = McpClient(client_transport)
        await client.initialize()
        await client.shutdown()
        assert client.state == SessionState.CLOSED

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, client_transport: FakeTransport) -> None:
        client = McpClient(client_transport)
        await client.close()
        await client.close()
        assert client.state == SessionState.CLOSED


# ── Client tools/resources/prompts ────────────────────────────────


class TestMcpClientOperations:
    @pytest.fixture
    def ready_client(self) -> tuple[McpClient, FakeTransport]:
        server = FakeMcpServer(
            capabilities=ServerCapabilities(
                tools=True, resources=True, prompts=True, roots=True, tasks=True
            ),
            tools=[make_mcp_tool("search", "Search documents")],
            resources=[make_mcp_resource("file:///data.csv", "data")],
            prompts=[make_mcp_prompt("summarize", "Summarize text")],
        )
        transport = FakeTransport(server)
        client = McpClient(
            transport,
            client_capabilities=ClientCapabilities(
                tools=True, resources=True, prompts=True, roots=True, tasks=True
            ),
        )
        return client, transport

    @pytest.mark.asyncio
    async def test_list_tools(self, ready_client: tuple[McpClient, FakeTransport]) -> None:
        client, _ = ready_client
        await client.initialize()
        tools = await client.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "search"

    @pytest.mark.asyncio
    async def test_call_tool(self, ready_client: tuple[McpClient, FakeTransport]) -> None:
        client, _ = ready_client
        await client.initialize()
        result = await client.call_tool("search", {"query": "test"})
        assert not result.isError
        assert len(result.content) == 1

    @pytest.mark.asyncio
    async def test_list_resources(self, ready_client: tuple[McpClient, FakeTransport]) -> None:
        client, _ = ready_client
        await client.initialize()
        resources = await client.list_resources()
        assert len(resources) == 1
        assert resources[0].uri == "file:///data.csv"

    @pytest.mark.asyncio
    async def test_list_prompts(self, ready_client: tuple[McpClient, FakeTransport]) -> None:
        client, _ = ready_client
        await client.initialize()
        prompts = await client.list_prompts()
        assert len(prompts) == 1
        assert prompts[0]["name"] == "summarize"

    @pytest.mark.asyncio
    async def test_list_roots(self, ready_client: tuple[McpClient, FakeTransport]) -> None:
        client, _ = ready_client
        await client.initialize()
        roots = await client.list_roots()
        assert isinstance(roots, list)

    @pytest.mark.asyncio
    async def test_tasks_list(self, ready_client: tuple[McpClient, FakeTransport]) -> None:
        client, _ = ready_client
        await client.initialize()
        tasks = await client.tasks_list()
        assert isinstance(tasks, list)


# ── Sampling default off ─────────────────────────────────────────


class TestMcpClientSampling:
    @pytest.mark.asyncio
    async def test_sampling_disabled_by_default(self) -> None:
        server = FakeMcpServer(capabilities=ServerCapabilities(sampling=True))
        transport = FakeTransport(server)
        client = McpClient(transport, sampling_enabled=False)
        await client.initialize()
        with pytest.raises(McpSamplingDisabledError):
            await client.create_message({"messages": []})

    @pytest.mark.asyncio
    async def test_sampling_requires_server_support(self) -> None:
        server = FakeMcpServer(capabilities=ServerCapabilities(sampling=False))
        transport = FakeTransport(server)
        client = McpClient(transport, sampling_enabled=True)
        await client.initialize()
        with pytest.raises(McpClientError, match="does not support"):
            await client.create_message({"messages": []})

    @pytest.mark.asyncio
    async def test_sampling_works_when_enabled_and_offered(self) -> None:
        server = FakeMcpServer(capabilities=ServerCapabilities(sampling=True))
        transport = FakeTransport(server)
        client = McpClient(
            transport,
            client_capabilities=ClientCapabilities(sampling=True),
            sampling_enabled=True,
        )
        await client.initialize()
        result = await client.create_message({"messages": []})
        assert "model" in result


# ── Capability negotiation ────────────────────────────────────────


class TestCapabilityNegotiation:
    def test_full_negotiation(self) -> None:
        client = ClientCapabilities(
            tools=True, resources=True, prompts=True, roots=True, tasks=True
        )
        server = ServerCapabilities(
            tools=True, resources=True, prompts=True, roots=True, tasks=True
        )
        result = negotiate_capabilities(client, server)
        assert McpCapability.TOOLS in result
        assert McpCapability.RESOURCES in result
        assert McpCapability.PROMPTS in result
        assert McpCapability.ROOTS in result
        assert McpCapability.TASKS in result

    def test_partial_negotiation(self) -> None:
        client = ClientCapabilities(tools=True, resources=True, prompts=True)
        server = ServerCapabilities(tools=True, resources=False, prompts=True)
        result = negotiate_capabilities(client, server)
        assert McpCapability.TOOLS in result
        assert McpCapability.RESOURCES not in result
        assert McpCapability.PROMPTS in result

    def test_sampling_only_when_both_opt_in(self) -> None:
        client_no = ClientCapabilities(sampling=False)
        server_yes = ServerCapabilities(sampling=True)
        result = negotiate_capabilities(client_no, server_yes)
        assert McpCapability.SAMPLING not in result

        client_yes = ClientCapabilities(sampling=True)
        server_no = ServerCapabilities(sampling=False)
        result2 = negotiate_capabilities(client_yes, server_no)
        assert McpCapability.SAMPLING not in result2

        result3 = negotiate_capabilities(client_yes, server_yes)
        assert McpCapability.SAMPLING in result3

    def test_server_from_dict(self) -> None:
        data = {"tools": {}, "resources": {}}
        server = ServerCapabilities.from_dict(data)
        assert server.tools is True
        assert server.resources is True
        assert server.prompts is False

    def test_client_to_dict(self) -> None:
        caps = ClientCapabilities(tools=True, sampling=True, resources=False, prompts=False, roots=False)
        d = caps.to_dict()
        assert "tools" in d
        assert "sampling" in d
        assert "resources" not in d
        assert "prompts" not in d


# ── Mapping ───────────────────────────────────────────────────────


class TestMapping:
    def test_map_tool(self) -> None:
        tool = make_mcp_tool("calculator", "Do math", {"type": "object", "properties": {"x": {"type": "integer"}}})
        td = map_mcp_tool_to_tool_definition(tool, uuid4())
        assert td.tool_name == "calculator"
        assert td.tool_type == "mcp_tool"
        assert td.description == "Do math"
        assert td.input_schema["properties"]["x"]["type"] == "integer"
        assert td.status.value == "discovered"

    def test_map_tool_missing_name_raises(self) -> None:
        with pytest.raises(MappingError, match="name"):
            map_mcp_tool_to_tool_definition({}, uuid4())

    def test_map_resource(self) -> None:
        r = make_mcp_resource("file:///data.csv", "data", "My data", "text/csv")
        rd = map_mcp_resource_to_resource_definition(r, uuid4())
        assert rd.uri == "file:///data.csv"
        assert rd.name == "data"
        assert rd.mime_type == "text/csv"

    def test_map_resource_missing_uri_raises(self) -> None:
        with pytest.raises(MappingError, match="uri"):
            map_mcp_resource_to_resource_definition({}, uuid4())

    def test_map_prompt(self) -> None:
        p = make_mcp_prompt("summarize", "Summarize text", [{"name": "text", "required": True}])
        pd = map_mcp_prompt_to_prompt_definition(p, uuid4())
        assert pd.name == "summarize"
        assert len(pd.arguments) == 1
        assert pd.arguments[0]["name"] == "text"

    def test_map_prompt_missing_name_raises(self) -> None:
        with pytest.raises(MappingError, match="name"):
            map_mcp_prompt_to_prompt_definition({}, uuid4())

    def test_batch_tools(self) -> None:
        tools = [make_mcp_tool(f"t{i}") for i in range(3)]
        result = map_mcp_tools_batch(tools, uuid4())
        assert len(result) == 3
        for i, td in enumerate(result):
            assert td.tool_name == f"t{i}"
