"""S10 change-brief-v1 executor：pack task graph 经生产 Runtime 命令路径执行。

事实源：specs/s10-studio-third-app.md §4/§7、AGENTS.md「评测走生产 Runtime，
不写评测专用旁路」。

执行链路与生产完全同构（与 S6 ask executor 同款）：RunCommandService（Run 行 +
outbox 命令，同事务）→ OutboxDispatcher → Temporal dev server → AgentRunWorkflow →
RuntimeActivities（PG canonical events）。pack 侧构成：

- load_pack_dir 装载 solution-packs/change-brief 并要求 conformance 零 issue
  （fail closed——含 skill_entry_missing）；
- pack runtime（runtime/{impact_analysis,planner,synthesis}.py）经 importlib 装载，
  handler 按公共 TaskHandlerRegistry 机制注册（task_graph.yaml 的 Core primitive
  类型 → pack handler 工厂）；对 pack 之外的 primitive，注册表 fail closed；
- 每个 unit 的 task graph 由 pack task_graph.yaml 拓扑派生（unit 前缀隔离行为），
  经生产命令路径执行后对 reduced RunState 的 canonical brief 按冻结 expected 判分。

判分语义（score_change_brief，纯函数）：brief 必须与 fixture expected 对齐——
affected symbols/dependencies/tests 正确、risks/unknowns 诚实；unknown-symbol
场景必须产出指名 unknowns，绝不编造快照之外的符号（genericity/诚实性契约）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

from zhiwei.agents.pack_files import (
    PackFileBundle,
    PackFileError,
    load_pack_dir,
    validate_pack_bundle,
)
from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.evals.change_brief_suites import (
    CHANGE_BRIEF_V1,
    EXECUTOR_KIND,
    PRODUCTION_CHANGE_BRIEF_PATH,
    ChangeBriefSuiteDefinition,
    ChangeBriefUnit,
    ExpectedBrief,
    resolve_change_brief_suite,
)
from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.executors.agent_runtime import (
    DEFAULT_TASK_QUEUE,
    RuntimeEvalEnvironment,
)
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.runtime.reducer import RunState

logger = logging.getLogger(__name__)

PACK_DIR = Path(__file__).resolve().parents[3] / "solution-packs" / "change-brief"
PACK_RUNTIME_MODULES = ("impact_analysis", "planner", "synthesis")
_PACK_RUNTIME_PACKAGE = "zhiwei_eval_change_brief_pack_runtime"

# pack capability（pack.yaml）→ task primitive 的绑定：Retrieve 消费知识检索，
# 其余任务消费 GitHub 读取面。capability 是声明面（pack_capabilities 的映射），
# 生产 runtime 不在本路径做 capability 门禁（admission 在 S4 检查点）。
_TASK_CAPABILITIES = {
    "Retrieve": "knowledge.retrieve@1",
    "Analyze": "github.read@1",
    "Verify": "github.read@1",
    "Synthesize": "github.read@1",
    "EmitArtifact": "github.read@1",
    "Finish": "github.read@1",
}

_BRIEF_REQUIRED_KEYS = (
    "affected_symbols",
    "affected_dependencies",
    "affected_tests",
    "related_prs",
    "related_issues",
    "related_checks",
    "risks",
    "unknowns",
    "code_refs",
    "github_refs",
)

_TERMINAL_TIMEOUT = timedelta(seconds=60)
_POLL_INTERVAL = 0.1


def load_pack_runtime(pack_root: Path | None = None) -> dict[str, ModuleType]:
    """装载 pack runtime 模块（pack 目录不是 python 包，按文件路径 importlib 装载）。

    模块以稳定名注册进 sys.modules：重复装载返回同一份，handler 注册表与判分
    共享同一实现。缺失模块 fail closed——skill entry 缺失不是可降级状态。
    """
    root = (pack_root or PACK_DIR) / "runtime"
    modules: dict[str, ModuleType] = {}
    for name in PACK_RUNTIME_MODULES:
        qualified = f"{_PACK_RUNTIME_PACKAGE}.{name}"
        if qualified in sys.modules:
            modules[name] = sys.modules[qualified]
            continue
        path = root / f"{name}.py"
        if not path.is_file():
            raise PackFileError(
                f"pack runtime module missing: {path}",
                detail={"file": str(path), "reason": "missing"},
            )
        spec = importlib.util.spec_from_file_location(qualified, path)
        if spec is None or spec.loader is None:
            raise PackFileError(
                f"pack runtime module not loadable: {path}",
                detail={"file": str(path), "reason": "spec"},
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
        modules[name] = module
    return modules


def build_change_brief_registry(
    suite: ChangeBriefSuiteDefinition, *, pack_root: Path | None = None
) -> TaskHandlerRegistry:
    """从 pack runtime 构造 handler 注册表（primitive → pack handler 工厂）。

    构造期即要求 pack conformance 零 issue：声明漂移（含 skill_entry_missing）
    不是可运行状态。pack 之外的 primitive 不注册——validate_completeness 对
    未知类型 fail closed（genericity 负例的断言面）。
    """
    root = pack_root or suite.pack_dir
    bundle = load_pack_dir(root)
    issues = validate_pack_bundle(bundle, root)
    if issues:
        raise PackFileError(
            "change-brief pack conformance issues: "
            + ", ".join(f"{issue.code}@{issue.location}" for issue in issues)
        )
    runtime = load_pack_runtime(root)
    scenarios = {unit.unit_id: unit.model_dump(mode="json") for unit in suite.units}
    analyze_impact = runtime["impact_analysis"].analyze_impact
    plan_retrieval = runtime["planner"].plan_retrieval
    verify_impact = runtime["planner"].verify_impact
    registry = TaskHandlerRegistry()
    for handler in (
        runtime["planner"].build_retrieve_handler(scenarios, plan_retrieval=plan_retrieval),
        runtime["planner"].build_analyze_handler(
            scenarios, analyze_impact=analyze_impact, plan_retrieval=plan_retrieval
        ),
        runtime["planner"].build_verify_handler(
            scenarios, analyze_impact=analyze_impact, plan_retrieval=plan_retrieval
        ),
        runtime["synthesis"].build_synthesize_handler(
            scenarios,
            analyze_impact=analyze_impact,
            plan_retrieval=plan_retrieval,
            verify_impact=verify_impact,
        ),
        runtime["planner"].build_emit_artifact_handler(),
        runtime["planner"].build_finish_handler(),
    ):
        registry.register(handler)
    return registry


def build_change_brief_graph(unit_prefix: str, bundle: PackFileBundle) -> TaskGraph:
    """pack task_graph.yaml 拓扑 → unit 前缀隔离的 TaskGraph（声明即执行面）。

    节点 id 前缀隔离行为（ask_contracts 同型）；依赖/边由 pack 声明派生，
    构造期 validate_dag——pack 图漂移（环、悬边）在提交生产路径前拒绝。
    """
    declaration = bundle.task_graph
    if declaration is None:
        raise PackFileError("change-brief pack 缺少 task_graph.yaml")
    nodes: dict[str, TaskGraphNode] = {}
    for task in declaration.tasks:
        task_id = f"{unit_prefix}/{task.id}"
        capability = _TASK_CAPABILITIES.get(task.type)
        if capability is None:
            raise PackFileError(
                f"change-brief pack task {task.id!r} 使用未登记 primitive {task.type!r}"
            )
        nodes[task_id] = TaskGraphNode(
            task_id=task_id,
            task_type=task.type,
            dependencies=tuple(f"{unit_prefix}/{dep}" for dep in task.depends_on),
            required_capability=capability,
        )
    edges: dict[str, list[str]] = {}
    for edge in declaration.edges:
        edges.setdefault(f"{unit_prefix}/{edge.to}", []).append(f"{unit_prefix}/{edge.from_}")
    graph = TaskGraph(nodes=nodes, edges=edges)
    graph.validate_dag()
    return graph


async def build_change_brief_environment(
    *,
    sessions: Any,
    context: TenantContext,
    suite: ChangeBriefSuiteDefinition,
) -> RuntimeEvalEnvironment:
    """启动 change-brief-v1 的生产 runtime eval 环境（pack 注册表 + dev server + worker）。

    返回的实例已进入运行态（worker 已启动）；用 ``aclose()`` 显式关闭。
    """
    environment = await RuntimeEvalEnvironment.start(
        sessions=sessions,
        context=context,
        handler_registry=build_change_brief_registry(suite),
    )
    await environment.__aenter__()
    return environment


def resolve_unit(suite: ChangeBriefSuiteDefinition, unit: RegisteredUnit) -> ChangeBriefUnit:
    """注册单位 → fixture 定义；未知 (sample_id, unit_id) fail closed。"""
    for candidate in suite.units:
        if candidate.unit_id == unit.sample_id and unit.unit_id == unit.sample_id:
            return candidate
    raise LookupError(
        f"unit 未注册于 suite: {unit.sample_id}/{unit.unit_id}"
    )


def score_change_brief(
    expected: ExpectedBrief, state: RunState
) -> tuple[list[str], list[str]]:
    """对 reduced RunState 的 canonical brief 按冻结 expected 断言（纯函数）。

    返回 (checks, failures)：生产路径的 brief 行为改变会留下 failures（判 0 分），
    不反查场景回填答案。
    """
    checks: list[str] = []
    failures: list[str] = []
    if state.status != "completed":
        failures.append(f"run status {state.status!r} != 'completed'")
    brief = state.canonical.get("brief")
    if not isinstance(brief, dict):
        return [*checks], [*failures, "canonical 缺少 brief artifact"]
    missing = [key for key in _BRIEF_REQUIRED_KEYS if key not in brief]
    if missing:
        failures.append(f"brief 缺少必需字段: {missing}")
        return [*checks], [*failures]

    verification = state.canonical.get("verification_result")
    if not isinstance(verification, dict) or verification.get("verification_ok") is not True:
        failures.append(
            f"verification_result 未通过生产验证: {verification!r}"
        )
    else:
        checks.append("verification_ok")

    artifact_id = state.canonical.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.startswith("artifact:"):
        failures.append(f"artifact_id 缺失或非法: {artifact_id!r}")
    elif state.canonical.get("artifact_kind") != "verified-brief":
        failures.append(f"artifact_kind 非法: {state.canonical.get('artifact_kind')!r}")
    else:
        checks.append("brief_artifact_emitted")

    symbol_names = sorted({str(s.get("name")) for s in brief["affected_symbols"]})
    if symbol_names != sorted(set(expected.affected_symbols)):
        failures.append(
            f"affected_symbols {symbol_names} != expected {sorted(expected.affected_symbols)}"
        )
    else:
        checks.append("affected_symbols_match")
    fabricated = sorted(set(expected.no_fabricated_symbols) & set(symbol_names))
    if fabricated:
        failures.append(f"fabricated affected symbols: {fabricated}")
    dependency_names = sorted({str(d.get("name")) for d in brief["affected_dependencies"]})
    if dependency_names != sorted(set(expected.affected_dependencies)):
        failures.append(
            f"affected_dependencies {dependency_names} != "
            f"expected {sorted(expected.affected_dependencies)}"
        )
    else:
        checks.append("affected_dependencies_match")

    tests = {str(t.get("test_id")): str(t.get("expected_status")) for t in brief["affected_tests"]}
    if sorted(tests) != sorted(set(expected.affected_tests)):
        failures.append(
            f"affected_tests {sorted(tests)} != expected {sorted(expected.affected_tests)}"
        )
    else:
        checks.append("affected_tests_match")
    observed_failed = sorted(t for t, status in tests.items() if status == "fail")
    if observed_failed != sorted(set(expected.failed_tests)):
        failures.append(
            f"failed tests {observed_failed} != expected {sorted(expected.failed_tests)}"
        )

    brief_prs = sorted({int(p.get("pr_number")) for p in brief["related_prs"]})
    if brief_prs != sorted(set(expected.related_prs)):
        failures.append(f"related_prs {brief_prs} != expected {sorted(expected.related_prs)}")
    brief_issues = sorted({int(i.get("issue_number")) for i in brief["related_issues"]})
    if brief_issues != sorted(set(expected.related_issues)):
        failures.append(
            f"related_issues {brief_issues} != expected {sorted(expected.related_issues)}"
        )
    brief_checks = sorted({str(c.get("name")) for c in brief["related_checks"]})
    if brief_checks != sorted(set(expected.related_checks)):
        failures.append(
            f"related_checks {brief_checks} != expected {sorted(expected.related_checks)}"
        )
    severities = {str(r.get("severity")) for r in brief["risks"]}
    missing_severities = sorted(set(expected.risks_severities) - severities)
    if missing_severities:
        failures.append(f"risks 缺少声明严重度: {missing_severities}")
    else:
        checks.append("risks_severities_match")

    unknowns = [str(u) for u in brief["unknowns"]]
    if expected.unknowns_empty:
        if unknowns:
            failures.append(f"unknowns 必须为空: {unknowns}")
        else:
            checks.append("unknowns_empty")
    for needle in expected.unknowns_contain:
        if not any(needle in u for u in unknowns):
            failures.append(f"unknowns 未披露 {needle!r}")
        else:
            checks.append(f"unknowns_disclose:{needle}")
    if len(brief["code_refs"]) < expected.min_code_refs:
        failures.append(
            f"code_refs {len(brief['code_refs'])} < min {expected.min_code_refs}"
        )
    if len(brief["github_refs"]) < expected.min_github_refs:
        failures.append(
            f"github_refs {len(brief['github_refs'])} < min {expected.min_github_refs}"
        )
    if not failures:
        checks.append("brief_matches_expected")
    return checks, failures


class ChangeBriefPackExecutor:
    """经生产命令路径执行 change-brief-v1 单位并按冻结 expected 判分。"""

    def __init__(
        self,
        environment: RuntimeEvalEnvironment,
        suite: ChangeBriefSuiteDefinition | None = None,
    ) -> None:
        self._environment = environment
        self._suite = suite or resolve_change_brief_suite(CHANGE_BRIEF_V1)
        self._sessions = environment.sessions
        self._context = environment.tenant_context
        self._bundle = load_pack_dir(self._suite.pack_dir)
        issues = validate_pack_bundle(self._bundle, self._suite.pack_dir)
        if issues:
            raise PackFileError(
                "change-brief pack conformance issues: "
                + ", ".join(f"{issue.code}@{issue.location}" for issue in issues)
            )

    @property
    def sessions(self) -> Any:
        return self._sessions

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        try:
            definition = resolve_unit(self._suite, unit)
        except LookupError as exc:
            return SampleOutcome(
                unit=unit,
                status=SampleStatus.FAILED,
                result={"suite": self._suite.name, "error": str(exc)},
            )
        try:
            return await self._execute_unit(unit, definition)
        except Exception as exc:
            logger.exception("change-brief unit %s errored", unit.unit_id)
            return SampleOutcome(
                unit=unit,
                status=SampleStatus.ERROR,
                result={"error": str(exc), "unit_id": unit.unit_id},
            )

    async def _execute_unit(
        self, unit: RegisteredUnit, definition: ChangeBriefUnit
    ) -> SampleOutcome:
        graph = build_change_brief_graph(unit.unit_id, self._bundle)
        run_id = uuid4()

        # 1. 生产命令路径：Run 行 + start 命令（同一事务）
        async with tenant_session(self._sessions, self._context) as session:
            from zhiwei.persistence.run_commands import RunCommandService

            service = RunCommandService(session, self._context)
            await service.submit_start_run(
                run_id=run_id,
                # exclude_defaults：nodes 形态的图不得携带空的 tasks 列表——
                # TaskGraph 的双形态归一在 nodes 与 tasks 并存时 fail closed，
                # wire 载荷必须保持单形态。
                graph=graph.model_dump(mode="json", exclude_defaults=True),
                task_queue=DEFAULT_TASK_QUEUE,
            )

        # 2. dispatch start 并等待终态（真相在 PG）
        dispatcher = self._environment.dispatcher()
        await self._drain_commands(dispatcher)
        state = await self._wait_terminal(run_id)

        # 3. 判分（只读 reduced RunState 与事件序列）
        async with tenant_session(self._sessions, self._context) as session:
            from zhiwei.persistence.runtime_events import RuntimeEventStore

            events = await RuntimeEventStore(session, self._context).load_events(run_id)
        checks, failures = score_change_brief(definition.expected, state)

        result = {
            "suite": self._suite.name,
            "unit_id": unit.unit_id,
            "executor": EXECUTOR_KIND,
            "production_path": PRODUCTION_CHANGE_BRIEF_PATH,
            "corpus_digest": self._suite.corpus_digest,
            "run_id": str(run_id),
            "run_status": state.status,
            "tasks": {tid: t.status for tid, t in state.tasks.items()},
            "event_count": len(events),
            "canonical_keys": sorted(state.canonical),
            "brief": state.canonical.get("brief"),
            "checks": checks,
            "failures": failures,
            "score": 1.0 if not failures else 0.0,
            "verdict": "pass" if not failures else "fail",
        }
        if failures:
            return SampleOutcome(unit=unit, status=SampleStatus.FAILED, result=result)
        return SampleOutcome(unit=unit, status=SampleStatus.COMPLETED, result=result)

    async def _drain_commands(self, dispatcher: Any) -> None:
        """轮询直至没有 pending/processing 的 runtime 命令。"""
        for _ in range(200):
            results = await dispatcher.poll_once()
            pending = await self._pending_command_count()
            if not results and pending == 0:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("change-brief runtime commands did not drain")

    async def _pending_command_count(self) -> int:
        from sqlalchemy import select

        from zhiwei.persistence.models import OutboxMessage
        from zhiwei.persistence.run_commands import RUNTIME_COMMAND_TOPIC

        async with tenant_session(self._sessions, self._context) as session:
            rows = (
                await session.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.organization_id == self._context.organization_id,
                        OutboxMessage.workspace_id == self._context.workspace_id,
                        OutboxMessage.topic == RUNTIME_COMMAND_TOPIC,
                        OutboxMessage.status.in_(("pending", "processing")),
                    )
                )
            ).all()
            return len(rows)

    async def _wait_terminal(self, run_id: UUID) -> RunState:
        deadline = datetime.now(tz=UTC) + _TERMINAL_TIMEOUT
        while datetime.now(tz=UTC) < deadline:
            async with tenant_session(self._sessions, self._context) as session:
                from zhiwei.persistence.runtime_events import RuntimeEventStore

                state = await RuntimeEventStore(session, self._context).reduce_state(run_id)
                if state.is_terminal:
                    return state
            await asyncio.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"change-brief run {run_id} did not reach terminal state")
