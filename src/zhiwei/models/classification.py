"""F-R6-08（T-P4.6b/B7）：context inventory → routing 分类的派生面。

约定：ContextItem.metadata["data_classification"] 携带条目声明分类
（knowledge 侧 SourceVersion.classification 为权威源，S11 egress 接线批
落到 stamping 点）；本模块是唯一派生实现——max 语义（ADR-011 §4 门禁
输入取 context 实际最高分类），未知声明值 fail closed（跳过未可判定声明
= 重建低报面）。
"""

from __future__ import annotations

from collections.abc import Sequence

from zhiwei.context.types import ContextItem
from zhiwei.models.contracts import ClassificationCeiling

DATA_CLASSIFICATION_METADATA_KEY = "data_classification"


def derive_context_classification(items: Sequence[ContextItem]) -> str | None:
    """context 条目声明分类的最高值；无任何声明返回 None（unknown 不可虚构）。

    Raises ValueError: 任一条目声明了 ClassificationCeiling 词表外的分类值
    （fail closed——不可静默跳过）。
    """
    declared: list[ClassificationCeiling] = []
    for item in items:
        raw = item.metadata.get(DATA_CLASSIFICATION_METADATA_KEY)
        if raw is None:
            continue
        try:
            declared.append(ClassificationCeiling(str(raw).lower()))
        except ValueError:
            raise ValueError(
                f"context item declares unknown data classification {raw!r}"
            ) from None
    if not declared:
        return None
    return max(declared).value
