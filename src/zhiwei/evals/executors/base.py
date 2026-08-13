"""Eval executor 端口：运行单元到终态结果的统一入口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from zhiwei.evals.domain import RegisteredUnit, SampleOutcome


@runtime_checkable
class EvalExecutor(Protocol):
    """执行一个注册单位并返回 terminal SampleOutcome。

    S0 只接入 empty/legacy executor；S2 把同一 port 绑定真实 Agent Runtime。
    """

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome: ...
