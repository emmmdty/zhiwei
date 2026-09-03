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


class Planner(Protocol):
    """Run 创建的正式入口（S3+ 模型驱动 planner 经此注册）。"""

    def plan(self, intent: PlanIntent) -> PlannedRun: ...


class FixturePlanner:
    """S2 的 fixture planner：sandbox 图或固定模板，零模型调用。

    模板语义（评审确定性优先）：单一 fixture 模板产出单任务图；后续任务
    类型由 TaskHandlerRegistry 的注册表驱动（registry 校验在 worker 侧）。
    """

    def plan(self, intent: PlanIntent) -> PlannedRun:
        if intent.agent_version_ref:
            raise PlannerError(
                "agent-version graph planning arrives with S9 publish service; "
                "sandbox graph must be passed explicitly for now"
            )
        template = intent.template
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
