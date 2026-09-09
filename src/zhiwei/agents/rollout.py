"""S9 rollout 语义：cohort 路由、rollback 仅影响新 Run、security suspend 覆盖 pin。

specs/s9 §5 冻结语义（tests/contract/release/test_release_domain_frozen.py）：
- 路由优先级 user cohort > workspace cohort > default pin；
- 无 default 且无 cohort 命中 → RolloutNotConfigured（fail closed，不取「常见默认」）；
- security suspend 不受 release pin 保护：暂停一律拒绝路由——版本 pin 只描述
  发布意图，不能翻转安全挂起；
- rollback 只改 default pin（cohort 属 canary 计划，不由回滚改写），且只影响
  新 Run：在途 Run 的 complete/terminate 由 runtime 安全策略落地，域层只声明
  disposition（executed 恒 False），不发明第二套执行语义。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RolloutNotConfigured(RuntimeError):
    """路由无解：security suspend 生效，或无 cohort 命中且无 default pin。"""


class RollbackNotApplicable(RuntimeError):
    """回滚不适用：目标版本与当前 pin 相同，或当前没有可回退的 default pin。"""


class Cohort(BaseModel):
    """一个 canary 分组：workspace/user 选择器钉到指定 agent 版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["workspace", "user"]
    selector_id: UUID
    version: int = Field(ge=1)


class RolloutPolicy(BaseModel):
    """活跃路由策略：default pin + cohort 列表（服务层持久化，rollback 可改 default）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_version: int | None = Field(default=None, ge=1)
    cohorts: tuple[Cohort, ...] = ()


class RollbackPolicy(BaseModel):
    """在途 Run 的处置声明：完成或终止——由 runtime 安全策略执行，不在这里执行。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    in_flight: Literal["complete", "terminate"]


def route_version(
    policy: RolloutPolicy,
    *,
    workspace_id: UUID,
    user_id: UUID | None,
    suspended: bool = False,
) -> int:
    """解析某次新 Run 应使用的 agent 版本；无解一律抛 RolloutNotConfigured。"""
    if suspended:
        # security suspend 不受 release pin 保护：先于一切 pin 判定
        raise RolloutNotConfigured("security suspend overrides release pin")
    if user_id is not None:
        for cohort in policy.cohorts:
            if cohort.kind == "user" and cohort.selector_id == user_id:
                return cohort.version
    for cohort in policy.cohorts:
        if cohort.kind == "workspace" and cohort.selector_id == workspace_id:
            return cohort.version
    if policy.default_version is not None:
        return policy.default_version
    raise RolloutNotConfigured("no cohort match and no default version pin")


class RollbackOutcome(BaseModel):
    """回滚结果：新 pin 策略 + 在途 Run 处置声明（域层不执行终止）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: RolloutPolicy
    applies_to: Literal["new_runs_only"]
    executed: bool
    in_flight_disposition: Literal["complete", "terminate"]
    in_flight_run_ids: tuple[UUID, ...]


def apply_rollback(
    policy: RolloutPolicy,
    *,
    from_version: int,
    to_version: int,
    in_flight_run_ids: Iterable[UUID],
    rollback: RollbackPolicy,
) -> RollbackOutcome:
    """把 default pin 改回 to_version；cohort pin 原样保留，在途 Run 只声明处置。"""
    if to_version < 1:
        raise RollbackNotApplicable("rollback target version must be positive")
    if from_version == to_version:
        raise RollbackNotApplicable(
            f"version {to_version} is already the pinned default; nothing to roll back"
        )
    return RollbackOutcome(
        policy=RolloutPolicy(default_version=to_version, cohorts=policy.cohorts),
        applies_to="new_runs_only",
        executed=False,
        in_flight_disposition=rollback.in_flight,
        in_flight_run_ids=tuple(in_flight_run_ids),
    )
