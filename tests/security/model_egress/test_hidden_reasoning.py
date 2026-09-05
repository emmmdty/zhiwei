"""S3 Security: hidden reasoning 不持久化（spec s3 §5「正文不出现在 PG/manifest/log」）。

生产路径：canonical event（PG 侧持久化单元）→ reducer → CanonicalState 投影 →
Context Compiler → ContextIR（manifest / wire 侧）。断言 reasoning 正文沿该路径
不出现；`context.opaque.terminal` 之后的销毁行为由既有锁定测试覆盖
（tests/unit/context/test_reducer.py::TestHiddenReasoningSentinel）。
"""

from __future__ import annotations

import json

from zhiwei.context.compiler import ContextCompiler
from zhiwei.context.reducer import reduce_events

# 唯一哨兵：正文若沿任何持久化面泄漏，字符串扫描即可捕获。
SENTINEL = "HIDDEN-REASONING-BODY-do-not-persist-9f2c"


def _reasoning_event() -> dict:
    """模拟含 hidden reasoning 的模型响应进入 context 的 canonical 事件。"""
    return {
        "id": "e1",
        "sequence_no": 1,
        "event_type": "context.created",
        "event_digest": "sha256:" + "a" * 64,
        "payload": {"content": {"id": "o1", "hidden_reasoning": SENTINEL}},
    }


def _terminal_event() -> dict:
    return {
        "id": "e2",
        "sequence_no": 2,
        "event_type": "context.opaque.terminal",
        "event_digest": "sha256:" + "b" * 64,
        "payload": {},
    }


def test_reasoning_body_absent_from_canonical_state_projection() -> None:
    """state 投影不得保留 reasoning 正文——ContextItem 契约只允许 artifact ref / summary。"""
    state = reduce_events([_reasoning_event()])
    assert all(SENTINEL not in str(item.content) for item in state.items)


def test_reasoning_body_absent_from_compiled_ir_and_manifest() -> None:
    """编译产物（→ manifest / wire body）不得包含 reasoning 正文。"""
    state = reduce_events([_reasoning_event()])
    result = ContextCompiler(context_window=128_000).compile(state)
    for item in result.context_ir.items:
        assert SENTINEL not in str(item.content)
    assert SENTINEL not in json.dumps(result.manifest(), default=str)


def test_reasoning_body_destroyed_after_terminal() -> None:
    """当前实现已具备的能力：terminal 事件之后正文从投影中销毁（安全基线）。"""
    state = reduce_events([_reasoning_event(), _terminal_event()])
    assert all(SENTINEL not in str(item.content) for item in state.items)
