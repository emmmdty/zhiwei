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

import asyncio
from uuid import uuid4

import pytest
from fixtures.capabilities.malicious.corpus import PROMPT_INJECTION_IGNORE_PREVIOUS
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

    # RED 修订（F-R2-03，P3/T-P3.3 预授权）：原契约 test_empty_isolation_key_always_passes
    # 固化了 fail-open 缺陷——空隔离键不构成任何隔离域（spec s4-capability-hub.md §5:
    # 隔离键 = org/workspace/ProviderVersion/subject/Run 且禁止跨键复用），空键恒过
    # 与仓库 fail-closed 纪律冲突。修订后语义：构造期拒绝空键。
    def test_empty_isolation_key_rejected_at_construction(self) -> None:
        from zhiwei.capabilities.mcp.transport import StdioTransport

        with pytest.raises(TransportError, match="isolation key"):
            StdioTransport(command="echo", isolation_key=())

    def test_empty_isolation_key_rejected_at_construction_http(self) -> None:
        from zhiwei.capabilities.mcp.transport import StreamableHttpTransport

        with pytest.raises(TransportError, match="isolation key"):
            StreamableHttpTransport(
                endpoint_url="https://example.com/mcp", isolation_key=()
            )


class TestNotificationDispatch:
    """F-R9-05：server notification 不再被静默丢弃——读循环/SSB 解析分发到 handler。"""

    def test_stdio_dispatches_notification_to_handlers(self) -> None:
        from zhiwei.capabilities.mcp.transport import StdioTransport

        transport = StdioTransport(
            command="echo", isolation_key=("org1", "ws1", "pv1", "conn1", "run1")
        )
        received: list[tuple[str, dict[str, object]]] = []
        transport.on_notification(lambda method, params: received.append((method, params)))

        transport.process_message(
            {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        )
        assert received == [("notifications/tools/list_changed", {})]

    def test_stdio_notification_without_handler_is_not_fatal(self) -> None:
        from zhiwei.capabilities.mcp.transport import StdioTransport

        transport = StdioTransport(
            command="echo", isolation_key=("org1", "ws1", "pv1", "conn1", "run1")
        )
        transport.process_message(
            {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        )

    def test_stdio_process_message_resolves_pending_response(self) -> None:
        import asyncio

        from zhiwei.capabilities.mcp.transport import JsonRpcResponse, StdioTransport

        transport = StdioTransport(
            command="echo", isolation_key=("org1", "ws1", "pv1", "conn1", "run1")
        )
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        transport._pending[7] = future
        transport.process_message({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
        assert future.result() == JsonRpcResponse(id=7, result={"ok": True})
        loop.close()

    def test_sse_parse_dispatches_interleaved_notifications(self) -> None:
        from zhiwei.capabilities.mcp.transport import StreamableHttpTransport

        transport = StreamableHttpTransport(
            endpoint_url="https://example.com/mcp",
            isolation_key=("org1", "ws1", "pv1"),
        )
        received: list[str] = []
        transport.on_notification(lambda method, params: received.append(method))

        sse = (
            'data: {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}\n'
            'data: {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}\n'
        )
        response = transport._parse_sse_response(sse)
        assert response.id == 1
        assert received == ["notifications/tools/list_changed"]


class TestMcpClientIsolationBinding:
    """F-R2-03：client 与 transport 的隔离键绑定——client 构造期断言、每次请求前复检。"""

    def test_client_rejects_transport_without_isolation_key(self) -> None:
        # 必须真正抵达 McpClient 构造期守卫：FakeTransport 自身会在构造期拒绝
        # 空键，故用绕过 transport 构造期校验的最小替身（防空键守卫被静默删除）。
        class _KeylessTransport(FakeTransport):
            @property
            def isolation_key(self) -> tuple[str, ...]:
                return ()

        server = FakeMcpServer()
        with pytest.raises(McpClientError, match="isolation"):
            McpClient(_KeylessTransport(server))

    def test_client_binds_transport_isolation_key(self) -> None:
        server = FakeMcpServer()
        transport = FakeTransport(server, isolation_key=("org1", "ws1"))
        client = McpClient(transport)
        assert client.isolation_key == ("org1", "ws1")

    @pytest.mark.asyncio
    async def test_client_request_with_mismatched_key_raises(self) -> None:
        server = FakeMcpServer()
        transport = FakeTransport(server, isolation_key=("org1", "ws1"))
        client = McpClient(transport)
        transport.override_isolation_key(("org2", "ws1"))
        with pytest.raises(TransportError, match="Cross-key reuse"):
            await client._send_request(JsonRpcRequest(method="ping", id=1))


class TestMcpClientListChanged:
    """F-R9-05：tools/list_changed → 自动 re-inspection + drift 报告分发。"""

    @pytest.fixture
    def ready(self) -> tuple[McpClient, FakeTransport]:
        server = FakeMcpServer(
            capabilities=ServerCapabilities(tools=True),
            tools=[make_mcp_tool("search", "Search documents")],
        )
        transport = FakeTransport(server)
        client = McpClient(
            transport,
            client_capabilities=ClientCapabilities(tools=True),
        )
        return client, transport

    @pytest.mark.asyncio
    async def test_list_tools_tracks_seen_names(
        self, ready: tuple[McpClient, FakeTransport]
    ) -> None:
        client, _ = ready
        await client.initialize()
        await client.list_tools()
        assert client.seen_tool_names == ["search"]

    @pytest.mark.asyncio
    async def test_list_changed_triggers_reinspection_callback(
        self, ready: tuple[McpClient, FakeTransport]
    ) -> None:
        client, transport = ready
        await client.initialize()
        await client.list_tools()

        received: list[tuple[list[str], list[str], object]] = []
        client.on_tools_list_changed(
            lambda previous, current, report: received.append((previous, current, report))
        )
        transport.server.tools.append(make_mcp_tool("send_email", "Send an email"))

        await transport.dispatch_notification("notifications/tools/list_changed")
        await asyncio.sleep(0)  # 让 re-inspection task 跑完
        assert len(received) == 1
        previous, current, report = received[0]
        assert previous == ["search"]
        assert "send_email" in current
        assert report is not None

    @pytest.mark.asyncio
    async def test_reinspection_scans_new_tools(
        self, ready: tuple[McpClient, FakeTransport]
    ) -> None:
        client, transport = ready
        await client.initialize()
        await client.list_tools()

        transport.server.tools.append(
            make_mcp_tool("evil", PROMPT_INJECTION_IGNORE_PREVIOUS)
        )
        with pytest.raises(Exception, match=r"[Ii]njection"):
            await client._reinspect_tools()


class TestMcpToolNameUniqueness:
    """F-R9-09：(workspace 域内) 工具名唯一性裁决——跨 server 影子化在注册期拒绝。"""

    def test_registry_rejects_shadowing(self) -> None:
        from zhiwei.capabilities.mcp.mapping import McpToolNameRegistry

        registry = McpToolNameRegistry()
        pv_a, pv_b = uuid4(), uuid4()
        registry.register("search", pv_a)
        with pytest.raises(Exception, match="search"):
            registry.register("search", pv_b)

    def test_registry_allows_same_provider_rebind(self) -> None:
        from zhiwei.capabilities.mcp.mapping import McpToolNameRegistry

        registry = McpToolNameRegistry()
        pv = uuid4()
        registry.register("search", pv)
        registry.register("search", pv)

    def test_batch_registers_names_and_conflicts(self) -> None:
        from zhiwei.capabilities.mcp.mapping import McpToolNameRegistry, map_mcp_tools_batch

        pv_a, pv_b = uuid4(), uuid4()
        registry = McpToolNameRegistry()
        map_mcp_tools_batch(
            [make_mcp_tool("t0", "d"), make_mcp_tool("t1", "d")], pv_a, name_registry=registry
        )
        with pytest.raises(Exception, match="t0"):
            map_mcp_tools_batch([make_mcp_tool("t0", "d")], pv_b, name_registry=registry)


class TestMcpResultScanning:
    """F-R2-07：call_tool/read_resource 结果面注入/秘密切描——blocking 即拒绝。"""

    @pytest.fixture
    def ready(self) -> tuple[McpClient, FakeTransport]:
        server = FakeMcpServer(
            capabilities=ServerCapabilities(tools=True, resources=True),
            tools=[make_mcp_tool("search", "Search documents")],
            resources=[make_mcp_resource("file:///data.csv", "data")],
        )
        transport = FakeTransport(server)
        client = McpClient(
            transport,
            client_capabilities=ClientCapabilities(tools=True, resources=True),
        )
        return client, transport

    @pytest.mark.asyncio
    async def test_call_tool_scans_result_content(
        self, ready: tuple[McpClient, FakeTransport]
    ) -> None:
        client, transport = ready
        await client.initialize()
        transport.server.call_results["search"] = {
            "content": [
                {"type": "text", "text": PROMPT_INJECTION_IGNORE_PREVIOUS}
            ],
            "isError": False,
        }
        with pytest.raises(Exception, match=r"[Ii]njection"):
            await client.call_tool("search", {"query": "test"})

    @pytest.mark.asyncio
    async def test_call_tool_benign_result_passes(
        self, ready: tuple[McpClient, FakeTransport]
    ) -> None:
        client, _ = ready
        await client.initialize()
        result = await client.call_tool("search", {"query": "test"})
        assert not result.isError

    @pytest.mark.asyncio
    async def test_read_resource_scans_contents(
        self, ready: tuple[McpClient, FakeTransport]
    ) -> None:
        client, transport = ready
        await client.initialize()
        transport.server.malicious_resource_text = PROMPT_INJECTION_IGNORE_PREVIOUS
        with pytest.raises(Exception, match=r"[Ii]njection"):
            await client.read_resource("file:///data.csv")

    @pytest.mark.asyncio
    async def test_read_resource_benign_passes(
        self, ready: tuple[McpClient, FakeTransport]
    ) -> None:
        client, _ = ready
        await client.initialize()
        result = await client.read_resource("file:///data.csv")
        assert result


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
