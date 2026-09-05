"""S8 runtime triggers：DiscoveryProgram trigger → S2 Runtime StartRun 生产路径。

事实源：specs/s8-discover-actions.md §3.1。

DiscoveryProgram 的所有 trigger（schedule、webhook、source delta）必须通过 S2
Runtime 的 StartRun 命令发起执行，不得绕过——Run 行 + outbox 命令由既有
RunCommandService 同事务落账，canonical event tracking / approval 门禁 /
evidence 绑定都因此可用。

Service identity 继承语义：后台 run 使用 DiscoveryProgram 的 service identity
（StartRun.requested_by），不继承触发者的 session/token/personal memory。
下游 S7 memory activity 对 SERVICE_ACCOUNT 主体的 personal memory 查询有
fail-closed 拒绝（zhiwei.workflows.activities.memory）——本模块保证 Discover 侧
发出的请求形态只声明 service account 主体、从不携带创建者身份。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from zhiwei.contracts.time import ensure_utc
from zhiwei.discover.programs import DiscoveryProgram, ProgramStatus, ProgramVersion
from zhiwei.discover.triggers import Trigger, WebhookTrigger
from zhiwei.persistence.run_commands import RunCommandService
from zhiwei.runtime.commands import StartRun


class TriggerFireError(RuntimeError):
    """trigger 触发的前置条件不成立——fail closed，不默认放行。"""


@dataclass(frozen=True)
class BackgroundRunContext:
    """后台 run 的主体上下文：service identity，无创建者身份、无 personal memory。"""

    principal_kind: str
    principal_id: str | None
    personal_memory_access: bool


class DiscoveryTriggerService:
    """把 DiscoveryProgram 的 trigger 转成 StartRun 命令（经既有 RunCommandService）。

    本服务不持有、不接受任何触发者 session/token：命令构造的入参只有程序与
    trigger，身份来源唯一（program.service_identity）。
    """

    def __init__(self, run_commands: RunCommandService) -> None:
        self._run_commands = run_commands
        self._watermarks: dict[str, object] = {}

    @staticmethod
    def background_run_context(program: DiscoveryProgram) -> BackgroundRunContext:
        """后台 run 的主体声明：service_account + personal memory 不可访问。"""
        return BackgroundRunContext(
            principal_kind="service_account",
            principal_id=program.service_identity,
            personal_memory_access=False,
        )

    @staticmethod
    def build_start_run(
        program: DiscoveryProgram,
        version: ProgramVersion,
        trigger: Trigger,
        *,
        now: datetime,
        run_id: UUID,
        graph: dict[str, object] | None = None,
        task_queue: str = "discover",
        webhook_secret: str | None = None,
    ) -> StartRun:
        """校验前置条件并构造 StartRun 命令（不落账——落账见 fire()）。"""
        now = ensure_utc(now)
        DiscoveryTriggerService._validate(program, version, trigger)
        if isinstance(trigger, WebhookTrigger):
            DiscoveryTriggerService._verify_webhook_secret(trigger, webhook_secret)
        return StartRun(
            run_id=run_id,
            task_queue=task_queue,
            graph=graph if graph is not None else DiscoveryTriggerService._discover_graph(version),
            requested_by=program.service_identity or "",
        )

    async def fire(
        self,
        program: DiscoveryProgram,
        version: ProgramVersion,
        trigger: Trigger,
        *,
        now: datetime,
        run_id: UUID,
        graph: dict[str, object] | None = None,
        task_queue: str = "discover",
        webhook_secret: str | None = None,
    ) -> StartRun:
        """构造 StartRun 并经既有 RunCommandService 落账（Run 行 + outbox 同事务）。

        走 submit_start_run 生产 API——不直接拼 outbox 行，Run 生命周期归 S2 管。
        """
        start_run = self.build_start_run(
            program,
            version,
            trigger,
            now=now,
            run_id=run_id,
            graph=graph,
            task_queue=task_queue,
            webhook_secret=webhook_secret,
        )
        payload_graph: dict[str, object] = {
            str(k): v for k, v in (start_run.graph or {}).items()
        }
        await self._run_commands.submit_start_run(
            run_id=start_run.run_id,
            graph=payload_graph,
            task_queue=start_run.task_queue,
            max_task_attempts=start_run.max_attempts,
            continue_as_new_after=start_run.continue_as_new_after,
            activity_timeout_seconds=start_run.activity_timeout_seconds,
            requested_by=start_run.requested_by,
        )
        return start_run

    def source_delta_changed(
        self,
        program: DiscoveryProgram,
        version: ProgramVersion,
        trigger: Trigger,
        *,
        observed: object,
        now: datetime,
    ) -> bool:
        """watermark 推进检测：watermark 未推进（重复投递/重试）不得重复触发 run。

        首次观察到 watermark 视为相对激活基线的 delta（返回 True）；之后只有
        watermark 值变化才再次为 True。生产部署应持久化上次 watermark
        （TriggerRecord 生命周期属 S8 后续任务）——当前进程内状态满足最小生产路径。
        """
        DiscoveryTriggerService._validate(program, version, trigger)
        if trigger.type != "source_delta":
            raise TriggerFireError(f"trigger {trigger.type} is not a source delta")
        key: str = trigger.model_dump_json()
        previous = self._watermarks.get(key)
        self._watermarks[key] = observed
        return previous is None or previous != observed

    @staticmethod
    def _validate(
        program: DiscoveryProgram, version: ProgramVersion, trigger: Trigger
    ) -> None:
        if program.status != ProgramStatus.ACTIVE:
            raise TriggerFireError(
                f"program {program.id} is in {program.status} status; only active programs fire"
            )
        if not program.service_identity:
            raise TriggerFireError(
                f"program {program.id} has no service identity; background runs refuse to start"
            )
        if version.id != program.current_version_id:
            raise TriggerFireError(
                f"trigger bound to version {version.id}, program current version is "
                f"{program.current_version_id}"
            )
        if isinstance(trigger, WebhookTrigger) and not trigger.path.startswith("discover/"):
            raise TriggerFireError(f"webhook path {trigger.path!r} is not a discover path")

    @staticmethod
    def _verify_webhook_secret(trigger: Trigger, webhook_secret: str | None) -> None:
        if not isinstance(trigger, WebhookTrigger):
            raise TriggerFireError(f"trigger {trigger.type} is not a webhook")
        if not webhook_secret:
            raise TriggerFireError("webhook secret missing; refusing to fire")
        digest = hashlib.sha256(webhook_secret.encode()).hexdigest()
        stored = trigger.secret_digest
        # 允许带算法前缀的 digest（Model: "SHA-256 digest of the shared secret"）；
        # 常数时间比较——digest 差异位不泄露给提前中断的时序观察者
        stored_hex = stored.split(":")[-1] if ":" in stored else stored
        if not hmac.compare_digest(digest.encode(), stored_hex.encode()):
            raise TriggerFireError("webhook secret digest mismatch")

    @staticmethod
    def _discover_graph(version: ProgramVersion) -> dict[str, object]:
        """Discover App 的 task graph 声明（进入 StartRun.graph，供 workflow 编排）。"""
        return {
            "app": "discover",
            "program_version_id": str(version.id),
            "service_identity_bound": True,
            "tasks": {
                "intake": {"type": "Intake"},
                "detect": {"type": "Detect", "depends_on": ["intake"]},
                "falsify": {"type": "Falsify", "depends_on": ["detect"]},
                "dedupe": {"type": "Dedupe", "depends_on": ["falsify"]},
                "triage": {"type": "EmitTriageFeed", "depends_on": ["dedupe"]},
            },
        }
