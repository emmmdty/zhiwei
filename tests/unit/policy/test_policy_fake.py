"""F-R3-05 RED：FakePolicyEnforcer 默认 deny 契约（fail closed 测试替身）。

finding 证据：tests/fixtures/policy_fake.py `allow: bool = True` 默认放行——新增
router 的集成测试若只配 fake 即默认放行，矩阵 deny 语义不会在 wiring 层被意外
发现。与生产 PEP 的 fail-closed 纪律同构：测试替身的默认也必须 deny，allow 须
显式声明；deny 路径产生 metadata 全 NULL 的本地拒绝决策（与生产本地 deny 同形）。

波及面（F-R3-05 GREEN 迁移）：既有依赖默认 allow 的构造点全部补显式
allow=True（分批 ≤5 文件），默认值翻转为 deny 后，未来新测试必须显式声明。
"""

import pytest
from fixtures.policy_fake import ALLOW_DECISION_ID, FakePolicyEnforcer

_SAMPLE_INPUT = {"resource": {"type": "org"}, "action": "create"}


class TestFakeEnforcerDefaultDeny:
    @pytest.mark.asyncio
    async def test_default_construction_denies(self) -> None:
        enforcer = FakePolicyEnforcer()
        decision = await enforcer.authorize(_SAMPLE_INPUT)
        assert decision.allow is False
        # 本地拒绝形态：metadata 双 NULL（与生产 enforcer.deny 同形）
        assert decision.decision_id is None
        assert decision.revision is None

    @pytest.mark.asyncio
    async def test_explicit_allow_is_honored(self) -> None:
        enforcer = FakePolicyEnforcer(allow=True)
        decision = await enforcer.authorize(_SAMPLE_INPUT)
        assert decision.allow is True
        # allow 决策携带非空 metadata（满足 v2 审计契约）
        assert decision.decision_id == ALLOW_DECISION_ID
        assert decision.revision is not None

    def test_deny_helper_ignores_allow_flag(self) -> None:
        enforcer = FakePolicyEnforcer(allow=True)
        decision = enforcer.deny("custom_reason")
        assert decision.allow is False
        assert decision.decision_id is None
        assert decision.revision is None
