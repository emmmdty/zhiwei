"""S9-T1 RED: campaign 划分契约——精确覆盖、拒绝重叠与漏覆盖、完成只认全部子运行终态。

campaign 不是第二套 EvalRun：这里只冻结划分与状态推导的纯领域规则，子运行的创建
复用既有 EvalFoundationService（集成层验证）。
"""

from __future__ import annotations

from uuid import UUID

import pytest
from zhiwei.evals.campaigns import (
    CampaignPlan,
    CampaignStatus,
    derive_campaign_status,
    partition_units,
)

from zhiwei.evals.domain import RegisteredUnit
from zhiwei.evals.runs import RunPhase

SUITE_ID = UUID("22222222-2222-4222-8222-222222222222")


def _unit(index: int) -> RegisteredUnit:
    return RegisteredUnit(sample_id=f"sample-{index}", unit_id=f"unit-{index}")


def _units(count: int) -> tuple[RegisteredUnit, ...]:
    return tuple(_unit(index) for index in range(1, count + 1))


def _unit_key(unit: RegisteredUnit) -> tuple[str, str]:
    return (unit.sample_id, unit.unit_id)


def test_partition_covers_every_registered_unit_exactly_once() -> None:
    units = _units(4)
    chunks = partition_units(units, (3, 1))

    assert [len(chunk) for chunk in chunks] == [3, 1]
    flattened = [unit for chunk in chunks for unit in chunk]
    assert [_unit_key(unit) for unit in flattened] == [
        _unit_key(unit) for unit in sorted(units, key=_unit_key)
    ]
    assert len({_unit_key(unit) for unit in flattened}) == len(units)


def test_partition_is_deterministic_regardless_of_input_order() -> None:
    units = _units(4)
    shuffled = (units[2], units[0], units[3], units[1])
    assert partition_units(shuffled, (2, 2)) == partition_units(units, (2, 2))


def test_partition_refuses_duplicate_units_as_overlap() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        partition_units((_unit(1), _unit(1)), (2,))


def test_partition_refuses_uncovered_registered_units() -> None:
    with pytest.raises(ValueError, match="cover"):
        partition_units(_units(4), (1, 1))


def test_partition_refuses_sizes_that_exceed_the_registry() -> None:
    with pytest.raises(ValueError, match="exceed"):
        partition_units(_units(4), (3, 2))


def test_partition_refuses_non_positive_child_sizes() -> None:
    with pytest.raises(ValueError, match="positive"):
        partition_units(_units(4), (0, 4))
    with pytest.raises(ValueError, match="positive"):
        partition_units(_units(4), (2, -2))


def test_partition_refuses_an_empty_child_list() -> None:
    with pytest.raises(ValueError, match="child"):
        partition_units(_units(4), ())


def test_campaign_status_derives_from_child_run_phases() -> None:
    assert derive_campaign_status((RunPhase.RUNNING, RunPhase.RUNNING)) is CampaignStatus.RUNNING
    assert derive_campaign_status((RunPhase.PARTIAL, RunPhase.RUNNING)) is CampaignStatus.RUNNING
    assert derive_campaign_status((RunPhase.SEALED, RunPhase.RUNNING)) is CampaignStatus.PARTIAL
    assert derive_campaign_status((RunPhase.SEALED, RunPhase.PARTIAL)) is CampaignStatus.PARTIAL
    assert derive_campaign_status((RunPhase.SEALED, RunPhase.SEALED)) is CampaignStatus.COMPLETED


def test_campaign_without_sealed_children_is_never_complete() -> None:
    """fail closed：没有任何 sealed 子运行（含空 children）时不得推断完成。"""
    assert derive_campaign_status(()) is CampaignStatus.RUNNING


def test_campaign_plan_children_exactly_cover_the_frozen_registry() -> None:
    plan = CampaignPlan.partition(
        suite_id=SUITE_ID,
        suite_version=1,
        registered_units=_units(4),
        child_sizes=(2, 2),
    )

    assert plan.suite_id == SUITE_ID
    assert plan.suite_version == 1
    assert plan.registered_units == tuple(sorted(_units(4), key=_unit_key))
    assert [len(chunk) for chunk in plan.children] == [2, 2]
    covered = [unit for chunk in plan.children for unit in chunk]
    assert sorted(covered, key=_unit_key) == sorted(_units(4), key=_unit_key)


def test_campaign_plan_rejects_a_partition_that_is_not_exact() -> None:
    with pytest.raises(ValueError, match="exceed"):
        CampaignPlan.partition(
            suite_id=SUITE_ID,
            suite_version=1,
            registered_units=_units(4),
            child_sizes=(4, 1),
        )
