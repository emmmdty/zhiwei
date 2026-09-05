"""S3-T6 Model Activity for Temporal: model I/O through Activity boundary.

Per S3 plan Task 6:
- Execute model I/O through Activity
- Commit through Attempt events
- Plan/Analyze/Synthesize route through model actions
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.models.first_use import AuditedEndpointResolver
from zhiwei.models.router import ModelRouter, RoutingRequest
from zhiwei.models.usage import (
    TokenUsage,
    compute_weighted_tokens,
)
from zhiwei.persistence.model_first_use import CanonicalEndpointFirstUseSink
from zhiwei.persistence.tenant import TenantContext
from zhiwei.runtime.handlers.base import TaskInput
from zhiwei.runtime.handlers.model_actions import (
    AnalyzeModelHandler,
    PlanModelHandler,
    SynthesizeModelHandler,
)
from zhiwei.telemetry.traces import (
    GENAI_SEMCONV_REVISION,
    SpanNames,
    start_span,
)

logger = logging.getLogger(__name__)

# span 属性：标注本平台 span 遵循的 GenAI semconv 快照（telemetry/traces.py 冻结值）。
GENAI_SEMCONV_REVISION_ATTRIBUTE = "zhiwei.genai_semconv_revision"

_MODEL_HANDLERS = {
    "Plan": PlanModelHandler(),
    "Analyze": AnalyzeModelHandler(),
    "Synthesize": SynthesizeModelHandler(),
}


def build_audited_endpoint_resolver(
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
    *,
    endpoints_path: Path,
    env_overrides: Mapping[str, str] | None = None,
    declared_by: str = "operator:env-override",
) -> AuditedEndpointResolver:
    """组合根接线（ADR-011 §6）：模型请求路径的默认 endpoint 解析入口。

    写入器用 canonical 落账路径实现（persistence.model_first_use）——unverified
    endpoint 首次解析即同事务写 canonical event + audit。模型 egress 路径接入
    ModelActivity 时经本工厂构造 resolver，不得绕过留痕直接解析 endpoint。
    """
    return AuditedEndpointResolver(
        CanonicalEndpointFirstUseSink(sessions, context),
        endpoints_path=endpoints_path,
        env_overrides=env_overrides,
        declared_by=declared_by,
    )


@dataclass
class ModelActivityInput:
    """Input for a model activity execution.

    Encapsulates the routing request, task type, and the raw input
    that will be forwarded to the model handler.
    """

    run_id: str
    task_id: str
    attempt_id: str
    task_type: str
    input_values: dict[str, Any] = field(default_factory=dict)
    routing_request: dict[str, Any] = field(default_factory=dict)
    actor_ref: str = "agent-runtime:worker"


@dataclass
class ModelActivityOutput:
    """Output from a model activity execution.

    Carries the handler output, routing decision, and the weighted
    token cost for the call.
    """

    task_id: str
    attempt_id: str
    status: str  # completed | refused | routed_failed
    output_values: dict[str, Any] = field(default_factory=dict)
    routing_decision: dict[str, Any] = field(default_factory=dict)
    weighted_tokens: float = 0.0
    error: str | None = None


class ModelActivity:
    """Temporal activity boundary for model I/O.

    Orchestrates:
    1. Route selection via ModelRouter
    2. Handler dispatch via TaskHandler
    3. Usage tracking via weighted_tokens
    4. Attempt event generation context
    """

    __slots__ = ("_router",)

    def __init__(self, router: ModelRouter | None = None) -> None:
        self._router = router or ModelRouter()

    def execute(self, input: ModelActivityInput) -> ModelActivityOutput:
        """Execute a model activity: route → handle → track usage.

        Returns ModelActivityOutput with the handler result, routing
        decision, and weighted token cost.
        """
        # S9-T6 唯一埋点：model 域 span（metadata only）。默认 NoOp provider 下
        # 零副作用；operator 配置 exporter 后 span 经 W3C context 关联 run/task。
        # 属性不经 metadata_attributes 预包装：start_span 统一在 telemetry 层做
        # body key 剥离 + 非标量降级，调用点再包一层是重复处理。
        with start_span(
            SpanNames.MODEL,
            {
                "run_id": input.run_id,
                "task_id": input.task_id,
                "attempt_id": input.attempt_id,
                "task_type": input.task_type,
                GENAI_SEMCONV_REVISION_ATTRIBUTE: GENAI_SEMCONV_REVISION,
            },
        ):
            return self._execute(input)

    def _execute(self, input: ModelActivityInput) -> ModelActivityOutput:
        routing_request = self._build_routing_request(input)
        decision = self._router.route(routing_request)

        if decision.is_rejected:
            return ModelActivityOutput(
                task_id=input.task_id,
                attempt_id=input.attempt_id,
                status="routed_failed",
                routing_decision=decision.model_dump(),
                error=decision.rejection_reason,
            )

        handler = _MODEL_HANDLERS.get(input.task_type)
        if handler is None:
            return ModelActivityOutput(
                task_id=input.task_id,
                attempt_id=input.attempt_id,
                status="routed_failed",
                routing_decision=decision.model_dump(),
                error=f"No model handler for task type '{input.task_type}'",
            )

        task_input = TaskInput(
            task_id=input.task_id,
            attempt_id=__import__("uuid").UUID(input.attempt_id),
            input_values=input.input_values,
        )

        try:
            handler.validate_input(task_input)
            output = handler.execute(task_input)
            handler.validate_output(output)
        except Exception as exc:
            return ModelActivityOutput(
                task_id=input.task_id,
                attempt_id=input.attempt_id,
                status="routed_failed",
                routing_decision=decision.model_dump(),
                error=str(exc),
            )

        usage = TokenUsage(
            new_input_tokens=input.input_values.get("new_input_tokens", 0),
            cache_read_tokens=input.input_values.get("cache_read_tokens", 0),
            output_tokens=input.input_values.get("output_tokens", 0),
        )
        weighted = compute_weighted_tokens(usage)

        return ModelActivityOutput(
            task_id=input.task_id,
            attempt_id=input.attempt_id,
            status="completed",
            output_values=dict(output.output_values),
            routing_decision=decision.model_dump(),
            weighted_tokens=weighted,
        )

    def _build_routing_request(
        self, input: ModelActivityInput
    ) -> RoutingRequest:
        """Build a RoutingRequest from the activity input."""
        rr = input.routing_request
        return RoutingRequest(
            candidates=rr.get("candidates", []),
            endpoints=rr.get("endpoints", {}),
            required_capabilities=set(rr.get("required_capabilities", [])),
            context_items=rr.get("context_items", ()),
            spend_guard_enabled=rr.get("spend_guard_enabled", False),
            spend_limit_usd=rr.get("spend_limit_usd"),
            quality_scores=rr.get("quality_scores", {}),
            latency_scores=rr.get("latency_scores", {}),
            data_classification=rr.get("data_classification", "public"),
        )
