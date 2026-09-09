"""S9 eval campaign：冻结 suite 注册单位的精确划分与既有 EvalRun 运行时复用。

campaign 不引入第二套 Dataset/Suite/EvalRun：划分纯函数保证「每个注册单位恰好
出现在一个子运行」，子运行一律由 EvalFoundationService 创建（eval_runs.campaign_id
关联）。campaign 完成只从全部子运行 sealed 推导——partial 可 resume，未全部终态
绝不完成；已完成的 campaign 是终态，不再接受任何子运行转移。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.time import utc_now
from zhiwei.evals.domain import EvalMode, RegisteredUnit, sorted_unique_units, unit_sort_key
from zhiwei.evals.runs import (
    CreateEvalRunCommand,
    EvalFoundationService,
    EvalRunNotFound,
    EvalStateError,
    RunPhase,
)
from zhiwei.object_store.ports import ObjectStore
from zhiwei.persistence.models import EvalCampaign, EvalRun
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired


class CampaignStatus(StrEnum):
    """campaign 生命周期状态；completed 是终态，只由全部子运行 sealed 推导。"""

    RUNNING = "running"
    PARTIAL = "partial"
    COMPLETED = "completed"


def derive_campaign_status(child_statuses: Iterable[RunPhase]) -> CampaignStatus:
    """campaign 状态只从子运行状态推导：全部 sealed 才 completed，绝不提前。"""
    statuses = tuple(child_statuses)
    if statuses and all(status is RunPhase.SEALED for status in statuses):
        return CampaignStatus.COMPLETED
    if any(status is RunPhase.SEALED for status in statuses):
        return CampaignStatus.PARTIAL
    return CampaignStatus.RUNNING


def partition_units(
    registered_units: Sequence[RegisteredUnit], child_sizes: Sequence[int]
) -> tuple[tuple[RegisteredUnit, ...], ...]:
    """把冻结 registry 精确划分为 child_sizes 形状的子注册表。

    划分前先去重排序：重复输入即重叠（拒绝）。child_sizes 之和必须等于 registry
    大小——小于则存在漏覆盖单位（fail closed），大于则必然产生重复单位。
    """
    if not child_sizes:
        raise ValueError("campaign requires at least one child partition")
    if any(
        isinstance(size, bool) or not isinstance(size, int) or size <= 0 for size in child_sizes
    ):
        raise ValueError("child sizes must be positive integers")
    units = sorted_unique_units(tuple(registered_units))
    if sum(child_sizes) < len(units):
        raise ValueError(
            f"partition does not cover the frozen registry: "
            f"{sum(child_sizes)} child slots for {len(units)} registered units"
        )
    if sum(child_sizes) > len(units):
        raise ValueError(
            f"partition exceeds the frozen registry: "
            f"{sum(child_sizes)} child slots for {len(units)} registered units"
        )
    chunks: list[tuple[RegisteredUnit, ...]] = []
    cursor = 0
    for size in child_sizes:
        chunks.append(tuple(units[cursor : cursor + size]))
        cursor += size
    return tuple(chunks)


class CampaignPlan(BaseModel):
    """一次 campaign 的冻结划分：children 拼接恰好等于排序后的 registry。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_id: UUID
    suite_version: int
    registered_units: tuple[RegisteredUnit, ...]
    children: tuple[tuple[RegisteredUnit, ...], ...]

    @classmethod
    def partition(
        cls,
        *,
        suite_id: UUID,
        suite_version: int,
        registered_units: Sequence[RegisteredUnit],
        child_sizes: Sequence[int],
    ) -> CampaignPlan:
        units = sorted_unique_units(tuple(registered_units))
        return cls(
            suite_id=suite_id,
            suite_version=suite_version,
            registered_units=tuple(sorted(units, key=unit_sort_key)),
            children=partition_units(units, child_sizes),
        )


class CreateCampaignCommand(BaseModel):
    """创建一次 campaign；manifest ids 为可选的操作员冻结输入，透传给每个子运行。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EvalMode
    suite_id: UUID
    suite_version: int
    registered_units: tuple[RegisteredUnit, ...]
    child_sizes: tuple[int, ...]
    code_digest: str
    config_digest: str
    schema_digest: str
    prereg_manifest_id: UUID | None = None
    model_manifest_id: UUID | None = None
    source_manifest_id: UUID | None = None
    attempt_manifest_id: UUID | None = None


class CreatedCampaign(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    eval_run_ids: tuple[UUID, ...]
    unit_count: int
    organization_id: UUID
    workspace_id: UUID


class CampaignChildStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_run_id: UUID
    status: RunPhase


class CampaignStatusView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    status: CampaignStatus
    unit_count: int
    children: tuple[CampaignChildStatus, ...]


class EvalCampaignNotFound(LookupError):
    """Raised when the campaign is absent from the explicit tenant scope."""


def _campaign_status(value: str) -> CampaignStatus:
    try:
        return CampaignStatus(value)
    except ValueError as exc:
        raise EvalStateError(f"campaign status is unknown: {value!r}") from exc


def _run_phase(value: str) -> RunPhase:
    try:
        return RunPhase(value)
    except ValueError as exc:
        raise EvalStateError(f"eval run status is unknown: {value!r}") from exc


class EvalCampaignService:
    """campaign 应用服务：划分经 EvalFoundationService 落成真实子 EvalRun。"""

    def __init__(
        self,
        session: AsyncSession,
        context: TenantContext | None,
        store: ObjectStore,
    ) -> None:
        if context is None or context.workspace_id is None:
            raise TenantContextRequired("eval campaigns require workspace context")
        self._runs = EvalFoundationService(session, context, store)
        self._session = session
        self._context = context
        self._workspace_id = context.workspace_id

    async def create(self, command: CreateCampaignCommand) -> CreatedCampaign:
        plan = CampaignPlan.partition(
            suite_id=command.suite_id,
            suite_version=command.suite_version,
            registered_units=command.registered_units,
            child_sizes=command.child_sizes,
        )
        campaign_id = uuid4()
        self._session.add(
            EvalCampaign(
                id=campaign_id,
                organization_id=self._context.organization_id,
                workspace_id=self._workspace_id,
                suite_id=plan.suite_id,
                version=plan.suite_version,
                unit_count=len(plan.registered_units),
                status=CampaignStatus.RUNNING.value,
                schema_version=1,
            )
        )
        # eval_runs.campaign_id 的复合 FK 依赖 campaign 行先落库。
        await self._session.flush()
        children: list[UUID] = []
        for chunk in plan.children:
            created = await self._runs.create(
                CreateEvalRunCommand(
                    mode=command.mode,
                    registered_units=chunk,
                    dataset_payload={
                        "registered_units": [
                            {"sample_id": unit.sample_id, "unit_id": unit.unit_id}
                            for unit in chunk
                        ]
                    },
                    code_digest=command.code_digest,
                    config_digest=command.config_digest,
                    schema_digest=command.schema_digest,
                    campaign_id=campaign_id,
                    prereg_manifest_id=command.prereg_manifest_id,
                    model_manifest_id=command.model_manifest_id,
                    source_manifest_id=command.source_manifest_id,
                    attempt_manifest_id=command.attempt_manifest_id,
                )
            )
            children.append(created.eval_run_id)
        return CreatedCampaign(
            campaign_id=campaign_id,
            eval_run_ids=tuple(children),
            unit_count=len(plan.registered_units),
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
        )

    async def status(self, campaign_id: UUID) -> CampaignStatusView:
        """从子运行行推导当前状态（只读；不推进 campaign 行）。"""
        campaign = await self._load_campaign(campaign_id)
        children = await self._load_children(campaign_id)
        return CampaignStatusView(
            campaign_id=campaign_id,
            status=derive_campaign_status(_run_phase(child.status) for child in children),
            unit_count=campaign.unit_count,
            children=tuple(
                CampaignChildStatus(eval_run_id=child.id, status=_run_phase(child.status))
                for child in children
            ),
        )

    async def complete(self, campaign_id: UUID) -> CampaignStatusView:
        """campaign 完成转移：全部子运行 sealed 才允许，其余一律拒绝（fail closed）。"""
        campaign = await self._load_campaign(campaign_id, for_update=True)
        children = await self._load_children(campaign_id)
        if _campaign_status(campaign.status) is CampaignStatus.COMPLETED:
            raise EvalStateError("campaign is already completed")
        derived = derive_campaign_status(_run_phase(child.status) for child in children)
        if derived is not CampaignStatus.COMPLETED:
            unsealed = sum(1 for child in children if child.status != RunPhase.SEALED.value)
            raise EvalStateError(
                "campaign cannot complete until every child run is sealed; "
                f"{unsealed} unsealed"
            )
        campaign.status = derived.value
        campaign.updated_at = utc_now()
        await self._session.flush()
        return CampaignStatusView(
            campaign_id=campaign_id,
            status=derived,
            unit_count=campaign.unit_count,
            children=tuple(
                CampaignChildStatus(eval_run_id=child.id, status=_run_phase(child.status))
                for child in children
            ),
        )

    async def resume_child(self, campaign_id: UUID, eval_run_id: UUID) -> None:
        """恢复 campaign 内一个 partial 子运行；已完成的 campaign 是终态，拒绝转移。"""
        campaign = await self._load_campaign(campaign_id, for_update=True)
        if _campaign_status(campaign.status) is CampaignStatus.COMPLETED:
            raise EvalStateError("completed campaign cannot resume a child run")
        eval_run = await self._session.scalar(
            select(EvalRun).where(
                EvalRun.organization_id == self._context.organization_id,
                EvalRun.workspace_id == self._workspace_id,
                EvalRun.campaign_id == campaign_id,
                EvalRun.id == eval_run_id,
            )
        )
        if eval_run is None:
            raise EvalRunNotFound("child eval run is missing from the campaign")
        await self._runs.resume(eval_run_id)

    async def _load_campaign(self, campaign_id: UUID, *, for_update: bool = False) -> EvalCampaign:
        statement = select(EvalCampaign).where(
            EvalCampaign.organization_id == self._context.organization_id,
            EvalCampaign.workspace_id == self._workspace_id,
            EvalCampaign.id == campaign_id,
        )
        if for_update:
            statement = statement.with_for_update()
        campaign = await self._session.scalar(statement)
        if campaign is None:
            raise EvalCampaignNotFound("campaign is missing from tenant scope")
        return campaign

    async def _load_children(self, campaign_id: UUID) -> Sequence[EvalRun]:
        return tuple(
            (
                await self._session.scalars(
                    select(EvalRun)
                    .where(
                        EvalRun.organization_id == self._context.organization_id,
                        EvalRun.workspace_id == self._workspace_id,
                        EvalRun.campaign_id == campaign_id,
                    )
                    .order_by(EvalRun.created_at, EvalRun.id)
                )
            ).all()
        )
