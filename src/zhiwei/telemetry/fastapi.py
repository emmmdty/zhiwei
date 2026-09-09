"""S9 §6：API 请求路径的 W3C traceparent 提取与 api 请求 span。

中间件只做三件事：从 traceparent 头提取上游 trace context（缺失/无效 → 本地
根 trace）、在请求期间保持 api span 为 current（端点/依赖里的子 span 经
context 自动挂接）、把方法/路由模板/状态码写进属性。属性严格 metadata-only：
绝不记录 header/body——认证 cookie、Authorization、tracestate 等请求头一律
不进 span（redaction 纪律在 span 面同样成立）。

默认 no-op：进程未显式安装 SDK provider 时 start_span 产生 non-recording
span，本中间件零副作用、不改响应、不吞异常（start_span 记录异常后原样上抛）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan

from zhiwei.telemetry.traces import SpanNames, extract_trace_context, start_span


async def trace_context_middleware(
    request: Any, call_next: Callable[[Any], Awaitable[Any]]
) -> Any:
    """请求级 span：续接 W3C traceparent（若有）并标注 http 方法/路由/状态。"""
    span_context = extract_trace_context(
        {"traceparent": request.headers.get("traceparent", "")}
    )
    # use_span 只把上游 context 挂进当前 context 让新 span 认父，不产生记录
    # 行为；None（无有效 traceparent）时保持本地根 trace。
    upstream = (
        trace.use_span(NonRecordingSpan(span_context), end_on_exit=False)
        if span_context is not None
        else nullcontext()
    )
    with upstream, start_span(
        SpanNames.API,
        {"http.method": request.method, "http.path": request.url.path},
    ) as span:
        response = await call_next(request)
        # 路由模板在路由匹配后才可得（middleware 先于路由执行）：命中路由
        # 时以模板覆盖原始路径，未命中保留原始路径——模板才是跨请求稳定
        # 的低基数标识。
        route = request.scope.get("route")
        route_path = getattr(route, "path", None)
        if route_path:
            span.set_attribute("http.route", route_path)
        span.set_attribute("http.status_code", response.status_code)
        return response
