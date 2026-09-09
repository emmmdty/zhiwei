"""F-R6-08（T-P4.6b/B7）：routing 分类派生——inventory 最高分类而非自报。

finding 证据：RoutingRequest.data_classification 默认 "public"、组装点不
从 context inventory 推导——「错误地声明为 public」畅通无阻，门禁强度取决于
最诚实的调用方。本批契约：

- 派生 helper：context item 的 data_classification 元数据（约定键）取
  max（ClassificationCeiling 全序）；无声明 → None（unknown 不可虚构）；
  声明未知分类值 → fail closed 拒绝（不可跳过未可判定声明）。
- 组装点（ModelActivity._build_routing_request）：缺省不再虚构 "public"——
  声明与派生并存取 max（under-declaration 被派生顶起、over-declaration
  保留）；二者皆无 → fail closed。
- 残留（登记）：RoutingRequest 直接构造面的缺省 "public" 保持不变（无
  生产 egress 调用方；全量 stamping 随 S11 egress 接线批）。
"""

from __future__ import annotations

import pytest

from zhiwei.context.types import ContextCategory, ContextItem
from zhiwei.models.classification import (
    DATA_CLASSIFICATION_METADATA_KEY,
    derive_context_classification,
)


def _item(classification: str | None) -> ContextItem:
    metadata = (
        {DATA_CLASSIFICATION_METADATA_KEY: classification}
        if classification is not None
        else {}
    )
    return ContextItem(
        category=ContextCategory.AUTHORITATIVE,
        content={"id": "item"},
        metadata=metadata,
    )


class TestDeriveContextClassification:
    def test_max_over_declared_items(self) -> None:
        items = (_item("public"), _item("confidential"), _item("internal"))
        assert derive_context_classification(items) == "confidential"

    def test_no_declaration_returns_none(self) -> None:
        assert derive_context_classification((_item(None), _item(None))) is None
        assert derive_context_classification(()) is None

    def test_unknown_declared_value_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="unknown data classification"):
            derive_context_classification((_item("top-secret-unknown-level"),))

    def test_value_normalized_to_enum_value(self) -> None:
        # 大写形态（knowledge Classification 词表）归一为 models 层小写值
        assert derive_context_classification((_item("RESTRICTED"),)) == "restricted"
