"""Blind holdout 密钥访问边界（specs/s9 §3/§4：internal frozen + blind holdout）。

blind holdout 题面不入库，只有持有密钥材料的 operator 能解锁：

- 密钥只能通过显式 typed 参数（`HoldoutKey`）进入——绝不读 env、绝不扫描文件。
  原因：holdout 的价值在于「执行前不可见」，任何自动发现路径（env/文件）都会
  扩大泄露面，且会让「没解锁」和「已解锁」的状态变得不可审计。
- 未提供密钥 → runner 得到 unavailable + 机器可读理由 `holdout_key_missing`
  （不是异常：这是部署处合法的未解锁状态，Gate 要求 claim 保持 planned，
  不得生成空成功报告）。
- 密钥错误 → fail closed 异常（`HoldoutKeyInvalid`）：错误密钥意味着越权尝试，
  静默降级为 unavailable 会掩盖它。

注册表只保存密钥的 sha256 指纹——密钥本体永不入库、永不入日志。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from zhiwei.evals.external.base import (
    AVAILABLE,
    BLIND_HOLDOUT_SCOPE,
    UNAVAILABLE,
    diagnostic_scope,
)

HOLDOUT_KEY_MISSING = "holdout_key_missing"


@dataclass(frozen=True, slots=True)
class HoldoutKey:
    """显式传入的 holdout 密钥材料；这是密钥进入系统的唯一合法入口。"""

    material: str

    def __post_init__(self) -> None:
        if not self.material:
            raise ValueError("holdout key 材料不能为空")

    @property
    def digest(self) -> str:
        """密钥的 sha256 指纹（hex）；比较用指纹，不用明文。"""
        return hashlib.sha256(self.material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HoldoutSuiteSpec:
    """一个 blind holdout suite 的注册声明（key_digest 是 operator 持钥的指纹）。"""

    name: str
    source_suite: str
    claim_id: str
    key_digest: str


_registry: dict[str, HoldoutSuiteSpec] = {}


def register_holdout_suite(spec: HoldoutSuiteSpec) -> HoldoutSuiteSpec:
    """注册 holdout suite；重复名 fail closed。"""
    if spec.name in _registry:
        raise ValueError(f"holdout suite 重复注册: {spec.name}")
    _registry[spec.name] = spec
    return spec


def resolve_holdout_suite(name: str) -> HoldoutSuiteSpec:
    """按名解析 holdout suite；未知名称 fail closed（LookupError）。"""
    try:
        return _registry[name]
    except KeyError:
        raise LookupError(f"未知 holdout suite: {name}") from None


@dataclass(frozen=True, slots=True)
class HoldoutSuiteAccess:
    """一次解锁尝试的结果：状态 + 机器可读理由 + 命中时的密钥指纹。"""

    suite: str
    source_suite: str
    claim_id: str
    scope: str
    status: str
    reasons: tuple[dict[str, str], ...]
    key_digest: str | None


class HoldoutKeyInvalid(RuntimeError):
    """holdout key 校验失败——fail closed，不降级为 unavailable。"""


def unlock_holdout_suite(name: str, key: HoldoutKey | None) -> HoldoutSuiteAccess:
    """按显式 typed 参数解锁 holdout suite；env/文件发现路径在这里不存在。"""
    spec = resolve_holdout_suite(name)
    scope = diagnostic_scope(BLIND_HOLDOUT_SCOPE, name)
    if key is None:
        return HoldoutSuiteAccess(
            suite=name,
            source_suite=spec.source_suite,
            claim_id=spec.claim_id,
            scope=scope,
            status=UNAVAILABLE,
            reasons=(
                {
                    "code": HOLDOUT_KEY_MISSING,
                    "path": f"holdout:{name}",
                    "detail": "未显式提供 holdout key（typed 参数）；不读 env、不做文件发现",
                },
            ),
            key_digest=None,
        )
    digest = key.digest
    if digest != spec.key_digest:
        raise HoldoutKeyInvalid(f"blind holdout {name} 密钥校验失败（fail closed）")
    return HoldoutSuiteAccess(
        suite=name,
        source_suite=spec.source_suite,
        claim_id=spec.claim_id,
        scope=scope,
        status=AVAILABLE,
        reasons=(),
        key_digest=digest,
    )
