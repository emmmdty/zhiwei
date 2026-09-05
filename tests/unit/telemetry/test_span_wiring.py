"""R2-A：specs/s9 §6 十层 span 埋点接线契约（metadata-only，默认 no-op）。

两层验证：
1. 动态（SDK processor 显式配置，不触网）：能无 DB 构造的代表 seam
   （policy authorize 直调、memory retrieve、tool gateway invoke、api 中间件）——
   断言 span 名取自 SpanNames 常量、属性 metadata-only（正文键不出现、植入
   canary 不幸存）。tracer 注入经 monkeypatch opentelemetry.trace.get_tracer：
   OTel 全局 provider 一旦 set 即不可再换（set_tracer_provider 二次调用被忽略），
   patch 是唯一能在测试后确定性还原的方式，避免污染同进程其它测试的 no-op
   默认假设。
2. 静态（沿用 tests/unit/evals/test_external_adapters.py 的 source-inspection
   模式）：需要 DB/Temporal 才能构造的 seam（run/task/retrieval/evidence/eval/
   approval）——断言调用点存在且使用 SpanNames 常量（名字是契约，不随调用点
   漂移）。
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from zhiwei.telemetry.redaction import DEFAULT_BODY_KEYS
from zhiwei.telemetry.traces import SpanNames

NOW = datetime(2026, 9, 6, tzinfo=UTC)
CANARY = "r2a-canary-7f3d"


def _recording_tracer(monkeypatch: Any) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = SdkTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer",
        lambda name=None, *args: provider.get_tracer(name or "test"),
    )
    return exporter


def _attributes_of(exporter: InMemorySpanExporter, name: str) -> dict[str, Any]:
    spans = [span for span in exporter.get_finished_spans() if span.name == name]
    assert len(spans) == 1, f"expected exactly one {name} span, got {len(spans)}"
    return dict(spans[0].attributes or {})


def _assert_metadata_only(exporter: InMemorySpanExporter) -> None:
    for span in exporter.get_finished_spans():
        attributes = span.attributes or {}
        for key, value in attributes.items():
            assert key not in DEFAULT_BODY_KEYS, f"body key {key!r} leaked into span"
            assert CANARY not in str(key)
            assert CANARY not in str(value), f"canary survived in {key!r}"


# --------------------------------------------------------------------- policy


def _policy_doc() -> dict[str, Any]:
    organization_id = "00000000-0000-0000-0000-000000000001"
    return {
        "organization_id": organization_id,
        "workspace_id": None,
        "actor": {
            "principal_id": "00000000-0000-0000-0000-0000000000a1",
            "kind": "user",
            "roles": [
                {
                    "name": "org_owner",
                    "scope": "org",
                    "organization_id": organization_id,
                    "workspace_id": None,
                }
            ],
        },
        "resource": {"type": "org", "id": str(uuid4()), "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": None,
        "risk": None,
        "delegation": [],
        "resource_context": {},
        "context": {
            "now": "2026-09-06T00:00:00Z",
            "classification_ceiling": None,
            "requires_delegation": False,
        },
    }


def _opa_response(allow: bool, reason: str) -> dict[str, Any]:
    return {
        "decision_id": "d1",
        "result": {"allow": allow, "reason": reason},
        "provenance": {
            "version": "1.19.0",
            "bundles": {"/bundle.tar.gz": {"revision": "rev-1"}},
        },
    }


@pytest.mark.asyncio
class TestPolicySpan:
    async def test_authorize_records_metadata_only_span(self, monkeypatch: Any) -> None:
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=_opa_response(True, "allowed:matrix"), request=request
            )

        client = OPAClient(
            "http://opa.test:8181",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        enforcer = PolicyEnforcer(client)
        exporter = _recording_tracer(monkeypatch)
        decision = await enforcer.authorize(_policy_doc())
        assert decision.allow is True
        attributes = _attributes_of(exporter, SpanNames.POLICY)
        assert attributes["policy_type"] == "org"
        assert attributes["decision"] == "allow"
        _assert_metadata_only(exporter)

    async def test_deny_decision_is_visible_not_payload(self, monkeypatch: Any) -> None:
        from zhiwei.policy.client import OPAClient
        from zhiwei.policy.enforcement import PolicyEnforcer

        # canary 植入在 decision reason（PEP 结果对象的真实字段）：reason 属于
        # 判定 payload，绝不能以任何形态进入 span 属性。
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_opa_response(False, f"default_deny:{CANARY}"),
                request=request,
            )

        client = OPAClient(
            "http://opa.test:8181",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        enforcer = PolicyEnforcer(client)
        exporter = _recording_tracer(monkeypatch)
        decision = await enforcer.authorize(_policy_doc())
        assert decision.allow is False
        assert CANARY in decision.reason
        attributes = _attributes_of(exporter, SpanNames.POLICY)
        assert attributes["decision"] == "deny"
        _assert_metadata_only(exporter)


# --------------------------------------------------------------------- memory


@pytest.mark.asyncio
class TestMemorySpan:
    async def test_retrieve_records_run_and_count(self, monkeypatch: Any) -> None:
        from zhiwei.identity.domain import PrincipalKind
        from zhiwei.workflows.activities.memory import MemoryActivity, MemoryActivityInput

        exporter = _recording_tracer(monkeypatch)
        activity = MemoryActivity()
        output = await activity.execute(
            MemoryActivityInput(
                run_id="11111111-1111-1111-1111-111111111111",
                task_id="task-1",
                attempt_no=1,
                organization_id=str(uuid4()),
                workspace_id=str(uuid4()),
                principal_id=str(uuid4()),
                principal_kind=PrincipalKind.USER,
                action="retrieve",
                # query 文本与正文键名（prompt）都是 canary 载体：检索输入不得
                # 以任何形态进入 span 属性。
                query={"text": CANARY, "top_k": 5, "prompt": CANARY},
                filters={},
            )
        )
        assert output.status == "completed"
        attributes = _attributes_of(exporter, SpanNames.MEMORY)
        assert attributes["run_id"] == "11111111-1111-1111-1111-111111111111"
        assert attributes["action"] == "retrieve"
        assert isinstance(attributes["record_count"], int)
        _assert_metadata_only(exporter)


# ---------------------------------------------------------------------- tool


class _FakeRunnerResponse:
    def __init__(self) -> None:
        self.status = "completed"
        self.output = {"result_key": CANARY}
        self.error: str | None = None


class _FakeRunnerClient:
    async def execute(self, _request: object) -> _FakeRunnerResponse:
        return _FakeRunnerResponse()


class _FakeInvocationRepo:
    def __init__(self) -> None:
        self.invocations: dict[UUID, Any] = {}

    def store(self, invocation: Any) -> None:
        self.invocations[invocation.id] = invocation

    def get(self, invocation_id: UUID) -> Any:
        return self.invocations.get(invocation_id)

    def get_by_idempotency_key(self, _key: str) -> Any:
        return None


@pytest.mark.asyncio
class TestToolSpan:
    async def test_invoke_records_digest_and_status(self, monkeypatch: Any) -> None:
        from tests.fixtures.policy_fake import FakePolicyEnforcer
        from zhiwei.capabilities.connections import Connection, SubjectMode
        from zhiwei.capabilities.credential_bindings import (
            CredentialBinding,
            CredentialType,
        )
        from zhiwei.capabilities.domain import CapabilityStatus, CapabilityVersion
        from zhiwei.capabilities.tool_gateway import ToolGateway
        from zhiwei.secrets.base import SecretRef

        exporter = _recording_tracer(monkeypatch)
        organization_id, workspace_id = uuid4(), uuid4()
        connection = Connection(
            id=uuid4(),
            organization_id=organization_id,
            workspace_id=workspace_id,
            provider_version_id=uuid4(),
            subject_mode=SubjectMode.WORKSPACE_SERVICE,
            created_at=NOW,
            updated_at=NOW,
        )
        credential = CredentialBinding(
            id=uuid4(),
            connection_id=connection.id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            credential_type=CredentialType.API_KEY,
            secret_ref=SecretRef(value="opaque-handle"),
            created_at=NOW,
            updated_at=NOW,
        )
        capability = CapabilityVersion(
            id=uuid4(),
            capability_type="tool",
            name="fixture-tool",
            version=1,
            status=CapabilityStatus.PUBLISHED,
            created_at=NOW,
            updated_at=NOW,
        )
        gateway = ToolGateway(
            FakePolicyEnforcer(allow=True),
            _FakeRunnerClient(),
            _FakeInvocationRepo(),
            connection_registry={connection.id: connection},
            credential_registry={credential.id: credential},
            capability_registry={capability.id: capability},
        )
        invocation = await gateway.invoke(
            organization_id=organization_id,
            workspace_id=workspace_id,
            run_id="run-r2a",
            task_id="task-r2a",
            attempt_no=1,
            tool_name="fixture-tool",
            tool_version_id=capability.id,
            provider_version_id=connection.provider_version_id,
            connection_id=connection.id,
            credential_binding_id=credential.id,
            principal_id=uuid4(),
            agent_identity_id=None,
            input_args={"query": CANARY, "prompt": CANARY},
            policy_input={
                "organization_id": str(organization_id),
                "workspace_id": str(workspace_id),
                "actor": {"principal_id": str(uuid4()), "kind": "user", "roles": []},
                "resource": {"type": "tool", "id": str(capability.id), "version": "v1"},
                "action": "invoke",
                "purpose": "general",
                "classification": None,
                "risk": None,
                "delegation": [],
                "resource_context": {},
                "context": {
                    "now": "2026-09-06T00:00:00Z",
                    "classification_ceiling": None,
                    "requires_delegation": False,
                },
            },
        )
        assert invocation.status.value == "completed"
        attributes = _attributes_of(exporter, SpanNames.TOOL)
        assert attributes["run_id"] == "run-r2a"
        assert attributes["status"] == "completed"
        assert attributes["input_digest"].startswith("sha256:")
        _assert_metadata_only(exporter)


# ------------------------------------------------- api request span (FastAPI)


@pytest.mark.asyncio
class TestApiRequestSpan:
    async def test_request_span_carries_w3c_context_and_status(
        self, monkeypatch: Any
    ) -> None:
        from fastapi import FastAPI
        from httpx2 import ASGITransport, AsyncClient
        from zhiwei.telemetry.fastapi import trace_context_middleware

        exporter = _recording_tracer(monkeypatch)
        app = FastAPI()

        @app.middleware("http")
        async def trace(request: Any, call_next: Any) -> Any:
            return await trace_context_middleware(request, call_next)

        @app.get("/api/v1/fixture")
        async def fixture_endpoint() -> dict[str, str]:
            from zhiwei.telemetry.traces import start_span

            with start_span(SpanNames.MODEL, {"run_id": "r-1"}):
                return {"ok": "true"}

        transport = ASGITransport(app=app)
        traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/fixture", headers={"traceparent": traceparent}
            )
        assert response.status_code == 200, response.text
        spans = {span.name: span for span in exporter.get_finished_spans()}
        assert set(spans) == {SpanNames.API, SpanNames.MODEL}
        api_span, model_span = spans[SpanNames.API], spans[SpanNames.MODEL]
        # W3C 传播：请求 span 续接上游 trace，端点内子 span 挂接在其下。
        assert f"{api_span.context.trace_id:032x}" == "0af7651916cd43dd8448eb211c80319c"
        assert model_span.parent is not None
        assert model_span.parent.span_id == api_span.context.span_id
        api_attributes = dict(api_span.attributes or {})
        assert api_attributes["http.method"] == "GET"
        assert api_attributes["http.route"] == "/api/v1/fixture"
        assert api_attributes["http.status_code"] == 200
        _assert_metadata_only(exporter)


# ----------------------------------------------- static seam map (DB/Temporal)


_STATIC_SEAMS = (
    ("zhiwei.telemetry.fastapi", "API"),
    ("zhiwei.persistence.run_commands", "RUN"),
    ("zhiwei.workflows.activities.runtime", "TASK"),
    ("zhiwei.workflows.activities.knowledge", "RETRIEVAL"),
    ("zhiwei.workflows.activities.memory", "MEMORY"),
    ("zhiwei.workflows.activities.model", "MODEL"),
    ("zhiwei.workflows.activities.runtime", "APPROVAL"),
    ("zhiwei.evidence.verifier", "EVIDENCE"),
    ("zhiwei.evals.runner", "EVAL"),
)


class TestSpanSeamWiring:
    """需要 DB/Temporal 才能构造的 seam：静态断言调用点存在且用 SpanNames 常量。"""

    @pytest.mark.parametrize(("module_path", "constant"), _STATIC_SEAMS)
    def test_seam_starts_span_with_constant(
        self, module_path: str, constant: str
    ) -> None:
        import importlib

        module = importlib.import_module(module_path)
        source = inspect.getsource(module)
        pattern = re.compile(rf"start_span\(\s*SpanNames\.{constant}\b")
        assert pattern.search(source) is not None, (
            f"{module_path} must start a {constant} span at its seam"
        )

    def test_app_wires_trace_middleware(self) -> None:
        import importlib

        source = inspect.getsource(importlib.import_module("zhiwei.app"))
        assert "trace_context_middleware" in source

    def test_run_span_carries_agent_identity(self) -> None:
        # run seam 的 agent 身份在提交期只能以 graph digest 表达
        # （agent_version_id 在 release 流程才绑定到 Run 行）。
        import importlib

        source = inspect.getsource(
            importlib.import_module("zhiwei.persistence.run_commands")
        )
        assert "agent_graph_digest" in source

    def test_all_eleven_domains_are_wired(self) -> None:
        wired = {constant for _module, constant in _STATIC_SEAMS} | {"TOOL", "POLICY"}
        assert wired == {
            SpanNames.API,
            SpanNames.RUN,
            SpanNames.TASK,
            SpanNames.MODEL,
            SpanNames.RETRIEVAL,
            SpanNames.MEMORY,
            SpanNames.TOOL,
            SpanNames.POLICY,
            SpanNames.APPROVAL,
            SpanNames.EVIDENCE,
            SpanNames.EVAL,
        }
