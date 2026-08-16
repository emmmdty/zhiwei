"""测试专用 policy enforcer fake（只存在于 tests/，禁止进入 src/）。

policy_enforcer 在二轮修复后是 router 组合期必需依赖；既有 router 组合测试
（test_memberships / test_idor）经本 fake 接线。fake 不实现任何授权语义：
authorize 恒 allow（携带非空 decision_id/revision，满足 v2 审计 metadata 契约），
deny 产生 fail-closed 决策（metadata 全 NULL）。

fake 继承真实 PolicyEnforcer（router 参数类型即 PolicyEnforcer，组合期类型检查
不被放宽）；超类构造只挂一个 inert OPAClient，authorize/deny 全部被覆盖，
client 永不触网（RED 修订登记：鸭子类型无法通过 pyright 对必需参数的类型检查）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from zhiwei.policy.client import OPAClient, PolicyDecision
from zhiwei.policy.enforcement import PolicyEnforcer

ALLOW_DECISION_ID = "test-fake-decision-allow"
ALLOW_REVISION = "test-fake-rev-1"
ALLOW_REASON = "allow:test_fake"

# 惰性共享实例：fake 不持有任何真实 endpoint（client 只做类型占位，永不使用）
_INERT_CLIENT_URL = "http://127.0.0.1:1/fake-policy-client-never-used"


class FakePolicyEnforcer(PolicyEnforcer):
    """authorize 恒 allow 的测试 fake；deny 生成本地 fail-closed 决策。"""

    def __init__(self, *, allow: bool = True) -> None:
        super().__init__(OPAClient(_INERT_CLIENT_URL))
        self._allow = allow
        self.inputs: list[Any] = []

    async def authorize(self, policy_input: Any) -> PolicyDecision:
        self.inputs.append(policy_input)
        if not self._allow:
            return PolicyDecision(
                allow=False, decision_id=None, revision=None,
                reason="deny:test_fake", evaluated_at=datetime.now(UTC),
                input_digest=None,
            )
        return PolicyDecision(
            allow=True,
            decision_id=ALLOW_DECISION_ID,
            revision=ALLOW_REVISION,
            reason=ALLOW_REASON,
            evaluated_at=datetime.now(UTC),
            input_digest=None,
        )

    def deny(self, reason: str) -> PolicyDecision:
        return PolicyDecision(
            allow=False, decision_id=None, revision=None, reason=reason,
            evaluated_at=datetime.now(UTC), input_digest=None,
        )
