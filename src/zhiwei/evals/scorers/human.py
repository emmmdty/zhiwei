"""Human/judge 评测协议：rubric/blinding/order/calibration/agreement 缺一即拒。

人评协议不完整时（空 rubric、无致盲、无校准锚点、无一致性度量）必须拒绝进入
评测流程——不完整的人评协议产出的分数既不可复现也不可审计。allowed_modes 把
human judge 限定在 inference/utility 场景（human/live/shadow），绝不进入内部
冻结确认口径（fixture/replay/offline）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from zhiwei.evals.domain import EvalMode


class HumanJudgeRefused(RuntimeError):
    """人评协议不完整或未被授权用于目标模式时拒绝。

    继承 RuntimeError 而非 ValueError：pydantic 会把 validator 里的 ValueError
    包装成 ValidationError，从而吞掉协议拒绝的具体语义；显式拒绝必须原样抛出。
    """


class HumanJudgeProtocol(BaseModel):
    """人评的预注册协议；字段在构造期校验，事后不可改写。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rubric: Mapping[str, str]
    blinding: str
    order: str
    calibration: Mapping[str, Any]
    agreement: Mapping[str, Any]

    @model_validator(mode="after")
    def _refuse_incomplete_protocol(self) -> HumanJudgeProtocol:
        if not self.rubric:
            raise HumanJudgeRefused("human judge protocol requires a non-empty rubric")
        if not self.blinding or self.blinding == "none":
            raise HumanJudgeRefused(
                "human judge protocol requires blinding; 'none' is not acceptable"
            )
        if not self.order:
            raise HumanJudgeRefused("human judge protocol requires a presentation order")
        if not self.calibration:
            raise HumanJudgeRefused(
                "human judge protocol requires calibration anchors"
            )
        if not self.agreement:
            raise HumanJudgeRefused(
                "human judge protocol requires an agreement metric"
            )
        return self

    @property
    def allowed_modes(self) -> frozenset[EvalMode]:
        """human judge 只服务 inference/utility，不进入内部冻结确认口径。"""
        return frozenset({EvalMode.HUMAN, EvalMode.LIVE, EvalMode.SHADOW})
