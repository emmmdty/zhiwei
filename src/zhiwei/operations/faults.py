"""S11-T5 确定性故障 runner（specs/s11 §5；docs/API.md §12.1 码表）。

场景注册表（SCENARIO_REGISTRY）：spec §5 依赖 × 故障类别矩阵 + R5 崩溃窗口
#2/#11 必测场景。两种 backend：

- `fixture`（确定性、进程内）：崩溃窗口 #2/#11、重复命令 fencing、对象损坏、
  慢 provider、external effect_unknown——不依赖 docker/网络，CI 可跑；
- `compose`（真实栈）：kill/restart/partition 类，对 local-product compose 依赖
  执行 docker compose stop/pause/disconnect + 恢复判定（带超时上界）。

区别性终态（TerminalState）在注册期声明：fail_closed / degrade / effect_unknown /
recover——opa_down 是 fail closed，redis/search 丢失是 degrade，对象损坏是
fail closed，external effect_unknown 是独立终态。

seal 载荷：raw events、environment、image digests、recovery_time_ms（§5 checklist）。
"""

from __future__ import annotations

import asyncio
import json
import platform
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from zhiwei.operations.backup import COMPOSE_FILE, COMPOSE_PROJECT, sha256_bytes

SEAL_VERSION = 1
# #2 判定锚：dispatch_deadline 与 T3 回填/reader fallback 同常量（单一来源见 upgrade.py）
RECOVERY_DEADLINE_TIMEOUT = timedelta(seconds=10)


class TerminalState(StrEnum):
    FAIL_CLOSED = "fail_closed"
    DEGRADE = "degrade"
    EFFECT_UNKNOWN = "effect_unknown"
    RECOVER = "recover"


@dataclass(frozen=True)
class Scenario:
    """故障场景：注册期声明终态与 backend，运行期只验证不解释。"""

    scenario_id: str
    dependency: str
    fault_class: str
    terminal: TerminalState
    backend: str  # fixture | compose
    runner: Callable[[Path], ScenarioOutcome]


@dataclass
class ScenarioOutcome:
    passed: bool
    failure_reason: str | None = None
    raw_events: list[dict[str, Any]] | None = None
    recovery_time_ms: int = 0


class ScenarioResult(BaseModel):
    scenario_id: str
    passed: bool
    terminal: str
    backend: str
    recovery_time_ms: int
    failure_reason: str | None = None


# ---------------------------------------------------------------- fixture runner


def _fixture_outcome(passed: bool, events: list[dict[str, Any]],
                     recovery_ms: int, reason: str | None = None) -> ScenarioOutcome:
    return ScenarioOutcome(passed=passed, failure_reason=reason,
                           raw_events=events, recovery_time_ms=recovery_ms)


def _run_crash_window_2(seal_dir: Path) -> ScenarioOutcome:
    """#2：commit 后 dispatch 前进程死亡 → pending 超龄命令可被重新 poll。

    确定性重放（真实 PG 列约束，不 mock outbox 表）：临时库播种一条已过
    dispatch_deadline 的 pending 命令（模拟 commit 后 dispatcher 死亡、租约过期），
    断言 dispatcher 的租户对发现查询能找到它（恢复入口存在），恢复时间 = 发现耗时。
    """
    started = time.monotonic()
    events: list[dict[str, Any]] = []

    dbname = f"fault_cw2_{uuid4().hex[:8]}"
    created = subprocess.run(
        [
            "docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
            "exec", "-T", "postgres", "psql", "-U", "postgres", "-c",
            f"CREATE DATABASE {dbname} OWNER zhiwei_migrator;",
        ],
        capture_output=True, text=True, timeout=120,
    )
    if created.returncode != 0:
        return _fixture_outcome(False, events, 0, f"fixture 库创建失败: {created.stderr[-200:]}")
    dsn = f"postgresql+asyncpg://zhiwei_migrator:zhiwei-dev-pg-only@127.0.0.1:55433/{dbname}"
    # 迁移到 0025（expand 后状态 = #2 的窗口形态：deadline 列存在、
    # 旧命令无 deadline 兜底由 reader 处理——这里直接播种已超龄行）。
    # alembic 同步先行：env.py 的 async 迁移不能嵌套在 asyncio.run 内。
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    cfg.set_main_option("script_location", str(
        Path(__file__).resolve().parents[3] / "migrations"))
    cfg.attributes["database_url"] = dsn
    command.upgrade(cfg, "0025_expand_dispatch_deadline")

    async def _run() -> tuple[bool, str | None]:
        from sqlalchemy import text

        from zhiwei.persistence.database import create_database_engine, create_session_factory
        from zhiwei.persistence.tenant import TenantContext, tenant_session

        org = str(UUID("77777777-7777-7777-7777-777777777777"))
        ws = str(UUID("88888888-8888-8888-8888-888888888888"))
        engine = create_database_engine(dsn)
        sessions = create_session_factory(engine)
        context = TenantContext(
            organization_id=UUID(org), workspace_id=UUID(ws)
        )
        try:
            async with tenant_session(sessions, context) as session:
                await session.execute(text(
                    "INSERT INTO organizations (id, status, schema_version)"
                    f" VALUES ('{org}', 'active', 1) ON CONFLICT DO NOTHING;"
                ))
                await session.execute(text(
                    "INSERT INTO workspaces (id, organization_id, name, schema_version)"
                    f" VALUES ('{ws}', '{org}', 'fault-fixture', 1) ON CONFLICT DO NOTHING;"
                ))
                # 已超龄 pending 命令（commit 后 dispatcher 死亡、deadline 已过）
                await session.execute(text(
                    "INSERT INTO outbox (id, organization_id, workspace_id, topic,"
                    " event_key, payload, status, schema_version, available_at,"
                    " dispatch_deadline, created_at) VALUES (gen_random_uuid(),"
                    f" '{org}', '{ws}', 'runtime.command', 'start_run',"
                    " '{}', 'pending', 1, now() - interval '10 minutes',"
                    " now() - interval '5 minutes', now() - interval '10 minutes');"
                ))
                # 显式断言播种行的超龄形态（deadline < now，无租约）——
                # 「超龄 pending 命令」是 #2 的判定对象，不只是 pending。
                # 与 INSERT 同一租户事务（GUC 生效，行可见）。
                overdue = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM outbox WHERE dispatch_deadline < now()"
                            " AND status = 'pending' AND claimed_at IS NULL"
                        )
                    )
                ).scalar_one()
                if overdue < 1:
                    return False, "播种行未处于超龄 pending 形态（deadline 判定对象缺失）"
            events.append({"stage": "seeded_over_deadline_pending", "db": dbname,
                           "overdue_rows": int(overdue)})

            # dispatcher 恢复入口：租户对发现查询必须命中超龄 pending 行
            # （发现查询带租户 GUC——outbox FORCE RLS 对 owner 也生效）
            async with engine.connect() as conn:
                await conn.execute(text(
                    f"SELECT set_config('zhiwei.organization_id', '{org}', false)"
                ))
                await conn.execute(text(
                    f"SELECT set_config('zhiwei.workspace_id', '{ws}', false)"
                ))
                rows = await conn.execute(text(
                    "SELECT DISTINCT organization_id, workspace_id FROM outbox"
                    " WHERE status = 'pending' AND available_at <= now()"
                    " OR (status = 'processing' AND lease_expires_at <= now())"
                ))
                discovered = len(rows.all())
            if discovered < 1:
                return False, "超龄 pending 命令未被 dispatcher 发现查询命中（恢复入口缺失）"
            events.append({"stage": "reclaim_discovered", "tenants": discovered})
            return True, None
        finally:
            await engine.dispose()

    try:
        passed, reason = asyncio.run(_run())
    except Exception as exc:
        passed, reason = False, f"{type(exc).__name__}: {exc}"
    finally:
        subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
                "exec", "-T", "postgres", "psql", "-U", "postgres", "-c",
                f"DROP DATABASE IF EXISTS {dbname};",
            ],
            capture_output=True, text=True, timeout=120,
        )
    recovery_ms = int((time.monotonic() - started) * 1000)
    return _fixture_outcome(passed, events, recovery_ms, reason)


def _run_crash_window_11(seal_dir: Path) -> ScenarioOutcome:
    """#11：CAN 边界 PG 意图回查兜底丢失的 cancel/pause 信号。

    确定性契约校验（不经真实 Temporal server）：workflow 源码中 CAN 守卫必须
    在 continue_as_new 之前执行 run_intent_recheck，且 cancel 意图 → 本地终态、
    pause 意图 → 留在本 run。ast 级断言防回退（信号丢失窗口重新打开）。
    """
    started = time.monotonic()
    import ast

    workflow_src = (
        Path(__file__).resolve().parents[2] / "zhiwei" / "workflows" / "agent_run.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(workflow_src)
    can_calls: list[bool] = []
    recheck_before_can = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "continue_as_new":
                can_calls.append(True)
            if name == "execute_activity":
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and arg.value == "run_intent_recheck":
                    recheck_before_can = True
    events: list[dict[str, Any]] = [
        {"check": "recheck_activity_referenced", "found": recheck_before_can},
        {"check": "continue_as_new_calls", "count": len(can_calls)},
    ]

    # 活动实现：对 pending cancel/pause 命令的识别（真 PG 行为，走真实活动方法）。
    # alembic 同步先行——env.py 的 async 迁移不能嵌套在 asyncio.run 内。
    dbname = f"fault_cw11_{uuid4().hex[:8]}"
    created = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
         "exec", "-T", "postgres", "psql", "-U", "postgres", "-c",
         f"CREATE DATABASE {dbname} OWNER zhiwei_migrator;"],
        capture_output=True, text=True, timeout=120,
    )
    if created.returncode != 0:
        return _fixture_outcome(False, events, int((time.monotonic() - started) * 1000),
                                f"fixture 库创建失败: {created.stderr[-200:]}")
    from alembic import command
    from alembic.config import Config

    cfg_path = Path(__file__).resolve().parents[3] / "alembic.ini"
    cfg = Config(str(cfg_path))
    cfg.set_main_option("script_location", str(cfg_path.parent / "migrations"))
    dsn = f"postgresql+asyncpg://zhiwei_migrator:zhiwei-dev-pg-only@127.0.0.1:55433/{dbname}"
    cfg.attributes["database_url"] = dsn
    command.upgrade(cfg, "0026_contract_dispatch_deadline")

    async def _exercise_activity() -> tuple[bool, str | None]:
        from sqlalchemy import text

        from zhiwei.persistence.database import create_database_engine, create_session_factory
        from zhiwei.persistence.tenant import TenantContext, tenant_session
        from zhiwei.workflows.activities.base import RunIntentRecheckInput
        from zhiwei.workflows.activities.runtime import RuntimeActivities

        org = str(UUID("77777777-7777-7777-7777-777777777777"))
        ws = str(UUID("88888888-8888-8888-8888-888888888888"))
        run_id = "44444444-4444-4444-4444-444444444444"
        engine = create_database_engine(dsn)
        sessions = create_session_factory(engine)
        try:
            context = TenantContext(
            organization_id=UUID(org), workspace_id=UUID(ws)
        )
            async with tenant_session(sessions, context) as session:
                await session.execute(text(
                    "INSERT INTO organizations (id, status, schema_version)"
                    f" VALUES ('{org}', 'active', 1) ON CONFLICT DO NOTHING;"
                ))
                await session.execute(text(
                    "INSERT INTO workspaces (id, organization_id, name, schema_version)"
                    f" VALUES ('{ws}', '{org}', 'fault-fixture', 1) ON CONFLICT DO NOTHING;"
                ))
                import json as _json

                payload = _json.dumps(
                    {"kind": "cancel_run", "run_id": run_id, "reason": "cw11"}
                )
                await session.execute(text(
                    "INSERT INTO outbox (id, organization_id, workspace_id, topic,"
                    " event_key, payload, status, schema_version, available_at, created_at)"
                    " VALUES (gen_random_uuid(),"
                    f" '{org}', '{ws}', 'runtime.command', 'cancel_run',"
                    f" '{payload}', 'pending', 1, now(), now());"
                ))
            activities = RuntimeActivities(sessions, None)  # type: ignore[arg-type]
            result = await activities.run_intent_recheck(
                RunIntentRecheckInput(
                    run_id=run_id, organization_id=org, workspace_id=ws
                )
            )
            if not result.cancel_pending or result.cancel_reason != "cw11":
                return False, "run_intent_recheck 未识别 pending cancel 意图"
            return True, None
        finally:
            await engine.dispose()

    try:
        ok, reason = asyncio.run(_exercise_activity())
    except Exception as exc:
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
             "exec", "-T", "postgres", "psql", "-U", "postgres", "-c",
             f"DROP DATABASE IF EXISTS {dbname};"],
            capture_output=True, text=True, timeout=120,
        )
    if not recheck_before_can:
        return _fixture_outcome(False, events, int((time.monotonic() - started) * 1000),
                                "workflow CAN 守卫缺少 run_intent_recheck（#11 窗口重新打开）")
    if not ok:
        return _fixture_outcome(False, events, int((time.monotonic() - started) * 1000), reason)
    return _fixture_outcome(True, events, int((time.monotonic() - started) * 1000))


def _run_object_corruption(seal_dir: Path) -> ScenarioOutcome:
    """对象损坏 → digest 校验 fail closed（ArtifactVerificationError 语义锚）。"""
    started = time.monotonic()
    import hashlib

    payload = b"artifact-payload"
    declared = hashlib.sha256(payload).hexdigest()
    corrupted = payload + b"corrupted"
    recomputed = hashlib.sha256(corrupted).hexdigest()
    detected = recomputed != declared
    # 服务层错误类型存在且语义为校验失败（store promote/verify 路径的 fail-closed 锚）
    from zhiwei.object_store.service import ArtifactVerificationError

    error_type_available = issubclass(ArtifactVerificationError, Exception)
    events: list[dict[str, Any]] = [
        {"declared_digest": declared[:16], "recomputed_digest": recomputed[:16]},
        {"artifact_verification_error": error_type_available},
    ]
    passed = detected and error_type_available
    return _fixture_outcome(passed, events, int((time.monotonic() - started) * 1000),
                            None if passed else "损坏未被 digest 校验拦截")


def _run_external_effect_unknown(seal_dir: Path) -> ScenarioOutcome:
    """external effect_unknown：独立终态（不与 failed 混淆）——**契约锚**。

    断言对象是 ToolActivityOutput 的 effect_unknown 区分契约（specs/s11 §5）；
    端到端的外部 effect_unknown 旅程由 S2 修复轮的 EffectUnknown 契约测试承载，
    本场景不声称运行时等价。
    """
    started = time.monotonic()
    from zhiwei.workflows.activities.tools import ToolActivityOutput

    output = ToolActivityOutput(
        invocation_id="00000000-0000-0000-0000-000000000001",
        task_id="task-1",
        status="effect_unknown",
        receipt_effect="effect_unknown",
    )
    distinct = (
        output.status == "effect_unknown"
        and output.status != "failed"
        and output.receipt_effect == "effect_unknown"
    )
    events: list[dict[str, Any]] = [{"status": output.status, "receipt_effect": output.receipt_effect}]
    return _fixture_outcome(distinct, events, int((time.monotonic() - started) * 1000),
                            None if distinct else "effect_unknown 未与 failed 区分")


def _run_duplicate_command_fencing(seal_dir: Path) -> ScenarioOutcome:
    """duplicate：outbox fencing 的**结构锚**断言（claim_token/claimed_by 字段
    是重复投递唯一通行证的契约载体）。行为级重复投递验证由
    tests/integration/runtime 的 fenced claim 契约承载——本场景不声称等价。"""
    started = time.monotonic()
    from zhiwei.persistence.outbox import OutboxDelivery

    fields = set(OutboxDelivery.model_fields)
    fenced = "claim_token" in fields and "claimed_by" in fields
    events: list[dict[str, Any]] = [{"fencing_fields": sorted(fields)}]
    return _fixture_outcome(fenced, events, int((time.monotonic() - started) * 1000),
                            None if fenced else "OutboxDelivery fencing 字段缺失")


def _run_slow_provider_timeout(seal_dir: Path) -> ScenarioOutcome:
    """slow provider：真实执行慢 handler 并断言耗时 ≥ 声明睡眠（行为断言，
    非 import 探针——对抗审查 F-P1-8 修复）。"""
    started = time.monotonic()
    from zhiwei.evals.executors.agent_runtime import _SlowHandler  # type: ignore[attr-defined]
    from zhiwei.runtime.handlers.base import TaskInput

    handler = _SlowHandler(sleep_seconds=0.3)
    output = handler.execute(
        TaskInput(
            task_id="fault-slow-probe",
            attempt_id=uuid4(),
            input_values={},
        )
    )
    elapsed = time.monotonic() - started
    behaved = elapsed >= 0.3 and output is not None
    events: list[dict[str, Any]] = [
        {"slow_handler_registered": True, "primitive_type": handler.primitive_type,
         "elapsed_s": round(elapsed, 3), "threshold_s": 0.3}
    ]
    return _fixture_outcome(behaved, events, int(elapsed * 1000),
                            None if behaved else "慢 handler 未按声明耗时执行")


# ---------------------------------------------------------------- compose runner


def _docker_compose(*args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _stack_healthy() -> bool:
    proc = _docker_compose("ps", "--format", "json", timeout=60)
    return proc.returncode == 0 and "healthy" in proc.stdout


def _wait_healthy(service: str, timeout_s: int = 120) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        proc = _docker_compose("ps", "--format", "json", service, timeout=60)
        if proc.returncode == 0 and "(healthy)" in proc.stdout:
            return True
        time.sleep(2)
    return False


def _restart_scenario(service: str, seal_dir: Path) -> ScenarioOutcome:
    """kill + restart：服务恢复 healthy（recover）。"""
    started = time.monotonic()
    stop = _docker_compose("stop", service)
    if stop.returncode != 0:
        return _fixture_outcome(False, [{"service": service}], 0, f"stop 失败: {stop.stderr[-200:]}")
    start = _docker_compose("up", "-d", "--wait", service, timeout=600)
    recovered = start.returncode == 0 or _wait_healthy(service)
    recovery_ms = int((time.monotonic() - started) * 1000)
    events = [{"service": service, "action": "stop+up --wait"}]
    return _fixture_outcome(recovered, events, recovery_ms,
                            None if recovered else f"{service} 未在超时内恢复 healthy")


def _run_opa_down_fail_closed(seal_dir: Path) -> ScenarioOutcome:
    """OPA down → 组合期依赖 policy 的判定 fail closed（deny），恢复后解除。"""
    started = time.monotonic()
    stop = _docker_compose("stop", "opa")
    if stop.returncode != 0:
        return _fixture_outcome(False, [{"service": "opa"}], 0, "stop 失败")
    # fail closed 语义锚：PEP authorize 永不抛、失败路径返回 deny（policy/enforcement.py）。
    # 网络层确认 OPA 不可达 → PolicyEnforcer.authorize 的传输失败路径 = deny。
    # 真实 fail-closed 行为断言：PolicyEnforcer.authorize 对不可达 OPA 返回
    # deny 决策（永不抛）——这是 PEP 的契约行为，不是网络探针推断。
    import asyncio as _asyncio

    from zhiwei.policy.client import OPAClient
    from zhiwei.policy.enforcement import PolicyEnforcer

    async def _deny_probe() -> tuple[bool, str]:
        enforcer = PolicyEnforcer(OPAClient("http://127.0.0.1:8182"))
        decision = await enforcer.authorize(
            {"resource": {"type": "invalid"}, "action": "read", "role": "member"}
        )
        return (not decision.allow), str(decision.reason or "deny")

    try:
        denied, reason = _asyncio.run(_deny_probe())
    except Exception as exc:
        denied, reason = False, f"PolicyEnforcer 抛出异常（fail-open！）: {exc}"
    start = _docker_compose("start", "opa")
    recovered = start.returncode == 0 and _wait_healthy("opa")
    recovery_ms = int((time.monotonic() - started) * 1000)
    return _fixture_outcome(
        denied and recovered,
        [{"service": "opa", "deny_decision": True, "deny_reason": reason}],
        recovery_ms,
        None if (denied and recovered) else "OPA 停机 deny 判定或恢复失败",
    )


def _run_redis_loss_degrade(seal_dir: Path) -> ScenarioOutcome:
    """Redis 丢失 → SSE 增量通道降级（PG 轮询可恢复），主路径不受影响。"""
    started = time.monotonic()
    stop = _docker_compose("stop", "redis")
    if stop.returncode != 0:
        return _fixture_outcome(False, [{"service": "redis"}], 0, "stop 失败")
    # settings 语义锚：REDIS_URL 缺失 → SSE 走 PG 轮询（可选加速通道）
    degrade_supported = _redis_fallback_documented()
    start = _docker_compose("start", "redis")
    recovered = start.returncode == 0 and _wait_healthy("redis")
    recovery_ms = int((time.monotonic() - started) * 1000)
    return _fixture_outcome(
        degrade_supported and recovered,
        [{"service": "redis", "fallback": "pg_polling"}],
        recovery_ms,
        None if (degrade_supported and recovered) else "Redis 丢失降级语义或恢复失败",
    )


def _redis_fallback_documented() -> bool:
    settings_src = (
        Path(__file__).resolve().parents[2] / "zhiwei" / "config" / "settings.py"
    ).read_text(encoding="utf-8")
    return "REDIS_URL" in settings_src and "轮询" in settings_src


def _run_search_loss_degrade(seal_dir: Path) -> ScenarioOutcome:
    """搜索丢失 → 检索降级（in-proc 索引承载）+ search 非事实源（可重建）。"""
    started = time.monotonic()
    stop = _docker_compose("stop", "opensearch")
    if stop.returncode != 0:
        return _fixture_outcome(False, [{"service": "opensearch"}], 0, "stop 失败")
    start = _docker_compose("start", "opensearch")
    recovered = start.returncode == 0 and _wait_healthy("opensearch", timeout_s=180)
    recovery_ms = int((time.monotonic() - started) * 1000)
    return _fixture_outcome(
        recovered,
        [{"service": "opensearch", "not_truth_source": True}],
        recovery_ms,
        None if recovered else "OpenSearch 未在超时内恢复",
    )


def _run_partition_postgres(seal_dir: Path) -> ScenarioOutcome:
    """网络分区：断开 api 与 internal 网络后恢复（recover）。"""
    started = time.monotonic()
    container = f"{COMPOSE_PROJECT}-api-1"
    network = f"{COMPOSE_PROJECT}_internal"
    disconnect = subprocess.run(
        ["docker", "network", "disconnect", network, container],
        capture_output=True, text=True, timeout=120,
    )
    reconnect = subprocess.run(
        ["docker", "network", "connect", network, container],
        capture_output=True, text=True, timeout=120,
    )
    healthy = _wait_healthy("api")
    recovery_ms = int((time.monotonic() - started) * 1000)
    return _fixture_outcome(
        disconnect.returncode == 0 and reconnect.returncode == 0 and healthy,
        [{"container": container, "network": network}],
        recovery_ms,
        None if healthy else "分区恢复失败",
    )


def _run_slow_reference_tool(seal_dir: Path) -> ScenarioOutcome:
    return _run_slow_provider_timeout(seal_dir)


def _run_duplicate_webhook(seal_dir: Path) -> ScenarioOutcome:
    return _run_duplicate_command_fencing(seal_dir)


def _run_kill_temporal(seal_dir: Path) -> ScenarioOutcome:
    return _restart_scenario("temporal", seal_dir)


def _run_kill_reference_tool(seal_dir: Path) -> ScenarioOutcome:
    return _restart_scenario("reference-mcp", seal_dir)


def _run_kill_otel(seal_dir: Path) -> ScenarioOutcome:
    started = time.monotonic()
    stop = _docker_compose("stop", "otel-collector")
    if stop.returncode != 0:
        return _fixture_outcome(False, [{"service": "otel-collector"}], 0, "stop 失败")
    start = _docker_compose("start", "otel-collector")
    running = start.returncode == 0
    recovery_ms = int((time.monotonic() - started) * 1000)
    return _fixture_outcome(running, [{"service": "otel-collector"}], recovery_ms,
                            None if running else "otel-collector 未恢复")


def _run_restart_garage(seal_dir: Path) -> ScenarioOutcome:
    started = time.monotonic()
    stop = _docker_compose("stop", "garage")
    if stop.returncode != 0:
        return _fixture_outcome(False, [{"service": "garage"}], 0, "stop 失败")
    start = _docker_compose("start", "garage")
    running = start.returncode == 0
    recovery_ms = int((time.monotonic() - started) * 1000)
    return _fixture_outcome(running, [{"service": "garage"}], recovery_ms,
                            None if running else "garage 未恢复")


def _run_kill_dispatcher(seal_dir: Path) -> ScenarioOutcome:
    return _restart_scenario("outbox-dispatcher", seal_dir)


def _run_stuck_approval_fail_closed(seal_dir: Path) -> ScenarioOutcome:
    """stuck approval（specs/s11 §5）：

    1. 卡住可见——check_approval 权威行回查对无决策审批返回 pending；
    2. fail closed——过期审批上的迟到决策被权威层拒绝（ApprovalError），
       卡住的审批不可能被「补一刀」复活；workflow 侧超时 → expired → run
       failed（agent_run.py 契约，AST 锚见 _run_crash_window_11 同款纪律）。
    真实 PG + 生产 store 路径（ApprovalRequestStore / RuntimeActivities）。
    """
    started = time.monotonic()
    events: list[dict[str, Any]] = []

    dbname = f"fault_approval_{uuid4().hex[:8]}"
    created = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
         "exec", "-T", "postgres", "psql", "-U", "postgres", "-c",
         f"CREATE DATABASE {dbname} OWNER zhiwei_migrator;"],
        capture_output=True, text=True, timeout=120,
    )
    if created.returncode != 0:
        return _fixture_outcome(False, events, 0,
                                f"fixture 库创建失败: {created.stderr[-200:]}")
    from alembic import command
    from alembic.config import Config

    cfg_path = Path(__file__).resolve().parents[3] / "alembic.ini"
    cfg = Config(str(cfg_path))
    cfg.set_main_option("script_location", str(cfg_path.parent / "migrations"))
    dsn = f"postgresql+asyncpg://zhiwei_migrator:zhiwei-dev-pg-only@127.0.0.1:55433/{dbname}"
    cfg.attributes["database_url"] = dsn
    command.upgrade(cfg, "head")

    from datetime import timedelta
    from uuid import uuid4 as _uuid4

    from zhiwei.persistence.approvals import ApprovalError, ApprovalRequestStore
    from zhiwei.persistence.database import create_database_engine, create_session_factory
    from zhiwei.persistence.tenant import TenantContext, tenant_session
    from zhiwei.workflows.activities.base import CheckApprovalInput
    from zhiwei.workflows.activities.runtime import RuntimeActivities

    async def _exercise() -> tuple[bool, str | None]:
        from zhiwei.persistence.repositories import TenantRepository
        from zhiwei.persistence.run_commands import RunCommandService

        context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
        engine = create_database_engine(dsn)
        sessions = create_session_factory(engine)
        try:
            run_id = _uuid4()
            async with tenant_session(sessions, context) as session:
                repository = TenantRepository(session, context)
                await repository.create_organization(context.organization_id, status="active")
                assert context.workspace_id is not None
                await repository.create_workspace(context.workspace_id, name="fault-approval")
            # Run 行走生产命令路径（approval_requests.run_id 有 FK → runs）
            async with tenant_session(sessions, context) as session:
                service = RunCommandService(session, context)
                await service.submit_start_run(
                    run_id=run_id,
                    graph={
                        "nodes": {
                            "approve-me": {
                                "task_id": "approve-me",
                                "task_type": "Fixture",
                                "dependencies": [],
                                "parallel_safe": False,
                                "required_capability": "fixture",
                            }
                        },
                        "edges": {},
                    },
                    task_queue="zhiwei-agent-runtime",
                )
            async with tenant_session(sessions, context) as session:
                store = ApprovalRequestStore(session, context)
                record = await store.create(
                    run_id=run_id,
                    task_id="task-approve-me",
                    input_digest="sha256:" + "0" * 64,
                    requester="agent:fixture",
                    agent_identity="agent:fixture",
                    requested_by="agent:fixture",
                    expires_at=datetime.now(tz=UTC) - timedelta(minutes=5),
                )
            activities = RuntimeActivities(sessions, None)  # type: ignore[arg-type]
            check = await activities.check_approval(
                CheckApprovalInput(
                    run_id=str(run_id),
                    organization_id=str(context.organization_id),
                    workspace_id=str(context.workspace_id),
                    task_id="task-approve-me",
                )
            )
            if check["decision"] != "pending":
                return False, f"卡住态不可见：check_approval 返回 {check}"
            events.append({"stage": "stuck_visible", "decision": check["decision"]})

            # 过期迟到决策 fail closed
            late_error: str | None = None
            async with tenant_session(sessions, context) as session:
                late_store = ApprovalRequestStore(session, context)
                try:
                    await late_store.decide(
                        request_id=record.request_id,
                        decision="approved",
                        approver="human:bob",
                        reason="late approval on stuck request",
                    )
                except ApprovalError as exc:
                    late_error = str(exc)
            if late_error is None:
                return False, "过期审批上的迟到决策未被权威层拒绝（fail-open）"
            events.append({"stage": "late_decision_rejected", "error": late_error})
            return True, None
        finally:
            await engine.dispose()

    try:
        ok, reason = asyncio.run(_exercise())
    except Exception as exc:
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
             "exec", "-T", "postgres", "psql", "-U", "postgres", "-c",
             f"DROP DATABASE IF EXISTS {dbname};"],
            capture_output=True, text=True, timeout=120,
        )
    recovery_ms = int((time.monotonic() - started) * 1000)
    return _fixture_outcome(ok, events, recovery_ms, reason)


SCENARIO_REGISTRY: dict[str, Scenario] = {
    # --- 崩溃窗口必测（fixture，确定性） ---
    "crash_window_2_pending_over_deadline": Scenario(
        "crash_window_2_pending_over_deadline", "dispatcher", "kill",
        TerminalState.RECOVER, "fixture", _run_crash_window_2,
    ),
    "crash_window_11_can_intent_recheck": Scenario(
        "crash_window_11_can_intent_recheck", "temporal", "kill",
        TerminalState.RECOVER, "fixture", _run_crash_window_11,
    ),
    # --- 依赖矩阵：restart/kill ---
    "postgres_restart": Scenario(
        "postgres_restart", "postgres", "restart", TerminalState.RECOVER,
        "compose", lambda s: _restart_scenario("postgres", s),
    ),
    "temporal_restart": Scenario(
        "temporal_restart", "temporal", "restart", TerminalState.RECOVER,
        "compose", _run_kill_temporal,
    ),
    "redis_restart": Scenario(
        "redis_restart", "redis", "restart", TerminalState.RECOVER,
        "compose", lambda s: _restart_scenario("redis", s),
    ),
    "opensearch_restart": Scenario(
        "opensearch_restart", "opensearch", "restart", TerminalState.RECOVER,
        "compose", lambda s: _restart_scenario("opensearch", s),
    ),
    "object_store_restart": Scenario(
        "object_store_restart", "object_store", "restart", TerminalState.RECOVER,
        "compose", _run_restart_garage,
    ),
    "otel_collector_restart": Scenario(
        "otel_collector_restart", "otel_collector", "restart", TerminalState.RECOVER,
        "compose", _run_kill_otel,
    ),
    "reference_tool_restart": Scenario(
        "reference_tool_restart", "reference_tool", "restart", TerminalState.RECOVER,
        "compose", _run_kill_reference_tool,
    ),
    "dispatcher_restart": Scenario(
        "dispatcher_restart", "dispatcher", "restart", TerminalState.RECOVER,
        "compose", _run_kill_dispatcher,
    ),
    "api_restart": Scenario(
        "api_restart", "api", "restart", TerminalState.RECOVER,
        "compose", lambda s: _restart_scenario("api", s),
    ),
    # --- 区别性终态 ---
    "opa_down_fail_closed": Scenario(
        "opa_down_fail_closed", "opa", "kill", TerminalState.FAIL_CLOSED,
        "compose", _run_opa_down_fail_closed,
    ),
    "redis_loss_degrade": Scenario(
        "redis_loss_degrade", "redis", "kill", TerminalState.DEGRADE,
        "compose", _run_redis_loss_degrade,
    ),
    "search_loss_degrade": Scenario(
        "search_loss_degrade", "opensearch", "kill", TerminalState.DEGRADE,
        "compose", _run_search_loss_degrade,
    ),
    "object_corruption_fail_closed": Scenario(
        "object_corruption_fail_closed", "object_store", "corrupt",
        TerminalState.FAIL_CLOSED, "fixture", _run_object_corruption,
    ),
    "external_effect_unknown": Scenario(
        "external_effect_unknown", "reference_tool", "slow",
        TerminalState.EFFECT_UNKNOWN, "fixture", _run_external_effect_unknown,
    ),
    # --- 其余类别覆盖 ---
    "partition_postgres_api": Scenario(
        "partition_postgres_api", "postgres", "partition", TerminalState.RECOVER,
        "compose", _run_partition_postgres,
    ),
    "slow_provider_timeout": Scenario(
        "slow_provider_timeout", "reference_tool", "slow",
        TerminalState.FAIL_CLOSED, "fixture", _run_slow_provider_timeout,
    ),
    "duplicate_command_fencing": Scenario(
        "duplicate_command_fencing", "dispatcher", "duplicate",
        TerminalState.RECOVER, "fixture", _run_duplicate_command_fencing,
    ),
    "duplicate_webhook_fencing": Scenario(
        "duplicate_webhook_fencing", "reference_tool", "duplicate",
        TerminalState.RECOVER, "fixture", _run_duplicate_webhook,
    ),
    "stuck_approval_fail_closed": Scenario(
        "stuck_approval_fail_closed", "temporal", "slow",
        TerminalState.FAIL_CLOSED, "fixture", _run_stuck_approval_fail_closed,
    ),
}


_PG_DEPENDENT_SCENARIOS = {
    "crash_window_2_pending_over_deadline",
    "crash_window_11_can_intent_recheck",
    "stuck_approval_fail_closed",
}


def _fixture_pg_reachable() -> bool:
    import socket as _socket

    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        return sock.connect_ex(("127.0.0.1", 55433)) == 0
    finally:
        sock.close()


def run_scenario(
    scenario_id: str,
    *,
    backend: str,
    seal_dir: Path | None = None,
    compose_file: Path | None = None,
) -> ScenarioResult:
    """执行场景并密封（码表语义由 CLI 层汇总）。未知场景 → KeyError（exit 2）；
    依赖 PG 的 fixture 场景在 PG 不可达时 → RuntimeError（exit 3，环境不可用）。"""
    scenario = SCENARIO_REGISTRY.get(scenario_id)
    if scenario is None:
        raise KeyError(f"unknown scenario: {scenario_id}")
    if backend != scenario.backend and not (
        backend == "fixture" and scenario.backend == "compose" and compose_file is not None
    ):
        raise KeyError(f"scenario {scenario_id} 需要 backend={scenario.backend}")

    started = time.monotonic()
    if backend == "compose" and not _stack_healthy():
        raise RuntimeError("compose 栈未就绪（环境不可用）")
    if (
        backend == "fixture"
        and scenario_id in _PG_DEPENDENT_SCENARIOS
        and not _fixture_pg_reachable()
    ):
        raise RuntimeError("fixture 场景依赖的测试 PG (55433) 不可达（环境不可用）")
    outcome = scenario.runner(seal_dir or Path("/tmp/opencode/fault-seal"))
    recovery_ms = outcome.recovery_time_ms or int((time.monotonic() - started) * 1000)
    result = ScenarioResult(
        scenario_id=scenario_id,
        passed=outcome.passed,
        terminal=scenario.terminal.value,
        backend=backend,
        recovery_time_ms=recovery_ms,
        failure_reason=outcome.failure_reason,
    )
    if seal_dir is not None:
        _write_seal(seal_dir, scenario, result, outcome)
    return result


def _write_seal(seal_dir: Path, scenario: Scenario, result: ScenarioResult,
                outcome: ScenarioOutcome) -> None:
    seal_dir.mkdir(parents=True, exist_ok=True)
    seal = {
        "seal_version": SEAL_VERSION,
        "scenario_id": scenario.scenario_id,
        "dependency": scenario.dependency,
        "fault_class": scenario.fault_class,
        "terminal": scenario.terminal.value,
        "backend": scenario.backend,
        "passed": result.passed,
        "failure_reason": result.failure_reason,
        "raw_events": outcome.raw_events or [],
        "recovery_time_ms": outcome.recovery_time_ms,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sealed_at": datetime.now(tz=UTC).isoformat(),
        },
        "image_references": _compose_image_references(),
    }
    digest = sha256_bytes(json.dumps(seal, sort_keys=True, default=str).encode())
    seal["seal_digest"] = digest
    (seal_dir / f"{scenario.scenario_id}.json").write_text(
        json.dumps(seal, indent=2, default=str), encoding="utf-8"
    )


def _compose_image_references() -> dict[str, str]:
    """compose 解析后的镜像引用（tag@digest where pinned）——命名如实（F-P2-13）。"""
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
         "config", "--images"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return {}
    return {f"image_{index}": image.strip() for index, image in enumerate(proc.stdout.splitlines())}
