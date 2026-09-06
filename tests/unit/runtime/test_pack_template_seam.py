"""S10 fix-A RED：Planner port 的 pack-template 解析分支（generic seam）。

事实源：R1 product-engineer REJECT D4——三个 pack App 的 member-facing run 必须
经生产路径创建。设计裁决（设计方 pre-made）：runtime 层只看见 generic 类型
（template_id → PlannedRun）；template→pack 的 App 名映射住在评测资产层
（src/zhiwei/evals/pack_templates.py），经组合根依赖注入进入 FixturePlanner——
冻结架构扫描（tests/architecture/test_app_boundaries.py）因此保持零 App 名字面量。

契约：
- 注册的 pack 模板 → 计划源产出的 PlannedRun（短路 fixture 模板分支）；
- 未注册模板 → 回落 fixture 模板分支（unknown → PlannerError，行为不变）；
- 计划源异常原样上抛（fail closed——pack 解析失败绝不静默降级为 fixture 规划）；
- 计划源可 pin 执行 task queue（pack fixture 绑定的执行面），None = 调用方默认。
"""

from __future__ import annotations

import pytest

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.runtime.planner import FixturePlanner, PlanIntent, PlannerError, PlannedRun


def _single_node_graph(task_id: str, task_type: str) -> TaskGraph:
    return TaskGraph(
        nodes={
            task_id: TaskGraphNode(
                task_id=task_id, task_type=task_type, required_capability="fixture"
            )
        },
        edges={},
    )


class _FakePackSource:
    """generic 计划源替身：runtime 层只依赖 resolve_pack_plan 的形状。"""

    def __init__(self, plans: dict[str, PlannedRun]) -> None:
        self._plans = plans
        self.asked: list[str] = []

    def resolve_pack_plan(self, template_id: str) -> PlannedRun | None:
        self.asked.append(template_id)
        if template_id == "broken-pack":
            raise PlannerError(
                "pack template 'broken-pack' not resolvable: conformance issues"
            )
        return self._plans.get(template_id)


class TestFixturePlannerPackBranch:
    def test_registered_pack_template_short_circuits_fixture_branch(self) -> None:
        planned = PlannedRun(
            graph=_single_node_graph("pack-node", "Fixture"),
            task_type_by_task={"pack-node": "Fixture"},
            task_queue="zhiwei-pack-example",
        )
        planner = FixturePlanner(pack_plans=_FakePackSource({"example-pack": planned}))

        result = planner.plan(PlanIntent(template="example-pack"))

        assert result is planned, "pack 计划必须原样短路，不得重走 fixture 分支"
        assert result.task_queue == "zhiwei-pack-example"

    def test_unregistered_template_falls_through_to_fixture_branch(self) -> None:
        planner = FixturePlanner(pack_plans=_FakePackSource({}))

        planned = planner.plan(PlanIntent(template="single-fixture"))

        assert planned.graph.nodes["intake"].task_type == "Fixture"

    def test_unknown_template_keeps_unknown_fixture_error(self) -> None:
        planner = FixturePlanner(pack_plans=_FakePackSource({}))

        with pytest.raises(PlannerError, match="unknown fixture template"):
            planner.plan(PlanIntent(template="mystery-template"))

    def test_resolution_failure_propagates_fail_closed(self) -> None:
        planner = FixturePlanner(pack_plans=_FakePackSource({}))

        # 计划源 raise ≠ 未注册：解析失败必须原样上抛，绝不回落 fixture 规划
        with pytest.raises(PlannerError, match="broken-pack"):
            planner.plan(PlanIntent(template="broken-pack"))

    def test_pack_source_is_queried_only_for_explicit_templates(self) -> None:
        source = _FakePackSource({})
        planner = FixturePlanner(pack_plans=source)

        planner.plan(PlanIntent(template="approval-chain"))

        assert source.asked == ["approval-chain"]

    def test_default_planner_has_no_pack_source(self) -> None:
        planner = FixturePlanner()

        with pytest.raises(PlannerError, match="unknown fixture template"):
            planner.plan(PlanIntent(template="example-pack"))
