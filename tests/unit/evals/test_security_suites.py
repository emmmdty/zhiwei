"""S9 R2-B RED: security-v1 suite 注册表与 executor 契约（specs/s9 §3 Security 层）。

事实源：specs/s9-eval-release-observability.md §3（层级 suite 必须包含 Security）、
ADR-011 §4（pre-send 分类门禁）、S2 effect_unknown / S3 model egress / S4 admission /
S5 ACL / S7 memory security 各阶段契约。

units 由代码定义：每个场景断言一条 fail-closed 安全性质（pass = 正确拒绝/围栏），
executor 绑定生产路径，不设评测专用旁路。负例对照（fixture 翻转为应放行的形态）
必须让该单位正确判 fail——判分器不是恒过。
"""

from __future__ import annotations

import pytest

from zhiwei.evals.domain import RegisteredUnit, SampleStatus
from zhiwei.evals.executors.security import SecurityGateExecutor
from zhiwei.evals.security_suites import (
    PRODUCTION_SECURITY_PATH,
    SECURITY_UNIT_CATEGORIES,
    SECURITY_V1,
    registered_security_units,
    resolve_security_suite,
)

REQUIRED_CATEGORIES = frozenset(
    {
        "memory_poisoning",
        "knowledge_acl_deny",
        "model_egress",
        "capability_admission",
        "effect_unknown",
        "service_account_memory",
    }
)


def test_suite_name_matches_gate_command() -> None:
    assert SECURITY_V1 == "security-v1"


def test_resolve_unknown_suite_fails_closed() -> None:
    with pytest.raises(LookupError, match="未知 security suite"):
        resolve_security_suite("security-v2")


def test_registered_units_cover_all_required_categories() -> None:
    assert REQUIRED_CATEGORIES <= SECURITY_UNIT_CATEGORIES


def test_unit_ids_are_unique_aligned_and_declare_security_property() -> None:
    suite = resolve_security_suite(SECURITY_V1)
    sample_ids = [definition.sample_id for definition in suite.definitions]
    assert 12 <= len(sample_ids) <= 20
    assert len(sample_ids) == len(set(sample_ids))
    for definition in suite.definitions:
        # 全部为 single 单位：independence unit 即 sample 本身。
        assert definition.unit_id == definition.sample_id
        # 每个单位声明它断言的 fail-closed 安全性质（判分语义的事实源）。
        assert definition.security_property.strip(), definition.sample_id


def test_registered_units_match_definitions() -> None:
    suite = resolve_security_suite(SECURITY_V1)
    units = registered_security_units()
    assert len(units) == len(suite.definitions)
    assert all(isinstance(unit, RegisteredUnit) for unit in units)
    assert {unit.sample_id for unit in units} == {
        definition.sample_id for definition in suite.definitions
    }


def test_production_path_is_declared() -> None:
    suite = resolve_security_suite(SECURITY_V1)
    assert suite.executor_kind == "security-gate"
    for seam in (
        "WriteMemoryCandidateHandler",
        "RetrieveHandler",
        "classification_gate",
        "ActionReceiptManager",
        "MemoryActivity",
    ):
        assert seam in PRODUCTION_SECURITY_PATH


async def _outcomes(
    overrides: dict[str, dict[str, object]] | None = None,
) -> dict[str, str]:
    suite = resolve_security_suite(SECURITY_V1)
    executor = SecurityGateExecutor(suite, fixture_overrides=overrides)
    return {
        unit.sample_id: await executor.execute(unit) for unit in suite.registered_units
    }


@pytest.mark.asyncio
async def test_every_unit_observes_security_pass() -> None:
    """生产安全路径按预期拒绝：每个 unit 都应观察到正确拒绝（verdict pass）。"""
    suite = resolve_security_suite(SECURITY_V1)
    executor = SecurityGateExecutor(suite)
    for unit in suite.registered_units:
        outcome = await executor.execute(unit)
        assert outcome.status is SampleStatus.COMPLETED, (
            unit.sample_id,
            outcome.result.get("failures"),
        )
        assert outcome.result["verdict"] == "pass"
        assert outcome.result["score"] == 1.0


@pytest.mark.asyncio
async def test_executor_is_deterministic() -> None:
    suite = resolve_security_suite(SECURITY_V1)
    first = SecurityGateExecutor(suite)
    second = SecurityGateExecutor(suite)
    for unit in suite.registered_units:
        left = await first.execute(unit)
        right = await second.execute(unit)
        assert left.result == right.result, unit.sample_id


@pytest.mark.asyncio
async def test_negative_control_flipped_fixture_fails_security_property() -> None:
    """负例对照：把 poisoning fixture 翻转为应放行的良性形态 → 该单位正确判 fail。

    判分器必须区分「正确拒绝」与「未触发拒绝」：恒过的判分器等于没有。
    """
    outcomes = await _outcomes(
        {
            "memory-poisoning/tool-instruction-refused": {
                "canonical_value": "editor theme dark"
            }
        }
    )
    flipped = outcomes["memory-poisoning/tool-instruction-refused"]
    assert flipped.status is SampleStatus.FAILED
    assert flipped.result["verdict"] == "fail"
    assert flipped.result["failures"], "翻转后必须留下失败记录"
    for sample_id, outcome in outcomes.items():
        if sample_id != "memory-poisoning/tool-instruction-refused":
            assert outcome.status is SampleStatus.COMPLETED, sample_id


@pytest.mark.asyncio
async def test_unknown_unit_fails_closed() -> None:
    suite = resolve_security_suite(SECURITY_V1)
    executor = SecurityGateExecutor(suite)
    outcome = await executor.execute(
        RegisteredUnit(sample_id="not-a-unit", unit_id="not-a-unit")
    )
    assert outcome.status is SampleStatus.FAILED
    assert "未注册" in outcome.result["error"]
