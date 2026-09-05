"""Metamorphic/fault suite 注册 API（specs/s9 §4：metamorphic/fault injection）。

- 派生完全确定性：metamorphic 单位 id 是源单位 id 的纯函数
  （`metamorphic:{transform}:{sample_id}`），不用 RNG/seed。源 suite 冻结 →
  同一派生逐字节可复现；任何随机性都会破坏「同一冻结 suite 派生同一套
  metamorphic 单位」的可复算契约。
- scope 标签 `metamorphic:*`、claim_status `diagnostic-only`：metamorphic 结果
  只是稳健性诊断，永不升级公开质量 claim（specs/s9 §4 naked diagnostic 只披露）。
- 派生源是既有冻结 suite 的 registered units（numeric-risk-v1 经 CHECKSUMS
  校验后只读加载）；evals/ 冻结资产本身零修改。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from zhiwei.evals.domain import RegisteredUnit
from zhiwei.evals.external.base import METAMORPHIC_SCOPE, diagnostic_scope
from zhiwei.evals.risk_suites import NUMERIC_RISK_V1, resolve_risk_suite

DIAGNOSTIC_ONLY_CLAIM = "diagnostic-only"


@dataclass(frozen=True, slots=True)
class MetamorphicTransform:
    """一个命名的确定性变换：源单位 → metamorphic 单位（id 的纯函数）。"""

    name: str
    apply: Callable[[RegisteredUnit], RegisteredUnit]


def label_flip(unit: RegisteredUnit) -> RegisteredUnit:
    """label 翻转：期望标签取对立面，健壮的系统应输出相反判定或拒绝。"""
    return RegisteredUnit(
        sample_id=f"metamorphic:label-flip:{unit.sample_id}",
        unit_id=f"label-flip:{unit.unit_id}",
    )


def perturb(unit: RegisteredUnit) -> RegisteredUnit:
    """扰动：等价改写输入，健壮的系统应保持同一判定。"""
    return RegisteredUnit(
        sample_id=f"metamorphic:perturb:{unit.sample_id}",
        unit_id=f"perturb:{unit.unit_id}",
    )


LABEL_FLIP = MetamorphicTransform(name="label-flip", apply=label_flip)
PERTURB = MetamorphicTransform(name="perturb", apply=perturb)


@dataclass(frozen=True, slots=True)
class MetamorphicSuiteDefinition:
    """metamorphic suite 的冻结视图：派生单位、scope 与源 suite 绑定。"""

    name: str
    source_suite: str
    transforms: tuple[str, ...]
    units: tuple[RegisteredUnit, ...]
    scope: str
    source_digest: str | None
    claim_status: str


_registry: dict[str, MetamorphicSuiteDefinition] = {}


def derive_metamorphic_units(
    units: Iterable[RegisteredUnit],
    transforms: Sequence[MetamorphicTransform],
) -> tuple[RegisteredUnit, ...]:
    """对每个源单位依序施加每个 transform；纯函数，无 RNG。"""
    return tuple(transform.apply(unit) for unit in units for transform in transforms)


def register_metamorphic_suite(
    name: str,
    source_suite: str,
    transforms: Sequence[MetamorphicTransform],
    *,
    source_units: tuple[RegisteredUnit, ...],
    source_digest: str | None = None,
) -> MetamorphicSuiteDefinition:
    """注册一个由既有 suite 派生的 metamorphic suite；重复名/空变换 fail closed。"""
    if name in _registry:
        raise ValueError(f"metamorphic suite 重复注册: {name}")
    if not transforms:
        raise ValueError(f"metamorphic suite {name} 至少需要一个 transform")
    definition = MetamorphicSuiteDefinition(
        name=name,
        source_suite=source_suite,
        transforms=tuple(transform.name for transform in transforms),
        units=derive_metamorphic_units(source_units, transforms),
        scope=diagnostic_scope(METAMORPHIC_SCOPE, name),
        source_digest=source_digest,
        claim_status=DIAGNOSTIC_ONLY_CLAIM,
    )
    _registry[name] = definition
    return definition


def resolve_metamorphic_suite(name: str) -> MetamorphicSuiteDefinition:
    """按名解析 metamorphic suite；未知名称 fail closed（LookupError）。"""
    try:
        return _registry[name]
    except KeyError:
        raise LookupError(f"未知 metamorphic suite: {name}") from None


NUMERIC_RISK_METAMORPHIC_V1 = "numeric-risk-v1-metamorphic"


def _register_builtin_suites() -> None:
    """登记内置 metamorphic suite：numeric-risk-v1 的 label-flip/perturb 派生。"""
    source = resolve_risk_suite(NUMERIC_RISK_V1)
    register_metamorphic_suite(
        NUMERIC_RISK_METAMORPHIC_V1,
        NUMERIC_RISK_V1,
        (LABEL_FLIP, PERTURB),
        source_units=source.units,
        source_digest=source.asset_digest,
    )


_register_builtin_suites()

# 供 CLI/测试引用的稳定只读视图（内置注册之后的快照）。
METAMORPHIC_SUITE_NAMES: frozenset[str] = frozenset(_registry)
