"""确定性空 executor：无网络、无子进程，恒返回 FAILED。"""

from __future__ import annotations

from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus


class EmptyExecutor:
    """把任何注册单位判为 FAILED 的 plumbing executor。

    用于 S0 的管线冒烟：证明「executor → record → seal」整条链路真实贯通，同时不伪造任何 Agent
    对话、不发起任何网络请求。结果 digest 只由 `{"reason": "empty_executor"}` 复算。
    """

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        return SampleOutcome(
            unit=unit,
            status=SampleStatus.FAILED,
            result={"reason": "empty_executor"},
        )
