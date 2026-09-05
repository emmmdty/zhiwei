"""S9-T6 RED：trace schema 契约（B 档实现级测试，GREEN 后锁定）。

specs/s9 §6：W3C trace + OTel spans for API/Run/Task/model/retrieval/memory/tool/
policy/approval/evidence/eval；固定 GenAI semconv revision；默认 metadata only。
默认路径必须是 no-op/non-recording——测试进程不开任何 socket、不向任何后端导出。
"""

from __future__ import annotations

import re

import pytest
from zhiwei.telemetry.traces import (
    GENAI_SEMCONV_REVISION,
    SPAN_DOMAINS,
    SpanNames,
    TracerProviderFactory,
    TracingConfig,
    extract_trace_context,
    inject_trace_context,
    metadata_attributes,
    start_span,
)

# https://github.com/open-telemetry/semantic-conventions/tree/v1.36.0/docs/gen-ai
_SEMCONV_REVISION_RE = re.compile(r"^genai-semconv-\d+\.\d+\.\d+$")

_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


class TestSpanSchema:
    def test_genai_semconv_revision_is_pinned(self) -> None:
        assert GENAI_SEMCONV_REVISION == "genai-semconv-1.36.0"
        assert _SEMCONV_REVISION_RE.fullmatch(GENAI_SEMCONV_REVISION)

    def test_span_name_constants_cover_all_domains(self) -> None:
        names = {
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
        assert names == {f"zhiwei.{domain}" for domain in SPAN_DOMAINS}
        for name in names:
            assert name.startswith("zhiwei.")

    def test_default_span_is_non_recording(self) -> None:
        # 未显式配置时绝不产生 recording span：默认遥测路径零副作用。
        with start_span(SpanNames.MODEL, {"run_id": "r-1"}) as span:
            assert not span.is_recording()
            assert not span.get_span_context().trace_id

    def test_metadata_attributes_strip_body_keys(self) -> None:
        attributes = metadata_attributes(
            {"run_id": "r-1", "prompt": "secret body", "messages": [{"role": "user"}]}
        )
        assert attributes == {"run_id": "r-1"}

    def test_metadata_attributes_non_scalars_become_canonical_json(self) -> None:
        # 非标量 metadata（如 routing 决策）降为 canonical JSON 字符串：
        # OTel 属性类型安全，且 body key 剥离后结构信息仍以 metadata 形态保留。
        attributes = metadata_attributes({"routing_decision": {"endpoint": "e-1"}})
        assert attributes == {"routing_decision": '{"endpoint":"e-1"}'}


class TestW3CPropagation:
    def test_extract_reads_traceparent(self) -> None:
        span_context = extract_trace_context({"traceparent": _TRACEPARENT})
        assert span_context is not None
        assert f"{span_context.trace_id:032x}" == _TRACE_ID

    def test_inject_round_trips_extracted_context(self) -> None:
        span_context = extract_trace_context({"traceparent": _TRACEPARENT})
        carrier: dict[str, str] = {}
        inject_trace_context(carrier, span_context=span_context)
        assert carrier["traceparent"] == _TRACEPARENT

    def test_inject_without_context_is_noop(self) -> None:
        carrier: dict[str, str] = {}
        inject_trace_context(carrier)
        assert carrier == {}

    def test_extract_from_carrier_without_traceparent_is_none(self) -> None:
        assert extract_trace_context({}) is None


class TestTracerProviderFactory:
    def test_default_provider_is_noop(self) -> None:
        # 无显式配置 → NoOp：SDK provider 只在显式部署配置下安装。
        from opentelemetry.trace import NoOpTracerProvider

        assert isinstance(TracerProviderFactory.create(), NoOpTracerProvider)

    def test_explicit_config_installs_sdk_provider(self) -> None:
        # 显式配置走 SDK；无 processor 即无导出目标（测试不开 socket）。
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProviderFactory.create(
            TracingConfig(exporter="otlp-http", endpoint="http://127.0.0.1:1")
        )
        assert isinstance(provider, TracerProvider)

    def test_explicit_config_requires_endpoint(self) -> None:
        with pytest.raises(ValueError):
            TracerProviderFactory.create(TracingConfig(exporter="otlp-http", endpoint=""))

    def test_unknown_exporter_refused(self) -> None:
        # fail closed：未知 exporter 不是「静默降级为 no-op」。
        with pytest.raises(ValueError):
            TracerProviderFactory.create(
                TracingConfig(exporter="carrier-pigeon", endpoint="http://127.0.0.1:1")
            )

    def test_factory_never_mutates_global_provider(self) -> None:
        from opentelemetry import trace

        before = trace.get_tracer_provider()
        TracerProviderFactory.create(
            TracingConfig(exporter="otlp-http", endpoint="http://127.0.0.1:1")
        )
        assert trace.get_tracer_provider() is before
