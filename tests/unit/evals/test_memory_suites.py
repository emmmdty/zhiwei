"""S7-T7 RED: enterprise-memory-v1 suite 注册表。

事实源：specs/s7-memory.md §6（内部 suite 覆盖 write precision、retrieval、temporal
conflict、scope leakage、forget completeness、poisoning）、§4/ADR-009（队列收敛是
suite 内的负载型 unit）、ADR-013 决策 2。

units 由代码定义（suite 行为契约即代码），executor 绑定生产路径：
WriteMemoryCandidateHandler → Memory policy → candidate/confirm/conflict/revoke/forget
生产服务；不旁路到任何仓库/存储直调。
"""

from __future__ import annotations

import pytest

from zhiwei.evals.domain import RegisteredUnit
from zhiwei.evals.memory_suites import (
    ENTERPRISE_MEMORY_UNIT_CATEGORIES,
    ENTERPRISE_MEMORY_V1,
    PRODUCTION_MEMORY_PATH,
    registered_memory_units,
    resolve_memory_suite,
)

REQUIRED_CATEGORIES = frozenset(
    {
        "write_matrix",
        "retrieval",
        "temporal_conflict",
        "scope_leakage",
        "forget_completeness",
        "poisoning",
        # ADR-009 队列收敛是 S7 Gate 条件之一，作为 suite 内负载型 unit 存在。
        "queue_convergence",
    }
)


def test_suite_name_matches_gate_command() -> None:
    assert ENTERPRISE_MEMORY_V1 == "enterprise-memory-v1"


def test_resolve_unknown_suite_fails_closed() -> None:
    with pytest.raises(LookupError, match="未知 memory suite"):
        resolve_memory_suite("enterprise-memory-v2")


def test_registered_units_cover_all_required_categories() -> None:
    assert REQUIRED_CATEGORIES <= ENTERPRISE_MEMORY_UNIT_CATEGORIES


def test_unit_ids_are_unique_and_aligned() -> None:
    suite = resolve_memory_suite(ENTERPRISE_MEMORY_V1)
    sample_ids = [definition.sample_id for definition in suite.definitions]
    assert len(sample_ids) == len(set(sample_ids))
    for definition in suite.definitions:
        # 全部为 single 单位：independence unit 即 sample 本身。
        assert definition.unit_id == definition.sample_id


def test_registered_units_match_definitions() -> None:
    suite = resolve_memory_suite(ENTERPRISE_MEMORY_V1)
    units = registered_memory_units()
    assert len(units) == len(suite.definitions)
    assert all(isinstance(unit, RegisteredUnit) for unit in units)
    assert {unit.sample_id for unit in units} == {
        definition.sample_id for definition in suite.definitions
    }


def test_production_path_is_declared() -> None:
    suite = resolve_memory_suite(ENTERPRISE_MEMORY_V1)
    assert suite.executor_kind == "memory-lifecycle"
    assert "WriteMemoryCandidateHandler" in PRODUCTION_MEMORY_PATH


def test_queue_convergence_unit_declares_load_shape() -> None:
    suite = resolve_memory_suite(ENTERPRISE_MEMORY_V1)
    convergence = [
        definition
        for definition in suite.definitions
        if definition.category == "queue_convergence"
    ]
    assert len(convergence) == 1
