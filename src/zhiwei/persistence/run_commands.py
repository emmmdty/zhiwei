"""S2 runtime: command side — Run 行 + outbox 命令在同一事务提交。

事实源：specs/s2-agent-runtime.md §4、S2-T4 plan。

start/signal 走 PG outbox + deterministic workflow id（`run-{run_id}`）；dispatch 由
OutboxDispatcher 完成（DB 成功 / Temporal 失败、重复投递、signal-before-worker、
poison、dispatcher crash 的语义见 tests/integration/runtime）。本模块只做「写真相 +
写命令」，不直接触碰 Temporal——不存在跨系统同时事务假设。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.models import OutboxMessage, Run
from zhiwei.persistence.tenant import TenantContext
from zhiwei.runtime.commands import (
    CancelRun,
    PauseRun,
    ResumeRun,
    StartRun,
)
from zhiwei.telemetry.traces import SpanNames, start_span

RUNTIME_COMMAND_TOPIC = "runtime.command"

_RUN_SCHEMA_VERSION = 1
_COMMAND_SCHEMA_VERSION = 1


class RunCommandError(RuntimeError):
    """Raised when a runtime command cannot be submitted."""


class RunCommandService:
    """Submit runtime commands transactionally (Run row + outbox message)."""

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        if context.workspace_id is None:
            raise RunCommandError("runtime commands require workspace context")
        self._session = session
        self._context = context

    async def submit_start_run(
        self,
        *,
        run_id: UUID,
        graph: dict[str, object],
        task_queue: str,
        max_task_attempts: int = 3,
        continue_as_new_after: int = 1000,
        activity_timeout_seconds: int = 60,
        requested_by: str = "system",
        delegation_chain: tuple[str, ...] = (),
        template: str | None = None,
    ) -> None:
        """Create the Run row and the start_run command in one transaction.

        delegation_chain 是 ADR-008 第②层的纵深防御：发布期环检测之外的
        运行时硬上界——超链在 Run 行写入前拒绝（fail closed），防发布校验
        被绕过或图在发布后被篡改。

        template 是创建期的 caller-declared planner 意图标识（S10 fix-A，
        0019 持久化）：API 层传入，eval/CLI 直连路径缺省 None——缺席是诚实
        语义，不猜默认值。
        """
        from sqlalchemy import select

        from zhiwei.persistence.models import Workspace
        from zhiwei.runtime.delegation import MAX_DELEGATION_DEPTH

        # S9 §6 run span：Run 行 + outbox 命令同事务提交的唯一入口。观测失败
        # 不吞异常（start_span 原样上抛），默认 NoOp provider 下零副作用。
        # agent 身份在提交期只能以 graph digest 表达：agent_version_id 在
        # release 流程才绑定到 Run 行（此处为 None），graph 是此刻决定执行
        # 语义的 agent 定义载荷，其 canonical digest 是最接近的稳定标识。
        with start_span(
            SpanNames.RUN,
            {
                "run_id": str(run_id),
                "agent_graph_digest": digest_bytes(canonical_json(dict(graph))),
                "task_queue": task_queue,
            },
        ):
            if len(delegation_chain) > MAX_DELEGATION_DEPTH:
                raise RunCommandError(
                    f"delegation chain length {len(delegation_chain)} exceeds hard "
                    f"depth cap {MAX_DELEGATION_DEPTH} (ADR-008 layer 2)"
                )
            workspace_exists = await self._session.scalar(
                select(Workspace).where(
                    Workspace.organization_id == self._context.organization_id,
                    Workspace.id == self._context.workspace_id,
                )
            )
            if workspace_exists is None:
                raise RunCommandError("workspace missing from tenant scope")
            self._session.add(
                Run(
                    id=run_id,
                    organization_id=self._context.organization_id,
                    workspace_id=self._context.workspace_id,
                    agent_version_id=None,
                    status="created",
                    template=template,
                    schema_version=_RUN_SCHEMA_VERSION,
                )
            )
            command = StartRun(
                run_id=run_id,
                task_queue=task_queue,
                max_attempts=max_task_attempts,
                graph=dict(graph),
                continue_as_new_after=continue_as_new_after,
                activity_timeout_seconds=activity_timeout_seconds,
                requested_by=requested_by,
                delegation_chain=tuple(delegation_chain),
            )
            self._add_command(command)
            await self._session.flush()

    async def submit_cancel_run(self, *, run_id: UUID, reason: str | None = None) -> None:
        command = CancelRun(run_id=run_id, reason=reason)
        self._add_command(command)
        await self._session.flush()

    async def submit_pause_run(self, *, run_id: UUID, reason: str | None = None) -> None:
        command = PauseRun(run_id=run_id, reason=reason)
        self._add_command(command)
        await self._session.flush()

    async def submit_resume_run(self, *, run_id: UUID) -> None:
        command = ResumeRun(run_id=run_id)
        self._add_command(command)
        await self._session.flush()

    def _add_command(self, command) -> None:
        now = utc_now()
        self._session.add(
            OutboxMessage(
                id=uuid4(),
                organization_id=self._context.organization_id,
                workspace_id=self._context.workspace_id,
                topic=RUNTIME_COMMAND_TOPIC,
                event_key=command.kind.value,
                payload=command.model_dump(mode="json"),
                status="pending",
                attempts=0,
                available_at=now,
                schema_version=_COMMAND_SCHEMA_VERSION,
                created_at=now,
            )
        )
