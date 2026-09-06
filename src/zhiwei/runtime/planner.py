"""S2-T7：Planner port 与 FixturePlanner。

事实源：specs/s2-agent-runtime.md §3（「FixturePlanner 通过正式 Planner port 输出
TaskGraphPatch，不允许 workflow 中硬编码演示路径」）、S2 plan T7。

Planner port 是「创建 Run」的正式入口：调用方（API/CLI/eval）提交意图，
planner 产出初始 TaskGraph。FixturePlanner 是 S2 的唯一实现——从 sandbox
AgentVersion 的 TaskGraphSchema 或显式 fixture 模板构建图；S3+ 的正式 planner
（模型驱动）经同一 port 注册，不新增路径。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode


class PlannerError(RuntimeError):
    """Planner 无法为给定意图产出合法 TaskGraph。"""


class PlanIntent(BaseModel):
    """一次 Run 创建的规划意图。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_version_ref: str | None = None
    template: str | None = None
    params: dict[str, Any] = {}


class PlannedRun(BaseModel):
    """Planner 的产出：初始图 + 执行参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: TaskGraph
    task_type_by_task: dict[str, str]
    max_task_attempts: int = 3
    continue_as_new_after: int = 1000
    # pack 模板的 fixture 绑定可 pin 独立执行队列（不同 pack 的同名 primitive
    # handler 语义互斥，单一注册表无法同时服务）；None = 调用方默认队列。
    task_queue: str | None = None


class PackPlanSource(Protocol):
    """pack 模板计划源（Planner port 的扩展 seam，组合根注入）。

    runtime 只见 generic 类型：template_id → PlannedRun；template→pack 的
    App 名映射与 fixture 绑定数据住在资产层（src/zhiwei/evals/pack_templates.py），
    不进入本层（冻结架构扫描：Core 不知道任何具体 App）。

    契约：返回 None = 非 pack 模板（调用方回落自有模板分支）；注册但不可解析
    （conformance 失败、fixture 绑定缺失）→ raise PlannerError（fail closed，
    绝不静默降级为其他规划路径）。
    """

    def resolve_pack_plan(self, template_id: str) -> PlannedRun | None: ...


class Planner(Protocol):
    """Run 创建的正式入口（S3+ 模型驱动 planner 经此注册）。"""

    def plan(self, intent: PlanIntent) -> PlannedRun: ...


class FixturePlanner:
    """S2 的 fixture planner：sandbox 图或固定模板，零模型调用。

    模板语义（评审确定性优先）：单一 fixture 模板产出单任务图；后续任务
    类型由 TaskHandlerRegistry 的注册表驱动（registry 校验在 worker 侧）。

    pack_plans（可选，组合根注入）：注册的 pack 模板先经它解析（S10 fix-A），
    命中即短路 fixture 模板分支；解析失败原样上抛，未注册回落既有分支。
    """

    def __init__(self, pack_plans: PackPlanSource | None = None) -> None:
        self._pack_plans = pack_plans

    def plan(self, intent: PlanIntent) -> PlannedRun:
        if intent.agent_version_ref:
            raise PlannerError(
                "agent-version graph planning arrives with S9 publish service; "
                "sandbox graph must be passed explicitly for now"
            )
        template = intent.template
        if template is not None and self._pack_plans is not None:
            resolved = self._pack_plans.resolve_pack_plan(template)
            if resolved is not None:
                return resolved
        if template == "single-fixture":
            graph = TaskGraph(
                nodes={
                    "intake": TaskGraphNode(
                        task_id="intake",
                        task_type="Fixture",
                        required_capability="fixture",
                    )
                },
                edges={},
            )
            return PlannedRun(
                graph=graph,
                task_type_by_task={"intake": "Fixture"},
            )
        if template == "approval-chain":
            graph = TaskGraph(
                nodes={
                    "intake": TaskGraphNode(
                        task_id="intake",
                        task_type="Fixture",
                        required_capability="fixture",
                    ),
                    "review": TaskGraphNode(
                        task_id="review",
                        task_type="RequestApproval",
                        dependencies=("intake",),
                        required_capability="approval",
                    ),
                    "final": TaskGraphNode(
                        task_id="final",
                        task_type="Fixture",
                        dependencies=("review",),
                        required_capability="fixture",
                    ),
                },
                edges={"review": ["intake"], "final": ["review"]},
            )
            return PlannedRun(graph=graph, task_type_by_task={})
        raise PlannerError(f"unknown fixture template: {template!r}")
