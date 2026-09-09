"""S11 compose worker 进程入口：`python -m zhiwei.workers.main <role>`。

三个角色对应 local-product 的三个 worker 容器（specs/s11 §2）：

- `agent`：Temporal worker（AgentRunWorkflow + TTL sweep + Source Ledger activities），
  挂契约 handler registry（fixture 模式的任务原语全集）；连接后幂等注册 TTL schedule
  （F-R6-06 的生产组装点落位）。
- `dispatcher`：outbox 后台轮询（崩溃窗口 #2 的恢复承载）。**双角色纪律**：
  发现连接（ZHIWEI_DISPATCHER_DISCOVERY_URL，bypass 角色）只做租户对发现；
  载荷连接（ZHIWEI_DATABASE_URL，app 角色）经租户上下文会话走
  SessionOutboxRepository 的 fenced claim——RLS 在 app 角色上真实生效
  （bypass 角色下 GUC 不解除 BYPASSRLS，两种连接混用会让 RLS 形同虚设）。
  绕过 RLS 的查询只做「发现租户对」，不读消息载荷。
- `capability-runner`：Temporal worker（ToolActivity），策略判定走真实 OPA，
  IPC secret 从挂载文件读（缺失即退出，fail closed）。

配置只来自环境变量（load_settings 不读 .env）；缺 Temporal target / DSN 在启动期
退出。fixtures-only：入口不读 OPENAI_*，也不发起模型请求。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from temporalio.client import Client

from zhiwei.config.settings import load_settings
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.tenant import TenantContext
from zhiwei.runtime.outbox_handlers import OutboxSignalHandler
from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE as AGENT_QUEUE
from zhiwei.workers.agent_worker import build_agent_worker
from zhiwei.workers.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxDispatcherConfig,
    SessionOutboxRepository,
)
from zhiwei.workers.schedules import ensure_memory_ttl_schedule
from zhiwei.workers.temporal_sender import TemporalWorkflowSender
from zhiwei.workflows.activities.tools import ToolActivityInput, ToolActivityOutput

logger = logging.getLogger("zhiwei.workers.main")

DISPATCHER_DISCOVERY_INTERVAL = timedelta(seconds=2)
TTL_SWEEP_INTERVAL_SECONDS = 3600


def _require(value: str | None, what: str) -> str:
    if not value:
        logger.error("缺少 %s（环境变量未配置）", what)
        raise SystemExit(1)
    return value


async def _connect_temporal() -> Client:
    settings = load_settings()
    target = _require(settings.temporal_target, "ZHIWEI_TEMPORAL_TARGET")
    return await Client.connect(target)


async def run_agent() -> int:
    client = await _connect_temporal()
    settings = load_settings()
    dsn = _require(
        settings.database_url.get_secret_value() if settings.database_url else None,
        "ZHIWEI_DATABASE_URL",
    )
    # 契约 handler registry：fixture 模式下任务原语全集（stable/observe/decision…）。
    # 延迟 import 避免把 evals 执行器拉进最常见路径。
    from zhiwei.evals.executors.agent_runtime import build_contract_registry

    engine = create_database_engine(dsn)
    sessions = create_session_factory(engine)
    # §2.4 version marker：build id 从部署期 env 注入（upgrade manifest preflight 对位）
    worker = build_agent_worker(
        client,
        session_factory=sessions,
        handler_registry=build_contract_registry(),
        task_queue=AGENT_QUEUE,
        build_id=os.environ.get("ZHIWEI_WORKER_BUILD_ID") or "dev-local",
    )
    await ensure_memory_ttl_schedule(
        client,
        task_queue=AGENT_QUEUE,
        interval_seconds=TTL_SWEEP_INTERVAL_SECONDS,
    )
    _touch_ready()
    async with worker:
        logger.info("agent worker 就绪 queue=%s", AGENT_QUEUE)
        await asyncio.Event().wait()  # 运行到进程终止（compose stop 发 SIGTERM）
    return 0


async def run_dispatcher() -> int:
    client = await _connect_temporal()
    settings = load_settings()
    # 双角色纪律（对抗审查 F-P0-1 修复）：
    # - 发现连接（bypass 角色）：只跑租户对发现 SQL，不读消息载荷；
    # - 载荷连接（app 角色，NOBYPASSRLS）：SessionOutboxRepository 的 claim/读取
    #   经 tenant_session 设 GUC 后 RLS 真实生效。bypass 角色下 GUC 不解除
    #   BYPASSRLS——把两种连接混在一个 engine 上会让 RLS 形同虚设。
    dsn = _require(
        settings.database_url.get_secret_value() if settings.database_url else None,
        "ZHIWEI_DATABASE_URL（载荷面，app 角色）",
    )
    discovery_dsn = _require(
        os.environ.get("ZHIWEI_DISPATCHER_DISCOVERY_URL"),
        "ZHIWEI_DISPATCHER_DISCOVERY_URL（发现面，bypass 角色）",
    )
    engine = create_database_engine(dsn)
    discovery_engine = create_database_engine(discovery_dsn)
    sessions = create_session_factory(engine)
    config = OutboxDispatcherConfig(worker_id=f"dispatcher-{uuid4().hex[:8]}")
    sender = TemporalWorkflowSender(client)

    async def _pending_tenant_pairs() -> list[tuple]:
        from sqlalchemy import text

        async with discovery_engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT DISTINCT organization_id, workspace_id FROM outbox "
                    "WHERE (status = 'pending' AND available_at <= :now) "
                    "OR (status = 'processing' AND lease_expires_at <= :now)"
                ),
                {"now": datetime.now(tz=UTC)},
            )
            return [(row[0], row[1]) for row in rows]

    first_successful_round = False
    while True:
        discovery_ok = False
        try:
            pairs = await _pending_tenant_pairs()
            discovery_ok = True
        except Exception:
            logger.exception("outbox 租户发现失败，下一轮重试")
            pairs = []
        for organization_id, workspace_id in pairs:
            # SessionOutboxRepository 每个操作自开租户事务（fenced claim）。
            # app 角色受 FORCE RLS 约束，GUC 是可见性的唯一开关——载荷读取
            # 全部经过租户上下文。
            context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
            repository = SessionOutboxRepository(sessions, context)
            dispatcher = OutboxDispatcher(repository, OutboxSignalHandler(sender), config)
            try:
                await dispatcher.poll_once()
            except Exception:
                logger.exception(
                    "outbox poll 失败 org=%s ws=%s，下一轮重试", organization_id, workspace_id
                )
        if discovery_ok and not first_successful_round:
            # readiness 语义：首轮「发现查询成功执行」才就绪——发现面配错
            # （比如 K8s 里误用 NOBYPASSRLS 角色导致零行/报错）不得假绿。
            first_successful_round = True
            _touch_ready()
        await asyncio.sleep(DISPATCHER_DISCOVERY_INTERVAL.total_seconds())


async def run_capability_runner() -> int:
    client = await _connect_temporal()
    settings = load_settings()
    opa_url = _require(settings.opa_base_url, "ZHIWEI_OPA_BASE_URL")
    master_key_file = _require(
        str(settings.identity_master_key_file) if settings.identity_master_key_file else None,
        "ZHIWEI_IDENTITY_MASTER_KEY_FILE",
    )
    ipc_secret = _read_ipc_secret(master_key_file)
    from temporalio import activity
    from temporalio.worker import Worker

    from zhiwei.capabilities.invocations import InvocationRepository
    from zhiwei.capabilities.runners.client import RunnerClient
    from zhiwei.capabilities.runners.contracts import RunnerKind, RunnerRegistry, RunnerSpec
    from zhiwei.capabilities.runners.prebuilt import PrebuiltRunner
    from zhiwei.capabilities.tool_gateway import ToolGateway
    from zhiwei.policy.client import OPAClient
    from zhiwei.policy.enforcement import PolicyEnforcer
    from zhiwei.workers.capability_runner import build_tool_activity

    runner_spec = RunnerSpec(
        id=uuid4(),
        name="local-product-prebuilt",
        kind=RunnerKind.PREBUILT,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    registry = RunnerRegistry()
    registry.register(PrebuiltRunner(runner_spec))
    gateway = ToolGateway(
        policy_enforcer=PolicyEnforcer(OPAClient(opa_url)),
        runner_client=RunnerClient(registry, ipc_secret=ipc_secret),
        invocation_repo=InvocationRepository(),
    )
    tool_impl = build_tool_activity(gateway, InvocationRepository())

    @activity.defn(name="execute_tool")
    async def execute_tool(input: ToolActivityInput) -> ToolActivityOutput:
        """Temporal activity 装饰面：ToolActivity 是普通类（单测直调），
        worker 组装需要 @activity.defn 包装（temporalio 运行期校验）。"""
        return await tool_impl.execute(input)

    worker = Worker(
        client,
        task_queue="zhiwei-capability-runner",
        activities=[execute_tool],
    )
    _touch_ready()
    async with worker:
        logger.info("capability runner 就绪")
        await asyncio.Event().wait()
    return 0


def _touch_ready() -> None:
    """readiness 标记：compose healthcheck 对 worker 容器检查 /tmp/ready（tmpfs 可写）。"""
    import pathlib

    pathlib.Path("/tmp/ready").write_text("ready", encoding="utf-8")


def _read_ipc_secret(master_key_file: str) -> bytes:
    """IPC secret 独立于 identity master key：从挂载的 secret 文件读，缺失即退出。"""
    import pathlib

    secret_file = pathlib.Path(master_key_file)
    if not secret_file.is_file():
        logger.error("IPC secret 文件不存在: %s", master_key_file)
        raise SystemExit(1)
    secret = secret_file.read_bytes().strip()
    if not secret:
        logger.error("IPC secret 文件为空（fail closed）")
        raise SystemExit(1)
    return secret


_ROLES = {
    "agent": run_agent,
    "dispatcher": run_dispatcher,
    "capability-runner": run_capability_runner,
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if len(sys.argv) != 2 or sys.argv[1] not in _ROLES:
        print("usage: python -m zhiwei.workers.main agent|dispatcher|capability-runner",
              file=sys.stderr)
        return 2
    try:
        return asyncio.run(_ROLES[sys.argv[1]]())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
