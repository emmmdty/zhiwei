"""S1-T3 PEP helper：编排、默认拒绝与结果映射。

职责边界（防屎山）：
- 只做 PEP 编排：接收 PolicyInput（或待校验 dict），调用 client 求值，映射结果；
- 默认拒绝：输入非法（未知枚举/缺失证据/未声明字段）在边界转为 deny
  （policy_input_invalid），**不进入 OPA**；内部异常也转为 deny，authorize 永不抛；
- 每次调用都走 client 重新求值——不存在「复用已存决策」的旁路
  （总设计 §8.6：审批等待不冻结授权，执行前重读当前 PolicyBundle）；
- 授权语义不在此层（Rego 唯一事实），也不做 T4 的 audit 写入。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from zhiwei.policy.client import OPAClient, PolicyDecision
from zhiwei.policy.input import PolicyInput
from zhiwei.telemetry.traces import SpanNames, start_span


def _policy_type_of(policy_input: PolicyInput | Mapping[str, Any]) -> str:
    """span 面的 policy 类型标识；未校验的 mapping 不得采信其自报类型。"""
    if isinstance(policy_input, PolicyInput):
        return policy_input.resource.type.value
    return "unvalidated_input"


class PolicyEnforcer:
    def __init__(self, client: OPAClient) -> None:
        self._client = client

    async def authorize(self, policy_input: PolicyInput | Mapping[str, Any]) -> PolicyDecision:
        """对请求做授权判定；永不抛异常，所有失败路径返回 deny 决策。

        传 dict 时先做边界校验（严格 schema），校验失败直接 deny——未知
        resource/action/role/scope/classification/risk/purpose、SoD 证据缺失、
        extra 字段都在这里拦截，不会带着非法值进 OPA。client 的传输/畸形响应
        失败已由 client 折叠为 deny；这里再兜住任何内部错误（默认拒绝纪律：
        PEP 对调用方永不 500）。
        """
        # S9 §6 policy span：PEP 判定面只暴露 policy 类型与判定结果——payload
        # （actor/资源上下文/decision reason）绝不进 span。start_span 不吞异常，
        # 与「authorize 永不抛」正交：_authorize 内部已把一切折叠为 deny。
        with start_span(
            SpanNames.POLICY, {"policy_type": _policy_type_of(policy_input)}
        ) as span:
            decision = await self._authorize(policy_input)
            span.set_attribute("decision", "allow" if decision.allow else "deny")
            return decision

    async def _authorize(self, policy_input: PolicyInput | Mapping[str, Any]) -> PolicyDecision:
        try:
            if isinstance(policy_input, PolicyInput):
                document = policy_input.model_dump(mode="json")
                return await self._client.evaluate(document)
            typed = PolicyInput.model_validate(policy_input)
        except ValidationError:
            return self.deny("policy_input_invalid")
        except Exception:
            return self.deny("enforcement_internal_error")
        try:
            return await self._client.evaluate(typed.model_dump(mode="json"))
        except Exception:
            return self.deny("enforcement_internal_error")

    def deny(self, reason: str) -> PolicyDecision:
        """PEP 构造本地拒绝（如健康检查降级路径），不带任何伪造的 OPA metadata。"""
        return self._client.fail_closed(reason)
