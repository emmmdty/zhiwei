"""Scorer 输入/输出的冻结契约：可见面封闭，隐藏目标进不来。

ScorerInput 用 extra="forbid" 把输入面钉死为 unit/output/reference/context 四个
字段：数据集答案之外的目标（hidden_target）与生成器内部状态（generator_state）
一旦尝试进入评分输入即被拒绝——scorer 只能看到被评文本与参考答案，防泄漏即防自证。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from zhiwei.evals.domain import RegisteredUnit


class ScorerInput(BaseModel):
    """scorer 的全部可见输入；context 只放显式声明的辅助信息。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: RegisteredUnit
    output: dict[str, Any]
    reference: dict[str, Any]
    context: dict[str, Any] = {}


class ScorerVerdict(BaseModel):
    """scorer 的唯一输出通道：布尔判定 + 分数 + 可审计明细。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    score: float
    details: dict[str, Any]


@runtime_checkable
class Scorer(Protocol):
    """确定性 scorer 端口：同一输入必须产出同一 verdict（纯函数）。"""

    def score(self, payload: ScorerInput) -> ScorerVerdict: ...
