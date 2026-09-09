"""S10 fix-A：pack 模板运行注册表——template_id → pack bundle 的执行绑定。

事实源：R1 REJECT D4（三个 pack App 的 member-facing run 必须经生产路径创建）、
ADR 纪律（「App 名映射是数据」）。本模块住在评测资产层（sanctioned layer，
tests/architecture/test_app_boundaries.py 的豁免面）：template→pack 的映射与
fixture 绑定是数据行，如同 cli/evals.py 的 suite 注册——Core（runtime/api/app）
只经 Planner port 与 TaskHandlerRegistry 公共机制消费 generic 类型，不含任何
App 名字面量。

对齐事实源：web 侧 T1 冻结绑定（apps/web/src/renderers/*/index.tsx 的
registerRunBinding）注册的 templateId：``ask-v1`` / ``discover-v1`` /
``change-brief``。后端注册表逐字对齐（不能多也不能少）。

解析语义（fail closed）：
- 注册且 fixture 绑定齐备 → pack bundle conformance 零 issue + pack 拓扑图 +
  经公共 TaskHandlerRegistry 机制的 handler 集（change-brief 走 T6 pack runtime
  机制；ask-v1 走 S6 契约 fixture 注册表——ask pack 无 runtime/ 目录，其
  fixture 绑定是场景注册表）；
- 注册但 fixture 绑定缺失（discover-v1：pack 声明在库、仓库内无可执行 fixture
  handler 资产）→ 解析即 PlannerError——run 在创建期被 machine reason 拒绝，
  不是 unknown template，也绝不让 worker 侧注册表校验去炸；
- 未注册 → None（planner 回落 fixture 模板分支，既有语义不变）。

执行队列：不同 pack 的同名 primitive（如 Synthesize）handler 语义互斥，单一
worker 注册表无法同时服务两个 pack——每个绑定 pin 独立队列（数据），worker
组合按队列装配对应注册表（T6 eval 环境按 suite 隔离注册表的同款纪律）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from zhiwei.agents.pack_files import load_pack_dir, validate_pack_bundle
from zhiwei.agents.task_graph import TaskGraph
from zhiwei.evals.ask_contracts import (
    ASK_V1_UNITS,
    build_ask_contract_registry,
    scenario_for_unit,
)
from zhiwei.evals.change_brief_suites import CHANGE_BRIEF_V1, resolve_change_brief_suite
from zhiwei.evals.domain import RegisteredUnit
from zhiwei.evals.executors.change_brief import (
    build_change_brief_graph,
    build_change_brief_registry,
)
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.runtime.planner import PlannedRun, PlannerError

REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKS_DIR = REPO_ROOT / "solution-packs"


def pack_template_queue(template_id: str) -> str:
    """pack 绑定的执行队列名（数据派生，确定性）。"""
    return f"zhiwei-pack-{template_id}"


@dataclass(frozen=True)
class PackTemplateBinding:
    """一条 pack 模板绑定：注册数据 + 解析器（解析器住在本资产层）。"""

    template_id: str
    pack_dir: Path
    # fixture 绑定（fixture unit/场景 id）；None = 仓库内无可执行 fixture 绑定，
    # 解析即拒绝（注册即声明缺口，不静默、不崩溃）。
    fixture_unit_id: str | None
    resolve_graph_and_registry: Callable[[str, Path], tuple[TaskGraph, TaskHandlerRegistry]]


def _resolve_ask_v1(
    fixture_unit_id: str, pack_dir: Path
) -> tuple[TaskGraph, TaskHandlerRegistry]:
    _require_conformed_pack(pack_dir)
    scenario = scenario_for_unit(
        RegisteredUnit(sample_id=ASK_V1_UNITS[0].sample_id, unit_id=fixture_unit_id)
    )
    return scenario.graph, build_ask_contract_registry()


def _resolve_change_brief(
    fixture_unit_id: str, pack_dir: Path
) -> tuple[TaskGraph, TaskHandlerRegistry]:
    suite = resolve_change_brief_suite(CHANGE_BRIEF_V1)
    # T6 机制：conformance 零 issue 的 pack bundle → pack runtime handler 注册表
    registry = build_change_brief_registry(suite)
    bundle = load_pack_dir(suite.pack_dir)
    return build_change_brief_graph(fixture_unit_id, bundle), registry


def _require_conformed_pack(pack_dir: Path) -> None:
    """pack bundle conformance 零 issue 是解析前提（缺 skill entry 不可降级）。"""
    bundle = load_pack_dir(pack_dir)
    issues = validate_pack_bundle(bundle, pack_dir)
    if issues:
        raise PlannerError(
            "pack template bundle conformance issues: "
            + ", ".join(f"{issue.code}@{issue.location}" for issue in issues)
        )


PACK_TEMPLATE_BINDINGS: tuple[PackTemplateBinding, ...] = (
    PackTemplateBinding(
        template_id="ask-v1",
        pack_dir=_PACKS_DIR / "ask",
        fixture_unit_id="cross-source",
        resolve_graph_and_registry=_resolve_ask_v1,
    ),
    PackTemplateBinding(
        template_id="change-brief",
        pack_dir=_PACKS_DIR / "change-brief",
        fixture_unit_id="mixed-refs",
        resolve_graph_and_registry=_resolve_change_brief,
    ),
    # discover-v1：pack 声明（task_graph.yaml）在库，但仓库内没有任何可执行的
    # fixture 绑定资产（discover 检测/证伪从未以 runtime 图形态实现）——注册即
    # 诚实声明缺口：member-facing discover run 在创建期被 machine reason 拒绝，
    # 等 fixture 绑定资产落库后只需补本行数据（绑定声明与执行解耦）。
    PackTemplateBinding(
        template_id="discover-v1",
        pack_dir=_PACKS_DIR / "discover",
        fixture_unit_id=None,
        resolve_graph_and_registry=_resolve_ask_v1,  # 不可达：fixture 绑定缺失先拒绝
    ),
)

_BINDINGS_BY_ID = {binding.template_id: binding for binding in PACK_TEMPLATE_BINDINGS}


def registered_pack_template_ids() -> tuple[str, ...]:
    """注册的 pack 模板 id（与 web T1 绑定逐字对齐的对齐面）。"""
    return tuple(binding.template_id for binding in PACK_TEMPLATE_BINDINGS)


@cache
def _resolved(binding: PackTemplateBinding) -> tuple[TaskGraph, TaskHandlerRegistry]:
    assert binding.fixture_unit_id is not None  # 调用方已 fail closed
    return binding.resolve_graph_and_registry(binding.fixture_unit_id, binding.pack_dir)


class PackTemplatePlanSource:
    """Planner port 的 pack 侧计划源（runtime 只见 generic 类型）。"""

    def resolve_pack_plan(self, template_id: str) -> PlannedRun | None:
        binding = _BINDINGS_BY_ID.get(template_id)
        if binding is None:
            return None
        if binding.fixture_unit_id is None:
            raise PlannerError(
                f"pack template {template_id!r} has no fixture bindings registered; "
                "run refused (fail closed)"
            )
        try:
            graph, _ = _resolved(binding)
        except PlannerError:
            raise
        except Exception as exc:
            # pack 资产加载/解析失败不是「未注册」：以 machine reason 拒绝创建，
            # 绝不静默降级为 fixture 规划（fail closed）。
            raise PlannerError(
                f"pack template {template_id!r} not resolvable: {exc}"
            ) from exc
        return PlannedRun(
            graph=graph,
            task_type_by_task={},
            task_queue=pack_template_queue(template_id),
        )


def pack_template_handler_registry(template_id: str) -> TaskHandlerRegistry:
    """pack 绑定的 handler 注册表（worker 侧组合按队列装配；公共机制）。"""
    binding = _BINDINGS_BY_ID[template_id]
    if binding.fixture_unit_id is None:
        raise PlannerError(
            f"pack template {template_id!r} has no fixture bindings registered; "
            "no handler registry available (fail closed)"
        )
    _, registry = _resolved(binding)
    return registry


PACK_TEMPLATE_PLAN_SOURCE = PackTemplatePlanSource()
"""组合根注入点（app.py）：生产 runs router 的 planner 携带 pack 解析能力。"""
