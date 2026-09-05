"""S7 spec 契约：memory 生命周期走 PG（同事务审计/event）——已登记的实现缺口。

事实源：specs/s7-memory.md §2（模块清单）、§7（「Runtime WriteMemoryCandidate
handler：typed task→Memory Activity/policy→candidate/refusal canonical event」）、
S7 plan Task 1/2（`src/zhiwei/memory/repositories.py`、`migrations/versions/0007_memory.py`、
「policy/repository I/O executes in Memory Activity and commits candidate/refusal as
canonical event」）。

当前状态（2026-09-04，S7 Gate 补全轮）：memory 域只有内存态生产服务（candidates/
confirmation/conflicts/forget），**没有** PG 持久化层——persistence/models.py 无
memory 表、migrations 无 memory migration、Memory Activity 无 session/uow 注入点。
按执行纪律「若某行为无生产实现，写 spec 契约的失败测试列为实现缺口，不许造假」，
本文件把 PG 生命周期契约固化为失败测试，等待 repositories/migration/Activity 接线
实现后转绿。在转绿之前：

- S7 Gate 的 `pytest tests/integration/memory` 含本文件的失败项，属已知缺口；
- 不得以内存态服务冒充「走 PG 的生命周期」（那会构成第二套契约）。
"""

from __future__ import annotations

import importlib

import pytest


def _import(module: str) -> object:
    """运行时探测缺失的生产模块（静态 import 会让 pyright 在缺口登记处报错）。"""
    return importlib.import_module(module)


class TestMemoryPgLifecycleContract:
    def test_memory_repository_persists_lifecycle_transitions(self) -> None:
        try:
            _import("zhiwei.memory.repositories")
        except ModuleNotFoundError as exc:
            pytest.fail(
                "实现缺口（specs/s7 §2 / plan Task 1）：memory 无 PG 持久化层 —— "
                f"src/zhiwei/memory/repositories.py 与 memory migration 未实现: {exc}"
            )

    def test_memory_transition_commits_same_transaction_event_and_audit(self) -> None:
        try:
            _import("zhiwei.memory.events")
        except ModuleNotFoundError as exc:
            pytest.fail(
                "实现缺口（specs/s7 §7 / plan Task 2）：memory 生命周期转移没有生产 "
                "canonical event / audit 落账路径（Memory Activity 无 uow 注入点）: "
                f"{exc}"
            )
