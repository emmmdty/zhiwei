"""S3 §5 hidden reasoning 销毁机制的实现级契约（随 GREEN 轮补齐，锁定 ref 语义）。

冻结安全契约（tests/security/model_egress/test_hidden_reasoning.py）断言「正文沿
投影/编译路径不可见」；本文件把「允许出现的形式」钉死：opaque ref 必须确定性
（同正文逐字节同 ref）、可幂等重放，且销毁覆盖 reducer 的全部内容入口。
"""

from __future__ import annotations

from zhiwei.context.opaque import (
    OPAQUE_REF_PREFIX,
    opaque_reasoning_ref,
    scrub_hidden_reasoning,
)
from zhiwei.context.reducer import reduce_events

_BODY_A = "chain-of-thought-alpha-3f1c"
_BODY_B = "chain-of-thought-beta-71ab"


def _event(seq: int, content: dict) -> dict:
    return {
        "id": f"e{seq}",
        "sequence_no": seq,
        "event_type": "context.created",
        "event_digest": "sha256:" + "0" * 63 + str(seq % 10),
        "payload": {"content": content},
    }


class TestOpaqueRefDeterminism:
    def test_same_body_yields_byte_identical_ref(self) -> None:
        assert opaque_reasoning_ref(_BODY_A) == opaque_reasoning_ref(_BODY_A)

    def test_different_body_yields_different_ref(self) -> None:
        assert opaque_reasoning_ref(_BODY_A) != opaque_reasoning_ref(_BODY_B)

    def test_ref_shape_is_opaque_digest_with_token_metadata(self) -> None:
        ref = opaque_reasoning_ref(_BODY_A)
        assert set(ref) == {"opaque_ref", "token_count"}
        assert ref["opaque_ref"].startswith(OPAQUE_REF_PREFIX + "sha256:")
        hex_part = ref["opaque_ref"].rsplit(":", 1)[1]
        assert len(hex_part) == 64 and all(c in "0123456789abcdef" for c in hex_part)
        assert ref["token_count"] >= 1

    def test_scrub_is_idempotent(self) -> None:
        content = {"id": "o1", "hidden_reasoning": _BODY_A}
        once = scrub_hidden_reasoning(content)
        assert scrub_hidden_reasoning(once) == once


class TestScrubCoverage:
    def test_body_never_reaches_input_structure(self) -> None:
        """scrub 不改写入参——正文留在调用方手里的入参不被意外固化。"""
        content = {"id": "o1", "hidden_reasoning": _BODY_A}
        original = dict(content)
        scrub_hidden_reasoning(content)
        assert content == original

    def test_unrelated_content_is_returned_unchanged(self) -> None:
        content = {"id": "a1", "kind": "objective", "name": "test"}
        assert scrub_hidden_reasoning(content) is content

    def test_identical_bodies_reduce_to_identical_refs(self) -> None:
        state = reduce_events([
            _event(1, {"id": "o1", "hidden_reasoning": _BODY_A}),
            _event(2, {"id": "o2", "hidden_reasoning": _BODY_A}),
        ])
        refs = [item.content["hidden_reasoning"]["opaque_ref"] for item in state.items]
        assert refs[0] == refs[1]

    def test_update_injecting_reasoning_is_scrubbed(self) -> None:
        events = [
            _event(1, {"id": "a1", "kind": "objective", "name": "n"}),
            {
                "id": "e2",
                "sequence_no": 2,
                "event_type": "context.updated",
                "event_digest": "sha256:" + "1" * 64,
                "payload": {"target_id": "a1", "updates": {"hidden_reasoning": _BODY_B}},
            },
        ]
        state = reduce_events(events)
        assert all(_BODY_B not in str(item.content) for item in state.items)
        assert any("hidden_reasoning" in item.content for item in state.items)

    def test_append_merge_entry_with_reasoning_is_scrubbed(self) -> None:
        events = [
            {
                "id": "e1",
                "sequence_no": 1,
                "event_type": "context.merge.append",
                "event_digest": "sha256:" + "2" * 64,
                "payload": {
                    "task_id": "t1",
                    "attempt_no": 1,
                    "entries": [{"id": "o1", "hidden_reasoning": _BODY_A}],
                },
            },
        ]
        state = reduce_events(events)
        assert len(state.opaque_items()) == 1
        assert _BODY_A not in str(state.items)

    def test_tuple_valued_payload_is_scrubbed_in_place(self) -> None:
        """runtime 事件 python-mode dump 可携带 tuple——形状保持且正文不漏。"""
        payload = {"values": ({"id": "o1", "hidden_reasoning": _BODY_A},)}
        scrubbed = scrub_hidden_reasoning(payload)
        assert isinstance(scrubbed["values"], tuple)
        assert _BODY_A not in str(scrubbed)
