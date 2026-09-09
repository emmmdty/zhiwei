"""S9 Agent release 生命周期：状态机、SoD、不可变 manifest 与 PG 应用服务。

specs/s9 §5 冻结语义（tests/contract/release/test_release_domain_frozen.py）：
- 生命周期 draft→sandbox→evaluated→review→staged→published→deprecated/retired，
  跳级/回退/复活一律拒绝；retired 是终态；
- 角色分离（SoD）：builder 推进 draft 侧、reviewer 复核、approver 批准、
  release_manager 发布/退役——未知角色按未知处理拒绝（fail closed）；
- ReleaseManifest 冻结不可变：依赖 digest 全部进入 content_digest，任何依赖
  变化都改变 manifest 身份。

Release Service 把状态机落到 agent_releases 行：manifest payload/digest 列
无 UPDATE 路径（不可变投影），state 与 rollout_policy 是仅有的生命周期可变列
——活跃 rollout 策略独立于 manifest 存放，因为 rollback 改 default pin 时
manifest（含其 content_digest）必须保持逐字节不变。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.agents.rollout import (
    RollbackNotApplicable,
    RollbackOutcome,
    RollbackPolicy,
    RolloutPolicy,
    apply_rollback,
    route_version,
)
from zhiwei.contracts.canonical import digest
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.models import AgentReleaseRow
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired

__all__ = [
    "ALLOWED_RELEASE_TRANSITIONS",
    "ReleaseManifest",
    "ReleaseNotFound",
    "ReleaseRecord",
    "ReleaseService",
    "ReleaseState",
    "ReleaseTransitionDenied",
    "require_transition_permission",
    "validate_transition",
]

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ReleaseTransitionDenied(RuntimeError):
    """状态转移被拒：不在迁移矩阵内，或角色未获该转移授权（未知角色同此）。"""


class ReleaseNotFound(LookupError):
    """目标 release 不在显式租户作用域内（RLS 下跨租户同此语义）。"""


class ReleaseState(StrEnum):
    """Agent release 生命周期状态；retired 是唯一终态。"""

    DRAFT = "draft"
    SANDBOX = "sandbox"
    EVALUATED = "evaluated"
    REVIEW = "review"
    STAGED = "staged"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


# 迁移矩阵：每个合法转移的授权 release 角色。未列组合（跳级、回退、终态出边）
# 一律拒绝——SoD 的唯一事实，不在服务层复制第二套。
ALLOWED_RELEASE_TRANSITIONS: Mapping[tuple[ReleaseState, ReleaseState], frozenset[str]] = {
    (ReleaseState.DRAFT, ReleaseState.SANDBOX): frozenset({"builder"}),
    (ReleaseState.SANDBOX, ReleaseState.EVALUATED): frozenset({"builder"}),
    (ReleaseState.EVALUATED, ReleaseState.REVIEW): frozenset({"reviewer"}),
    (ReleaseState.REVIEW, ReleaseState.STAGED): frozenset({"approver"}),
    (ReleaseState.STAGED, ReleaseState.PUBLISHED): frozenset({"release_manager"}),
    (ReleaseState.PUBLISHED, ReleaseState.DEPRECATED): frozenset({"release_manager"}),
    (ReleaseState.DEPRECATED, ReleaseState.RETIRED): frozenset({"release_manager"}),
}


def validate_transition(current: ReleaseState, target: ReleaseState) -> None:
    """仅校验状态机矩阵（不含角色）；retired 无出边，deprecated 不可复活。"""
    if (current, target) not in ALLOWED_RELEASE_TRANSITIONS:
        raise ReleaseTransitionDenied(
            f"release transition {current.value} -> {target.value} is not allowed"
        )


def require_transition_permission(
    role: str, current: ReleaseState, target: ReleaseState
) -> None:
    """SoD 校验：未知角色与未授权角色同语义拒绝（不做「常见默认」推断）。"""
    validate_transition(current, target)
    if role not in ALLOWED_RELEASE_TRANSITIONS[(current, target)]:
        raise ReleaseTransitionDenied(
            f"role {role!r} may not advance release {current.value} -> {target.value}"
        )


def _sha256_digest(value: str) -> str:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError("digest must be a lowercase sha256 digest with algorithm prefix")
    return value


class ReleaseManifest(BaseModel):
    """发布清单：依赖 digest 全集 + 审批人 + rollout/rollback 计划（冻结不可变）。

    content_digest 只覆盖不可变依赖集（pack/model/knowledge/memory/capability/
    policy/eval digests）——agent/版本/审批人/rollout 计划是清单的记录字段而非
    依赖：同一依赖集在不同 agent 上必须产出相同依赖 digest（冻结契约以随机
    agent_id/selector_id 断言这一点），任一依赖变化即改变 digest。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: UUID
    agent_version: int = Field(ge=1)
    pack_digest: str
    model_digest: str
    knowledge_digest: str
    memory_digest: str
    capability_digest: str
    policy_digest: str
    eval_digests: tuple[str, ...] = ()
    approver: str = Field(min_length=1)
    rollout: RolloutPolicy
    rollback: RollbackPolicy

    @field_validator(
        "pack_digest",
        "model_digest",
        "knowledge_digest",
        "memory_digest",
        "capability_digest",
        "policy_digest",
    )
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _sha256_digest(value)

    @field_validator("eval_digests")
    @classmethod
    def _validate_eval_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _sha256_digest(item)
        return value

    @property
    def content_digest(self) -> str:
        # 用普通 property 而非 computed_field：payload 需要能直接 model_validate
        # 往返（持久化列写什么读回什么），computed_field 会把 digest 混进 dump
        # 再被 extra=forbid 拒绝。
        return digest(
            {
                "pack_digest": self.pack_digest,
                "model_digest": self.model_digest,
                "knowledge_digest": self.knowledge_digest,
                "memory_digest": self.memory_digest,
                "capability_digest": self.capability_digest,
                "policy_digest": self.policy_digest,
                "eval_digests": list(self.eval_digests),
            }
        )


class ReleaseRecord(BaseModel):
    """release 的服务层投影：不可变 manifest + 当前状态 + 活跃 rollout 策略。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_id: UUID
    agent_id: UUID
    agent_version: int
    state: ReleaseState
    manifest: ReleaseManifest
    rollout: RolloutPolicy


class ReleaseService:
    """Agent release 的 PostgreSQL 应用服务：行锁串行化推进状态机与活跃 pin。"""

    def __init__(self, session: AsyncSession | None, context: TenantContext | None) -> None:
        if session is None:
            raise TenantContextRequired("agent releases require a database session")
        if context is None or context.workspace_id is None:
            raise TenantContextRequired("agent releases require workspace context")
        self._session = session
        self._context = context

    async def create_draft(self, manifest: ReleaseManifest) -> ReleaseRecord:
        now = utc_now()
        release_id = new_id()
        self._session.add(
            AgentReleaseRow(
                id=release_id,
                organization_id=self._context.organization_id,
                workspace_id=self._context.workspace_id,
                agent_id=manifest.agent_id,
                agent_version=manifest.agent_version,
                state=ReleaseState.DRAFT.value,
                manifest_digest=manifest.content_digest,
                manifest=manifest.model_dump(mode="json"),
                rollout_policy=manifest.rollout.model_dump(mode="json"),
                schema_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()
        return ReleaseRecord(
            release_id=release_id,
            agent_id=manifest.agent_id,
            agent_version=manifest.agent_version,
            state=ReleaseState.DRAFT,
            manifest=manifest,
            rollout=manifest.rollout,
        )

    async def get(self, release_id: UUID) -> ReleaseRecord:
        row = await self._load(release_id, for_update=False)
        return self._record(row)

    async def list(self) -> list[ReleaseRecord]:
        rows = (
            await self._session.scalars(
                select(AgentReleaseRow)
                .where(
                    AgentReleaseRow.organization_id == self._context.organization_id,
                    AgentReleaseRow.workspace_id == self._context.workspace_id,
                )
                .order_by(AgentReleaseRow.created_at, AgentReleaseRow.id)
            )
        ).all()
        return [self._record(row) for row in rows]

    async def advance(
        self, release_id: UUID, *, target: ReleaseState, role: str
    ) -> ReleaseRecord:
        """持行锁推进状态机；矩阵与 SoD 在写库前判定，拒绝时不产生任何变更。"""
        row = await self._load(release_id, for_update=True)
        try:
            current = ReleaseState(row.state)
        except ValueError as exc:
            raise ReleaseTransitionDenied(
                f"stored release state is unknown: {row.state!r}"
            ) from exc
        validate_transition(current, target)
        require_transition_permission(role, current, target)
        row.state = target.value
        row.updated_at = utc_now()
        await self._session.flush()
        return self._record(row)

    async def route(
        self,
        release_id: UUID,
        *,
        workspace_id: UUID,
        user_id: UUID | None,
        suspended: bool = False,
    ) -> int:
        """用活跃 rollout 策略解析新 Run 的版本（只读，不落库）。"""
        record = await self.get(release_id)
        return route_version(
            record.rollout,
            workspace_id=workspace_id,
            user_id=user_id,
            suspended=suspended,
        )

    async def rollback(
        self,
        release_id: UUID,
        *,
        to_version: int,
        in_flight_run_ids: Sequence[UUID] = (),
    ) -> RollbackOutcome:
        """回滚 default pin（对新 Run 生效）；manifest 保持不变，在途 Run 只声明处置。"""
        row = await self._load(release_id, for_update=True)
        record = self._record(row)
        from_version = record.rollout.default_version
        if from_version is None:
            raise RollbackNotApplicable("release has no default version pin to roll back")
        outcome = apply_rollback(
            record.rollout,
            from_version=from_version,
            to_version=to_version,
            in_flight_run_ids=tuple(in_flight_run_ids),
            rollback=record.manifest.rollback,
        )
        row.rollout_policy = outcome.policy.model_dump(mode="json")
        row.updated_at = utc_now()
        await self._session.flush()
        return outcome

    async def _load(self, release_id: UUID, *, for_update: bool) -> AgentReleaseRow:
        statement = select(AgentReleaseRow).where(
            AgentReleaseRow.id == release_id,
            AgentReleaseRow.organization_id == self._context.organization_id,
            AgentReleaseRow.workspace_id == self._context.workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        if row is None:
            raise ReleaseNotFound("agent release is missing from tenant scope")
        return row

    @staticmethod
    def _record(row: AgentReleaseRow) -> ReleaseRecord:
        try:
            state = ReleaseState(row.state)
        except ValueError as exc:
            raise ReleaseTransitionDenied(f"stored release state is unknown: {row.state!r}") from exc
        return ReleaseRecord(
            release_id=row.id,
            agent_id=row.agent_id,
            agent_version=row.agent_version,
            state=state,
            manifest=ReleaseManifest.model_validate(row.manifest),
            rollout=RolloutPolicy.model_validate(row.rollout_policy),
        )
