"""S9-T6：W3C tracecontext 传播与 span schema（specs/s9 §6）。

默认 no-op：进程不显式配置 TracingConfig 时，start_span 产生 non-recording span、
工厂返回 NoOpTracerProvider——测试/未部署观测后端的环境零副作用、零 socket。
只有 operator 显式提供 exporter 配置时才安装 SDK provider（fail closed：未知
exporter 报错，不静默降级为 no-op——「静默降级」会让 operator 误以为观测已生效）。

metadata 纪律：span 属性经 metadata_attributes（body key 剥离 + 非标量降为
canonical JSON）后才可进入 span；span 名常量覆盖 specs/s9 §6 的全部域。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.propagate import get_global_textmap
from opentelemetry.propagators.textmap import default_setter
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, TracerProvider

from zhiwei.contracts.canonical import canonical_json
from zhiwei.telemetry.redaction import metadata_only_view

# 冻结的 GenAI 语义约定 revision：span 属性名（gen_ai.*）以该版本文档为准，升级
# 须显式换值并复核全部 gen_ai 属性。来源：
# https://github.com/open-telemetry/semantic-conventions/tree/v1.36.0/docs/gen-ai
# （该 revision 下 GenAI conventions 状态为 Development，属性名以此快照冻结）。
GENAI_SEMCONV_REVISION = "genai-semconv-1.36.0"

_TRACER_INSTRUMENTATION = "zhiwei.telemetry"


class SpanNames:
    """span 名常量（specs/s9 §6 全部域）；名字是契约，不随调用点漂移。"""

    API = "zhiwei.api"
    RUN = "zhiwei.run"
    TASK = "zhiwei.task"
    MODEL = "zhiwei.model"
    RETRIEVAL = "zhiwei.retrieval"
    MEMORY = "zhiwei.memory"
    TOOL = "zhiwei.tool"
    POLICY = "zhiwei.policy"
    APPROVAL = "zhiwei.approval"
    EVIDENCE = "zhiwei.evidence"
    EVAL = "zhiwei.eval"


SPAN_DOMAINS = (
    "api",
    "run",
    "task",
    "model",
    "retrieval",
    "memory",
    "tool",
    "policy",
    "approval",
    "evidence",
    "eval",
)

_SCALAR_TYPES = (bool, int, float, str)


@dataclass(frozen=True)
class TracingConfig:
    """显式观测配置：无值 = 不安装 SDK（默认 no-op）。"""

    exporter: str
    endpoint: str


_EXPORTERS = ("otlp-http", "otlp-grpc")


def metadata_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """metadata-only 的 span 属性视图：剥正文键，非标量降为 canonical JSON。"""
    stripped = metadata_only_view(attributes or {})
    return {
        key: value if isinstance(value, _SCALAR_TYPES) else canonical_json(value).decode()
        for key, value in stripped.items()
    }


@contextmanager
def start_span(
    name: str, attributes: Mapping[str, Any] | None = None
) -> Iterator[Span]:
    """在当前 provider 上开 span；默认 provider 是 NoOp → non-recording。

    异常记录进 span 后原样上抛：观测失败不影响业务语义，也不吞异常。
    """
    tracer = trace.get_tracer(_TRACER_INSTRUMENTATION)
    with tracer.start_as_current_span(
        name, attributes=metadata_attributes(attributes)
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            raise


def extract_trace_context(carrier: Mapping[str, str]) -> SpanContext | None:
    """从 W3C tracecontext 载荷提取 SpanContext；无有效 traceparent 返回 None。

    返回值是值对象而非入栈 context：调用方显式决定是否续接（use_span/工厂注入），
    不隐式改写进程级当前 context。
    """
    context = get_global_textmap().extract(dict(carrier))
    span_context = trace.get_current_span(context).get_span_context()
    return span_context if span_context.is_valid else None


def inject_trace_context(
    carrier: dict[str, str], *, span_context: SpanContext | None = None
) -> None:
    """把（显式给定或当前的）trace context 以 W3C 格式写入 carrier。"""
    if span_context is None:
        # 显式传库内 DefaultSetter：上游 Setter 泛型与 dict 载荷存在方差摩擦，
        # 默认推导在此版本会选错 setter（行为由 round-trip 测试覆盖）。
        get_global_textmap().inject(carrier, setter=default_setter)  # type: ignore[arg-type]
        return
    with trace.use_span(NonRecordingSpan(span_context), end_on_exit=False):
        get_global_textmap().inject(carrier, setter=default_setter)  # type: ignore[arg-type]


class TracerProviderFactory:
    """SDK provider 只在显式配置下构造；永不隐式改写全局 provider。"""

    @staticmethod
    def create(config: TracingConfig | None = None) -> TracerProvider:
        if config is None:
            return trace.NoOpTracerProvider()
        if config.exporter not in _EXPORTERS:
            raise ValueError(f"unknown exporter: {config.exporter!r}")
        if not config.endpoint:
            raise ValueError("endpoint is required for the configured exporter")
        # SDK 延迟导入：仅 api 安装的进程不承担 sdk 加载成本。
        from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

        return SdkTracerProvider()

    @staticmethod
    def install(provider: TracerProvider) -> None:
        """显式安装全局 provider（operator 动作；进程内其他路径不得调用）。"""
        trace.set_tracer_provider(provider)
