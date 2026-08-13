"""Eval 领域最小原语：显式模式、注册单位与终态结果。

这些值对象是 `evals/` 的契约根：模式必须是六种显式取值之一（绝不把 fixture 与 live 混为一谈），
样本单位一旦注册就不可变，结果以 canonical result digest 绑定——digest 由构造期计算，调用方不能
传入一个与 result 无关的任意 digest。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field

from zhiwei.contracts.canonical import canonical_json, digest_bytes


class EvalMode(StrEnum):
    """评测执行模式。fixture/replay/offline 不产生 live 模型质量结论。"""

    FIXTURE = "fixture"
    REPLAY = "replay"
    OFFLINE = "offline"
    LIVE = "live"
    SHADOW = "shadow"
    HUMAN = "human"


class SampleStatus(StrEnum):
    """单个注册单位的生命周期状态。terminal 集合是完整分母的封闭定义。"""

    REGISTERED = "registered"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUSED = "refused"
    ERROR = "error"


TERMINAL_STATUSES = frozenset(
    {
        SampleStatus.COMPLETED,
        SampleStatus.FAILED,
        SampleStatus.REFUSED,
        SampleStatus.ERROR,
    }
)


def is_terminal(status: SampleStatus) -> bool:
    """`completed | failed | refused | error` 都是完整分母内的 terminal。"""
    return status in TERMINAL_STATUSES


class RegisteredUnit(BaseModel):
    """一个 (sample_id, unit_id) 注册单位；id 即标识，注册期后不可改写。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_id: str
    unit_id: str


def unit_sort_key(unit: RegisteredUnit) -> tuple[str, str]:
    """注册单位的稳定排序键；同一 sample 内按 unit 排序。"""
    return (unit.sample_id, unit.unit_id)


def unit_key(unit: RegisteredUnit) -> tuple[str, str]:
    """注册单位的内容寻址键；与排序键同构，可安全用作 dict/set 键。"""
    return (unit.sample_id, unit.unit_id)


def sorted_unique_units(units: tuple[RegisteredUnit, ...]) -> tuple[RegisteredUnit, ...]:
    """去重并按 (sample_id, unit_id) 排序；重复输入抛 ValueError。"""
    seen: set[tuple[str, str]] = set()
    unique: list[RegisteredUnit] = []
    for unit in units:
        key = (unit.sample_id, unit.unit_id)
        if key in seen:
            raise ValueError(f"duplicate registered unit: {unit.sample_id!r}/{unit.unit_id!r}")
        seen.add(key)
        unique.append(unit)
    return tuple(sorted(unique, key=unit_sort_key))


class SampleOutcome(BaseModel):
    """一个注册单位的终态结果；result_digest 由 canonical result 复算，不允许伪造。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: RegisteredUnit
    status: SampleStatus
    result: dict[str, Any]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def result_digest(self) -> str:
        """终端结果的内容寻址标识：digest(canonical_json(result))。"""
        return digest_bytes(canonical_json(self.result))
