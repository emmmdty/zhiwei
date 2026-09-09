"""F-R6-08（T-P4.6b/B7）：routing 组装点分类派生——不虚构 "public"。

ModelActivity._build_routing_request 是 Run 内 routing request 的组装缝：
data_classification 缺省曾是 "public"（finding 证据行）。本批契约：
声明与派生并存取 max（派生顶起低报声明、过高声明保留）；二者皆无 →
fail closed 拒绝组装。
"""

from __future__ import annotations

import pytest

from zhiwei.context.types import ContextCategory, ContextItem
from zhiwei.models.classification import DATA_CLASSIFICATION_METADATA_KEY
from zhiwei.workflows.activities.model import ModelActivity, ModelActivityInput


def _candidate() -> dict:
    return {
        "id": "mp-ep-a",
        "endpoint_id": "ep-a",
        "model_name": "m1",
        "wire_protocol": "openai_chat",
        "api_path": "/chat/completions",
        "context_window": 1024,
    }


def _item(classification: str) -> ContextItem:
    return ContextItem(
        category=ContextCategory.AUTHORITATIVE,
        content={"id": "item"},
        metadata={DATA_CLASSIFICATION_METADATA_KEY: classification},
    )


def _input(**routing_overrides) -> ModelActivityInput:
    rr: dict = {
        "candidates": [_candidate()],
        "endpoints": {},
    }
    rr.update(routing_overrides)
    return ModelActivityInput(
        run_id="r", task_id="t", attempt_id="a", task_type="Model",
        routing_request=rr,
    )


class TestRoutingAssemblyDerivesClassification:
    def test_absent_declaration_derives_from_inventory(self) -> None:
        rr = _input(context_items=(_item("confidential"),))
        request = ModelActivity()._build_routing_request(rr)
        assert request.data_classification == "confidential"

    def test_derived_classification_wins_over_lower_declaration(self) -> None:
        rr = _input(
            data_classification="public",
            context_items=(_item("confidential"),),
        )
        request = ModelActivity()._build_routing_request(rr)
        assert request.data_classification == "confidential"

    def test_higher_declaration_preserved_over_derivation(self) -> None:
        rr = _input(
            data_classification="restricted",
            context_items=(_item("internal"),),
        )
        request = ModelActivity()._build_routing_request(rr)
        assert request.data_classification == "restricted"

    def test_no_declaration_and_no_derivation_fails_closed(self) -> None:
        rr = _input()
        with pytest.raises(ValueError, match="public"):
            ModelActivity()._build_routing_request(rr)

    def test_declared_only_unknown_value_fails_closed(self) -> None:
        """审查响应（F1）：declared-only 分支不得裸传——未知值到 router rank
        -1 fail-open 静默放行；必须过 ClassificationCeiling 校验。"""
        rr = _input(data_classification="banana")
        with pytest.raises(ValueError):
            ModelActivity()._build_routing_request(rr)

    def test_declared_only_uppercase_normalized(self) -> None:
        """declared-only 大写词表（knowledge 形态）归一为 models 层小写值。"""
        rr = _input(data_classification="RESTRICTED")
        request = ModelActivity()._build_routing_request(rr)
        assert request.data_classification == "restricted"
