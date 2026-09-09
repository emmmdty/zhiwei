"""S11-T6 固定负载 runner（specs/s11 §5；docs/operations/capacity.md §2 冻结）。

四类 workload 全部经生产 Runtime 执行（fixture provider，CPU-only、无 live 模型）：
- `ask`：并发 Ask run（build_ask_environment 生产执行器 + 真实 PG + Temporal dev）；
- `eval`：同执行器路径的 eval 单元混合（S9 suite 生产绑定形态）；
- `discover`：生产命令路径 start_run（discover 形态图）+ dispatcher poll 至终态；
- `sync-index`：OpenSearchPort 重建 + 查询周期（生产索引 port，真实组件）。

测量口径（capacity.md §2）：
- 时延/排队/恢复：per-request 原始样本 → MetricsFacade histogram（封闭词汇表）
  + 报告层 p50/p95 聚合；
- 终态/错误：counter；
- CPU/mem/IO：runner 侧 OS 采集（resource + /proc），不是 SDK 指标；
- cost-mode：fixture 模式声明 `fixture_no_real_billing`；
- SLO：报告只记录观测与不确定性——无测量不承诺（spec §8），先报告再提 SLO。

环境语义：workload 需要可达 PG（ZHIWEI_DATABASE_URL）；不可达 → EnvironmentError
（CLI 层转退出码 3，ADR-012 显式登记口径）。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from zhiwei.telemetry.metrics import (
    METRIC_LOAD_ERRORS,
    METRIC_LOAD_LATENCY,
    METRIC_LOAD_QUEUE_WAIT,
    METRIC_LOAD_RECOVERY,
    METRIC_LOAD_TERMINAL,
    MetricsFacade,
)

COST_MODE_FIXTURE = "fixture_no_real_billing"
SLO_DISCLAIMER = (
    "本报告只记录单机 CPU-only fixture 负载的观测值与不确定性，"
    "不构成 SLO 或容量承诺（specs/s11 §8：没有测量就不承诺；"
    "没有跨节点 HA/长期运行证据就不写 HA）。"
)


def _require_database_url() -> str:
    dsn = os.environ.get("ZHIWEI_DATABASE_URL")
    if not dsn:
        raise OSError("ZHIWEI_DATABASE_URL 未配置：负载 runner 需要可达 PG（退出码 3 口径）")
    return dsn


def _percentile(samples: list[float], q: float) -> float:
    """报告层分位（最近秩法）；空样本返回 0。"""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = round(q / 100 * (len(ordered) - 1))
    index = min(len(ordered) - 1, max(0, rank))
    return ordered[index]


def _os_resource_sample() -> dict[str, float]:
    """runner 侧 OS 采样：CPU 时间、RSS 与 IO（/proc + resource，非 SDK 指标）。"""
    import resource

    rss_kb = 0.0
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_kb = float(line.split()[1])
                    break
    except OSError:
        pass
    io_bytes = {"rchar": 0.0, "wchar": 0.0}
    try:
        with open("/proc/self/io", encoding="ascii") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                if key.strip() in io_bytes:
                    io_bytes[key.strip()] = float(value.strip())
    except OSError:
        pass
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "cpu_user_s": usage.ru_utime,
        "cpu_system_s": usage.ru_stime,
        "rss_kb": rss_kb,
        "io_read_bytes": io_bytes["rchar"],
        "io_write_bytes": io_bytes["wchar"],
    }


class WorkloadResult(BaseModel):
    workload: str
    latency_samples_ms: list[float]
    queue_wait_samples_ms: list[float]
    terminal_counts: dict[str, int]
    error_count: int
    recovery_time_ms: int = 0


class RampObservation(BaseModel):
    concurrency: int
    p95_ms: float
    throughput_per_s: float


class LoadRunReport(BaseModel):
    status: str
    profile: str
    per_workload: dict[str, WorkloadResult]
    p50_ms: float
    p95_ms: float
    queue_wait_p50_ms: float
    queue_wait_p95_ms: float
    errors: int
    terminal_counts: dict[str, int]
    recovery_time_ms: int
    resource_samples: dict[str, float]
    environment: dict[str, str] = Field(default_factory=dict)
    uncertainty: dict[str, Any] = Field(default_factory=dict)
    cost_mode: str = COST_MODE_FIXTURE
    slo_disclaimer: str = SLO_DISCLAIMER
    ramp_observations: list[RampObservation] = Field(default_factory=list)


# ---------------------------------------------------------------- workload 实现


async def _ask_journey(unit_index: int, *, kind: str) -> dict[str, Any]:
    """单次 Ask/eval journey：生产命令路径 + 真实 runtime（build_ask_environment）。"""
    from uuid import uuid4

    from zhiwei.evals.ask_contracts import ASK_V1_UNITS
    from zhiwei.evals.executors.ask import AskRuntimeExecutor, build_ask_environment
    from zhiwei.persistence.database import create_database_engine, create_session_factory
    from zhiwei.persistence.repositories import TenantRepository
    from zhiwei.persistence.tenant import TenantContext

    _ = ASK_V1_UNITS
    dsn = _require_database_url()
    # 并发 journey：每 journey 独立 org/workspace 隔离写路径（生产租户仓储准备，
    # 与 tests/integration/ask 的 CLI suite flow 同款）
    context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
    engine = create_database_engine(dsn)
    sessions = create_session_factory(engine)
    async with tenant_session_safe(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        # TenantContext 的 workspace 在此路径恒为非空（journey 隔离租户由本函数构造）
        assert context.workspace_id is not None
        await repository.create_workspace(context.workspace_id, name=f"load-{kind}")
    environment = await build_ask_environment(sessions=sessions, context=context)
    try:
        executor = AskRuntimeExecutor(environment)
        unit = ASK_V1_UNITS[unit_index % len(ASK_V1_UNITS)]
        submit_at = time.monotonic()
        outcome = await executor.execute(unit)
        latency_ms = (time.monotonic() - submit_at) * 1000
        # 排队时延不经由本 workload 测量（执行器不暴露 claim 时点）——
        # 报告级 queue_wait 只聚合真实测得的样本（见 _load_discover）
        return {
            "latency_ms": [round(latency_ms, 3)],
            "queue_wait_ms": [],
            "terminal": {outcome.status.value: 1},
            "errors": 1 if outcome.status.value in {"error", "failed"} else 0,
            "recovery_ms": 0,
        }
    finally:
        await environment.aclose()
        await engine.dispose()


def tenant_session_safe(sessions, context):  # type: ignore[no-untyped-def]
    from zhiwei.persistence.tenant import tenant_session

    return tenant_session(sessions, context)


async def _gather_bounded(
    journeys: list[Any], concurrency: int
) -> list[Any]:
    """并发档位真实生效：信号量封顶同时在途的 journey 数（F-P1-6 修复）。"""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(coroutine: Any) -> Any:
        async with semaphore:
            return await coroutine

    return await asyncio.gather(*[_bounded(j) for j in journeys], return_exceptions=True)


def _load_ask(concurrency: int, max_runs: int) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        journeys = [_ask_journey(i, kind="ask") for i in range(max_runs)]
        outcomes = await _gather_bounded(journeys, concurrency)
        return _merge_outcomes(outcomes, max_runs)

    return asyncio.run(_run())


def _load_eval(concurrency: int, max_runs: int) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        journeys = [_ask_journey(i + 1, kind="eval") for i in range(max_runs)]
        outcomes = await _gather_bounded(journeys, concurrency)
        return _merge_outcomes(outcomes, max_runs)

    return asyncio.run(_run())


def _load_discover(concurrency: int, max_runs: int) -> dict[str, Any]:
    """定时 Discover：生产路径 ProgramManager + DiscoveryTriggerService.fire →
    outbox StartRun → dispatcher poll 至终态（与 S8 触发器集成测试同款）。"""
    from uuid import uuid4

    from zhiwei.discover.programs import ProgramManager
    from zhiwei.discover.triggers import ScheduleTrigger
    from zhiwei.evals.executors.agent_runtime import (
        RuntimeEvalEnvironment,
        build_contract_registry,
    )
    from zhiwei.persistence.database import create_database_engine, create_session_factory
    from zhiwei.persistence.repositories import TenantRepository
    from zhiwei.persistence.run_commands import RunCommandService
    from zhiwei.persistence.runtime_events import RuntimeEventStore
    from zhiwei.persistence.tenant import TenantContext, tenant_session
    from zhiwei.runtime.triggers.discovery import DiscoveryTriggerService

    dsn = _require_database_url()

    async def _run() -> dict[str, Any]:
        context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        engine = create_database_engine(dsn)
        sessions = create_session_factory(engine)
        environment = await RuntimeEvalEnvironment.start(
            sessions=sessions, context=context, handler_registry=build_contract_registry()
        )
        # start() 不启动 worker：与 build_ask_environment 同款显式 __aenter__
        await environment.__aenter__()
        try:
            # 租户准备在循环外执行一次（max_runs≥2 幂等——F-P1-7 修复）
            async with tenant_session(sessions, context) as session:
                repository = TenantRepository(session, context)
                await repository.create_organization(context.organization_id, status="active")
                assert context.workspace_id is not None
                await repository.create_workspace(context.workspace_id, name="load-discover")
            latencies: list[float] = []
            queues: list[float] = []
            terminal: dict[str, int] = {}
            errors = 0
            for _ in range(max_runs):
                manager = ProgramManager()
                program = manager.create_program(
                    name="load-discover",
                    created_by="load-runner",
                    risk_charter="fixture load program",
                    service_identity="svc:load-discover",
                )
                program = manager.activate(program.id, performed_by="load-runner")
                version = manager.get_version(program.current_version_id)
                trigger = ScheduleTrigger(cron_expression="0 6 * * *")
                run_id = uuid4()
                submit_at = time.monotonic()
                async with tenant_session(sessions, context) as session:
                    commands = RunCommandService(session, context)
                    service = DiscoveryTriggerService(commands)
                    # 环境 worker 监听 DEFAULT queue（fire 缺省 "discover" 无 worker）；
                    # 显式提供 discover 形态图（缺省 app 图不是 TaskGraph 形态，
                    # workflow 侧 TaskGraph.model_validate 会拒绝）
                    await service.fire(
                        program, version, trigger, now=_load_now(), run_id=run_id,
                        task_queue="zhiwei-agent-runtime",
                        graph=_discover_shape_graph(),
                    )
                # 真实排队测量锚：首次「claim 到本 run 命令」的 poll 时刻
                # （submit → claim 的真实间隔；mark_delivered 会清空 claimed_at，
                # 事后查库拿不到 claim 时点）。
                dispatcher = environment.dispatcher()
                first_dispatch_at: float | None = None
                for _ in range(200):
                    results = await dispatcher.poll_once()
                    if results and first_dispatch_at is None:
                        first_dispatch_at = time.monotonic()
                    await asyncio.sleep(0.02)
                deadline = time.monotonic() + 60
                state = None
                while time.monotonic() < deadline:
                    async with tenant_session(sessions, context) as session:
                        store = RuntimeEventStore(session, context)
                        state = await store.reduce_state(run_id)
                    if state.is_terminal:
                        break
                    await asyncio.sleep(0.05)
                latency_ms = (time.monotonic() - submit_at) * 1000
                latencies.append(round(latency_ms, 3))
                status = state.status if state is not None else "created"
                terminal[status] = terminal.get(status, 0) + 1
                if status != "completed":
                    errors += 1
                if first_dispatch_at is not None:
                    queue_ms = (first_dispatch_at - submit_at) * 1000
                    queues.append(round(queue_ms, 3))
            return {"latency_ms": latencies, "queue_wait_ms": queues,
                    "terminal": terminal, "errors": errors, "recovery_ms": 0}
        finally:
            await environment.aclose()
            await engine.dispose()

    return asyncio.run(_run())


def _load_now():
    from datetime import datetime as _dt

    return _dt.now(tz=UTC)


def _discover_shape_graph() -> dict[str, Any]:
    from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode

    # 契约 registry 的 primitive_type 全是 "Fixture"（_StableHandler 等）——
    # task_type 必须用注册名，不是工厂映射键
    nodes = {
        "scan": TaskGraphNode(
            task_id="scan",
            task_type="Fixture",
            dependencies=(),
            parallel_safe=False,
            required_capability="fixture",
        ),
        "digest": TaskGraphNode(
            task_id="digest",
            task_type="Fixture",
            dependencies=("scan",),
            parallel_safe=False,
            required_capability="fixture",
        ),
    }
    return TaskGraph(nodes=nodes, edges={"digest": ["scan"]}).model_dump(mode="json")


def _load_sync_index(concurrency: int, max_runs: int) -> dict[str, Any]:
    """sync/index：OpenSearchPort 重建 + 查询周期（生产索引 port）。"""
    from zhiwei.knowledge.indexes.opensearch import OpenSearchPort

    async def _one(index: int) -> tuple[float, bool]:
        started = time.monotonic()
        port = OpenSearchPort(alias=f"load-sync-index-{index}")
        stats = port.kill_and_rebuild(
            [{"doc_id": f"d{t}", "content": f"document {t} fixture"} for t in range(20)]
        )
        hits = port.hybrid_search("document 1", top_k=5)
        _ = stats
        if not hits:
            raise RuntimeError("索引查询无命中")
        return round((time.monotonic() - started) * 1000, 3), True

    async def _run() -> dict[str, Any]:
        latencies: list[float] = []
        terminal: dict[str, int] = {"completed": 0, "failed": 0}
        errors = 0
        outcomes = await _gather_bounded(
            [_one(index) for index in range(max_runs)], concurrency
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                errors += 1
                terminal["failed"] += 1
                continue
            latencies.append(outcome[0])
            terminal["completed"] += 1
        return {"latency_ms": latencies, "queue_wait_ms": [],
                "terminal": terminal, "errors": errors, "recovery_ms": 0}

    return asyncio.run(_run())


def _merge_outcomes(outcomes: list[Any], max_runs: int) -> dict[str, Any]:
    latencies: list[float] = []
    queues: list[float] = []
    terminal: dict[str, int] = {}
    errors = 0
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            errors += 1
            terminal["failed"] = terminal.get("failed", 0) + 1
            continue
        latencies.extend(outcome["latency_ms"])
        queues.extend(outcome["queue_wait_ms"])
        for key, count in outcome["terminal"].items():
            terminal[key] = terminal.get(key, 0) + count
        errors += outcome["errors"]
    # 全部失败时不再伪造零时延样本（F-P1-6）：空样本 + error 计数即真相
    _ = max_runs
    return {"latency_ms": latencies, "queue_wait_ms": queues,
            "terminal": terminal, "errors": errors, "recovery_ms": 0}


LOAD_WORKLOADS: dict[str, Callable[[int, int], dict[str, Any]]] = {
    "ask": _load_ask,
    "discover": _load_discover,
    "sync-index": _load_sync_index,
    "eval": _load_eval,
}


# ---------------------------------------------------------------- 报告层


def _load_percentiles(samples: list[float]) -> tuple[float, float]:
    return _percentile(samples, 50), _percentile(samples, 95)


def _make_report(
    results: dict[str, dict[str, Any]],
    profile: str,
    ramp: list[tuple[int, float, float]],
) -> LoadRunReport:
    all_latency: list[float] = []
    all_queue: list[float] = []
    terminal: dict[str, int] = {}
    errors = 0
    recovery = 0
    per_workload: dict[str, WorkloadResult] = {}
    for name, result in results.items():
        latencies = [float(x) for x in result["latency_ms"]]
        queues = [float(x) for x in result["queue_wait_ms"]]
        all_latency.extend(latencies)
        all_queue.extend(queues)
        for outcome, count in result["terminal"].items():
            terminal[outcome] = terminal.get(outcome, 0) + count
        errors += result["errors"]
        recovery += result["recovery_ms"]
        per_workload[name] = WorkloadResult(
            workload=name,
            latency_samples_ms=latencies,
            queue_wait_samples_ms=queues,
            terminal_counts=dict(result["terminal"]),
            error_count=result["errors"],
            recovery_time_ms=result["recovery_ms"],
        )
    p50, p95 = _load_percentiles(all_latency)
    q_p50, q_p95 = _load_percentiles(all_queue)
    return LoadRunReport(
        status="passed" if errors == 0 else "failed",
        profile=profile,
        per_workload=per_workload,
        p50_ms=p50,
        p95_ms=p95,
        queue_wait_p50_ms=q_p50,
        queue_wait_p95_ms=q_p95,
        errors=errors,
        terminal_counts=terminal,
        recovery_time_ms=recovery,
        resource_samples=_os_resource_sample(),
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": str(os.cpu_count() or 0),
            "generated_at": datetime.now(tz=UTC).isoformat(),
        },
        uncertainty={
            "sample_count": len(all_latency),
            "latency_stdev_ms": round(statistics.pstdev(all_latency), 3)
            if len(all_latency) > 1 else 0.0,
        },
        ramp_observations=[
            RampObservation(concurrency=c, p95_ms=p95v, throughput_per_s=tput)
            for c, p95v, tput in ramp
        ],
    )


def run_load(
    *,
    workloads: list[str],
    concurrency: int,
    max_runs: int,
    profile: str,
    seal_dir: Path | None = None,
    ramp: list[int] | None = None,
) -> LoadRunReport:
    """执行固定负载并产出报告（先报告再提 SLO；无测量不承诺）。"""
    unknown = set(workloads) - set(LOAD_WORKLOADS)
    if unknown:
        raise KeyError(f"unknown workloads: {sorted(unknown)}")
    _require_database_url()

    facade = MetricsFacade()
    ramp_observations: list[tuple[int, float, float]] = []
    results: dict[str, dict[str, Any]] = {}

    levels = ramp or [concurrency]
    for level in levels:
        level_start = time.monotonic()
        for name in workloads:
            runner = LOAD_WORKLOADS[name]
            result = runner(level, max_runs)
            if name not in results:
                results[name] = result
            else:
                results[name]["latency_ms"].extend(result["latency_ms"])
                results[name]["queue_wait_ms"].extend(result["queue_wait_ms"])
                for outcome, count in result["terminal"].items():
                    results[name]["terminal"][outcome] = (
                        results[name]["terminal"].get(outcome, 0) + count
                    )
                results[name]["errors"] += result["errors"]
        elapsed = max(time.monotonic() - level_start, 1e-6)
        total = max_runs * len(workloads)
        latencies = [x for r in results.values() for x in r["latency_ms"]]
        _, level_p95 = _load_percentiles(latencies)
        ramp_observations.append((level, level_p95, round(total / elapsed, 3)))

    for name, result in results.items():
        for latency in result["latency_ms"]:
            facade.record(METRIC_LOAD_LATENCY, latency, {"workload": name})
        for queue in result["queue_wait_ms"]:
            facade.record(METRIC_LOAD_QUEUE_WAIT, queue, {"workload": name})
        facade.record(METRIC_LOAD_RECOVERY, float(result["recovery_ms"]), {"workload": name})
        for outcome, count in result["terminal"].items():
            for _ in range(count):
                facade.increment(METRIC_LOAD_TERMINAL, {"workload": name, "outcome": outcome})
        for _ in range(result["errors"]):
            facade.increment(METRIC_LOAD_ERRORS, {"workload": name})

    report = _make_report(results, profile, ramp_observations)
    if seal_dir is not None:
        _write_seal(seal_dir, report)
    return report


def _write_seal(seal_dir: Path, report: LoadRunReport) -> None:
    seal_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(report.model_dump_json())
    payload["seal_version"] = 1
    (seal_dir / "load-run-seal.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
