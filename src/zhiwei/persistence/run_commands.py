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

from zhiwei.contracts.time import utc_now
from zhiwei.persistence.models import OutboxMessage, Run
from zhiwei.persistence.tenant import TenantContext
from zhiwei.runtime.commands import (
    CancelRun,
    PauseRun,
    ResumeRun,
    StartRun,
)

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
    ) -> None:
        """Create the Run row and the start_run command in one transaction."""

        from sqlalchemy import select

        from zhiwei.persistence.models import Workspace

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
