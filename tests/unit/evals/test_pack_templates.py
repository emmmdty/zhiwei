"""S10 fix-A RED：pack 模板运行注册表（template_id → pack bundle 执行绑定）。

事实源：R1 REJECT D4 + ADR 纪律——App 名映射是数据，住在评测资产层（本文件所在
的 sanctioned layer）；runtime 只见 generic 类型。Web 绑定（T1 冻结）注册的
templateId 是对齐事实源：ask-v1 / discover-v1 / change-brief。

契约：
- 注册表逐条声明 (template_id, pack_dir, fixture 绑定)；fixture 绑定缺失的注册项
  （discover-v1：pack 声明在库但无任何 fixture handler 资产）解析即拒绝——
  run 在创建期被 machine reason 拒绝，绝不让 worker 侧注册表校验去炸；
- 解析产出 pack 拓扑的 TaskGraph + 经公共 TaskHandlerRegistry 机制的 handler 集
  （change-brief 走 T6 的 pack runtime 机制；ask-v1 走 S6 契约 fixture 注册表）；
- 图与注册表互相印证：registry.validate_completeness 对图任务类型零缺口；
- pack bundle conformance 不干净 → 解析失败（fail closed，继承 T6 语义）。
"""

from __future__ import annotations

import pytest

from zhiwei.runtime.planner import PlannerError


def _source():
    from zhiwei.evals.pack_templates import (
        PackTemplatePlanSource,
        pack_template_handler_registry,
        pack_template_queue,
        registered_pack_template_ids,
    )

    return (
        PackTemplatePlanSource(),
        pack_template_handler_registry,
        pack_template_queue,
        registered_pack_template_ids,
    )


class TestPackTemplateRegistry:
    def test_registered_template_ids_match_web_bindings(self) -> None:
        _, _, _, registered = _source()

        # T1 web binding（apps/web/src/renderers/*/index.tsx）注册的 templateId
        # 是模板 id 的对齐事实源——后端注册表必须逐字一致（不能多也不能少）。
        assert set(registered()) == {"ask-v1", "discover-v1", "change-brief"}

    def test_unknown_template_is_not_a_pack_template(self) -> None:
        source, _, _, _ = _source()

        # None = 非 pack 模板 → planner 回落 fixture 分支（unknown 语义不变）
        assert source.resolve_pack_plan("no-such-pack") is None

    def test_ask_v1_resolves_pack_topology_with_handlers(self) -> None:
        source, handler_registry, pack_queue, _ = _source()

        planned = source.resolve_pack_plan("ask-v1")

        assert planned is not None
        assert planned.task_queue == pack_queue("ask-v1")
        graph = planned.graph
        graph.validate_dag()
        # pack 拓扑的场景实例：节点 id 携带 fixture 场景前缀（S6 语义）
        assert any(task_id.endswith("/intake") for task_id in graph.nodes)
        assert any(task_id.endswith("/synthesize_answer") for task_id in graph.nodes)
        registry = handler_registry("ask-v1")
        registry.validate_completeness({n.task_type for n in graph.nodes.values()})

    def test_change_brief_resolves_pack_graph_with_pack_runtime_handlers(self) -> None:
        source, handler_registry, pack_queue, _ = _source()

        planned = source.resolve_pack_plan("change-brief")

        assert planned is not None
        assert planned.task_queue == pack_queue("change-brief")
        graph = planned.graph
        graph.validate_dag()
        # pack task_graph.yaml 拓扑 + fixture unit 前缀隔离（T6 语义）
        assert all(task_id.startswith("mixed-refs/") for task_id in graph.nodes)
        registry = handler_registry("change-brief")
        registry.validate_completeness({n.task_type for n in graph.nodes.values()})

    def test_discover_v1_registered_without_fixture_bindings_is_refused(self) -> None:
        source, _, _, _ = _source()

        # pack 声明在库（task_graph.yaml）但仓库内没有可执行的 fixture 绑定资产：
        # 注册即声明缺口——解析以 machine reason 拒绝（fail closed），
        # 不是 unknown template，也不是 worker 侧崩溃。
        with pytest.raises(PlannerError, match=r"discover-v1.*fixture"):
            source.resolve_pack_plan("discover-v1")

    def test_pack_queue_names_are_namespaced_per_template(self) -> None:
        _, _, pack_queue, _ = _source()

        # 单一默认队列无法同时服务两个 pack（Synthesize primitive 的 handler
        # 语义互斥）——每个注册绑定 pin 自己的执行队列，worker 按队列组合注册表。
        queues = {pack_queue(tid) for tid in ("ask-v1", "change-brief", "discover-v1")}
        assert len(queues) == 3
        assert all(q != "zhiwei-agent-runtime" for q in queues)
