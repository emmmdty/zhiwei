"""S9 执行模式绑定层：Model/Source/Tool 替换声明与跨模式身份不变量。

specs/s9 §3：六种 EvalMode 只替换 Model/Source/Tool 绑定，AgentVersion/TaskGraph/
Runtime/Policy/Evidence 身份必须逐字段一致——漂移在收集处（assert_identity_invariant
与 BindingSet）fail closed，而不是等到密封后才发现不可比。live 是唯一有前置门禁的
模式：BindingSpec 的常规构造路径直接拒绝 live，只有 for_live 携带显式 operator token
才能产出；token 是审批凭据，绝不进入 manifest（manifest 才会被冻结复算）。

门禁是路径完备（path-total）的：pydantic 校验器只覆盖 model_validate，model_copy/
model_construct 会绕过它，因此 for_live 在实例上留私有哨兵，所有消费点
（BindingSet 组装、manifest 密封、身份不变量断言）经 ensure_live_gate 重查——
未过门的 live spec 无法被组装、密封或使用。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator, model_validator

from zhiwei.contracts.canonical import digest
from zhiwei.evals.domain import EvalMode

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

# 模块私有哨兵：只有 for_live 的构造路径能携带它。直接构造 live spec 的调用方
# 拿不到这个对象，从而无法绕过显式 operator token 门禁。
_OPERATOR_GATE: object = object()


def _is_live_mode(mode: object) -> bool:
    if not isinstance(mode, (EvalMode, str)):
        return False
    try:
        return EvalMode(mode) is EvalMode.LIVE
    except ValueError:
        return False


def _require_non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("value must be a non-empty string")
    return value


def _validate_digest(value: str) -> str:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError("identity digests must be lowercase SHA-256 digests")
    return value


class AgentIdentity(BaseModel):
    """跨全部执行模式保持不变的身份：AgentVersion/TaskGraph/Runtime/Policy/Evidence。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_version_id: UUID
    task_graph_digest: str
    runtime_digest: str
    policy_digest: str
    evidence_digest: str

    @field_validator("task_graph_digest", "runtime_digest", "policy_digest", "evidence_digest")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _validate_digest(value)


class ModelBinding(BaseModel):
    """模式替换后的 Model 绑定；ref 指向已登记的 provider endpoint，不内嵌 URL。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_ref: str
    model: str

    @field_validator("endpoint_ref", "model")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        return _require_non_empty(value)


class SourceBinding(BaseModel):
    """模式替换后的 Source 绑定；snapshot_ref 指向 replay/fixture 数据来源。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    snapshot_ref: str

    @field_validator("source_id", "snapshot_ref")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        return _require_non_empty(value)


class ToolBinding(BaseModel):
    """模式替换后的 Tool 绑定；human 模式以 human-rubric 类实现替换工具执行。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_id: str
    implementation_ref: str

    @field_validator("tool_id", "implementation_ref")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        return _require_non_empty(value)


def assert_identity_invariant(specs: Sequence[BindingSpec]) -> None:
    """跨模式身份不变量：任一身份字段漂移都拒绝，保证模式间结果可比。"""
    first = next(iter(specs), None)
    if first is None:
        return
    for spec in specs:
        ensure_live_gate(spec)
        if spec.identity != first.identity:
            raise ValueError(
                f"binding identity drift: mode {spec.mode.value!r} does not preserve "
                "the shared AgentVersion/TaskGraph/Runtime/Policy/Evidence identity"
            )


def ensure_live_gate(spec: BindingSpec) -> None:
    """live spec 的消费点门禁：非 for_live 产出的 live 实例一律拒绝。

    model_copy/model_construct 不经过任何校验器，构造级门禁对它们不可见；
    门禁因此必须在消费处重查（私有哨兵只由 for_live 写入），保证未过门的
    live spec 无法组装、密封或参与不变量断言。
    """
    if not _is_live_mode(spec.mode):
        return
    if spec._live_gate is not _OPERATOR_GATE:
        raise ValueError(
            "live binding spec did not pass the for_live operator gate "
            "(live specs can only be produced by BindingSpec.for_live)"
        )


class BindingSpec(BaseModel):
    """一种执行模式的绑定声明：mode 替换哪些 Model/Source/Tool，身份保持什么。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EvalMode
    identity: AgentIdentity
    model: ModelBinding | None = None
    sources: tuple[SourceBinding, ...] = ()
    tools: tuple[ToolBinding, ...] = ()

    # 私有门禁哨兵：不在字段面（extra=forbid 不受影响），只由 for_live 写入。
    # model_copy 会连带复制私有属性，model_construct 留默认值——两者在消费点
    # 由 ensure_live_gate 区分。
    _live_gate: object = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def _refuse_implicit_live(cls, data: object) -> object:
        if isinstance(data, BindingSpec):
            # 已构造的 live 实例只可能来自 for_live；重校验放行。
            return data
        if isinstance(data, dict) and _is_live_mode(data.get("mode")):
            # 复制后再弹哨兵：调用方传入的 dict 是引用，原地 pop 会让被拒绝方
            # 观察到副作用（伪造门禁痕迹被消费）。
            data = dict(data)
            gate = data.pop("_operator_gate", None)
            if gate is not _OPERATOR_GATE:
                raise ValueError(
                    "live bindings can only be created via BindingSpec.for_live "
                    "(explicit operator gate)"
                )
        return data

    @classmethod
    def for_live(
        cls,
        *,
        identity: AgentIdentity,
        model: ModelBinding | None = None,
        sources: tuple[SourceBinding, ...] = (),
        tools: tuple[ToolBinding, ...] = (),
        operator_token: str,
    ) -> BindingSpec:
        """live 模式唯一构造入口：operator 必须显式传入非空审批 token。"""
        if not isinstance(operator_token, str) or not operator_token.strip():
            raise ValueError("live binding requires an explicit non-empty operator token")
        # 门禁哨兵经原始 payload 传递（model_validate 不做形参检查），before 校验器
        # 消费后弹出，pydantic 的 extra=forbid 校验不受影响；消费点哨兵在实例
        # 私有属性上留痕，供 ensure_live_gate 重查。
        payload: dict[str, Any] = {
            "mode": EvalMode.LIVE,
            "identity": identity,
            "model": model,
            "sources": sources,
            "tools": tools,
            "_operator_gate": _OPERATOR_GATE,
        }
        spec = cls.model_validate(payload)
        spec._live_gate = _OPERATOR_GATE
        return spec

    @property
    def manifest(self) -> dict[str, Any]:
        """可冻结的绑定 manifest；只含声明数据，绝不含 operator token。"""
        ensure_live_gate(self)
        return {
            "mode": self.mode.value,
            "identity": {
                "agent_version_id": str(self.identity.agent_version_id),
                "task_graph_digest": self.identity.task_graph_digest,
                "runtime_digest": self.identity.runtime_digest,
                "policy_digest": self.identity.policy_digest,
                "evidence_digest": self.identity.evidence_digest,
            },
            "model": None
            if self.model is None
            else {"endpoint_ref": self.model.endpoint_ref, "model": self.model.model},
            "sources": [
                {"source_id": source.source_id, "snapshot_ref": source.snapshot_ref}
                for source in self.sources
            ],
            "tools": [
                {"tool_id": tool.tool_id, "implementation_ref": tool.implementation_ref}
                for tool in self.tools
            ],
        }

    @property
    def manifest_digest(self) -> str:
        """manifest 的 canonical digest；冻结到 EvalRun 前的稳定性由 canonical JSON 保证。"""
        return digest(self.manifest)


class BindingSet(BaseModel):
    """一次评测声明的一组模式绑定：构造期断言身份一致且模式不重复。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: AgentIdentity
    specs: tuple[BindingSpec, ...]

    @model_validator(mode="after")
    def _assert_shared_identity(self) -> BindingSet:
        for spec in self.specs:
            # 组装是消费点：未过 for_live 门的 live spec 不得进入 BindingSet。
            ensure_live_gate(spec)
        assert_identity_invariant(self.specs)
        for spec in self.specs:
            if spec.identity != self.identity:
                raise ValueError(f"binding set identity does not match mode {spec.mode.value!r}")
        modes = [spec.mode for spec in self.specs]
        if len(set(modes)) != len(modes):
            raise ValueError("a binding set cannot bind the same mode twice")
        return self
