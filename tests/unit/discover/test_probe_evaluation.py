"""NegativeProbe 确定性求值契约（ADR-004，spec s8 §4.1）。

「模型只提出、不判定」：probe 求值一律由确定性组件完成——本模块是被测的唯一求值实现。
关键危险信号测试（spec §7）：注入必然可被推翻的假设，断言其确实被推翻——
refutation_rate 恒为 0 等于证伪机制没有在工作。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from zhiwei.contracts.identifiers import new_id
from zhiwei.discover.signals import FalsificationResult, NegativeProbe
from zhiwei.evidence.patterns.numeric import PatternFinding
from zhiwei.evidence.patterns.probes import (
    ProbeEvaluationError,
    ProbeMetricTable,
    evaluate_probe,
    generate_probes_for_finding,
    probe_evidence_tags,
)

_NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _probe(**overrides: Any) -> NegativeProbe:
    base = NegativeProbe(
        probe_id=new_id(),
        metric="dso_days",
        entity_scope="customer:C03",
        window_hours=36 * 720,
        comparator="lt",
        threshold=55.0,
        description="若账期突变假设为假，dso 应保持在基线附近",
    )
    if not overrides:
        return base
    return NegativeProbe(**{**base.model_dump(), **overrides})


def _table() -> ProbeMetricTable:
    table = ProbeMetricTable()
    table.put("customer:C03", "dso_days", [47.0] * 18 + [110.0] * 18)
    table.put("customer:C08", "dso_days", [47.0] * 36)
    return table


class TestProbeMetricTable:
    def test_window_mean_over_series(self) -> None:
        table = _table()
        # 36 个月窗口均值 = (47*18 + 110*18) / 36
        value = table.value("customer:C03", "dso_days", "2023-01", "2025-12")
        assert value == pytest.approx((47.0 * 18 + 110.0 * 18) / 36)

    def test_unknown_scope_is_none_not_error(self) -> None:
        assert _table().value("customer:ZZ", "dso_days", "2023-01", "2025-12") is None


class TestEvaluateProbe:
    def test_refuted_when_disproving_observation_holds(self) -> None:
        """probe 断言「若假设为假应观察到 X」；X 被观察到 → 假设被推翻（passed=False）。"""
        # C03 dso 实际均值 ~78.5，probe 说「若为假应 < 55」→ 观察不成立 → 假设存活
        result = evaluate_probe(_probe(), _table(), evaluated_at=_NOW)
        assert result.passed is True
        assert result.actual_value == pytest.approx(78.5)

    def test_survives_when_disproving_observation_absent(self) -> None:
        # C08 dso = 47 < 55 → 「若为假应 < 55」被观察到 → 推翻
        result = evaluate_probe(
            _probe(entity_scope="customer:C08"), _table(), evaluated_at=_NOW
        )
        assert result.passed is False
        assert result.actual_value == pytest.approx(47.0)

    def test_all_comparators_are_evaluated(self) -> None:
        table = _table()
        cases = [
            ("gt", 50.0, True),   # 78.5 > 50 成立
            ("gte", 78.5, True),
            ("lt", 100.0, True),  # 78.5 < 100 成立
            ("lte", 78.5, True),
            ("eq", 78.5, True),
            ("neq", 10.0, True),
            ("gt", 100.0, False),
            ("eq", 1.0, False),
        ]
        for comparator, threshold, observed in cases:
            result = evaluate_probe(
                _probe(comparator=comparator, threshold=threshold), table, evaluated_at=_NOW
            )
            assert result.passed is (not observed), (comparator, threshold)
            assert result.evaluation_method == "deterministic"

    def test_unknown_metric_fails_closed(self) -> None:
        """求值所需数据缺失 → 明确报错，不得默认通过（fail closed）。"""
        with pytest.raises(ProbeEvaluationError):
            evaluate_probe(
                _probe(entity_scope="customer:ZZ"), _table(), evaluated_at=_NOW
            )

    def test_unknown_comparator_fails_closed(self) -> None:
        with pytest.raises(ProbeEvaluationError):
            evaluate_probe(
                _probe(comparator="approx"), _table(), evaluated_at=_NOW
            )

    def test_same_input_byte_identical_result(self) -> None:
        """确定性：同输入逐字节同结果（spec 契约测试要求）。"""
        probe = _probe()
        import json

        first = evaluate_probe(probe, _table(), evaluated_at=_NOW)
        second = evaluate_probe(probe, _table(), evaluated_at=_NOW)
        assert json.dumps(first.model_dump(mode="json")) == json.dumps(second.model_dump(mode="json"))

    def test_result_is_frozen(self) -> None:
        result = evaluate_probe(_probe(), _table(), evaluated_at=_NOW)
        with pytest.raises(ValidationError):
            result.passed = True  # type: ignore[misc]


class TestRefutationDangerSignal:
    def test_necessarily_refutable_hypothesis_is_refuted(self) -> None:
        """spec §7 一等指标测试：注入必然可被推翻的假设，断言其确实被推翻。

        假设：「C08 的 dso 出现了基线突变」。若该假设为假（dso 仍在基线），
        应观察到 dso 均值 < 55 —— C08 数据恒为 47，观察成立 → 假设必须被推翻。
        求值必须真实走 ProbeMetricTable，不得硬编码 probe 结果。
        """
        refutable_probe = _probe(
            entity_scope="customer:C08",
            metric="dso_days",
            comparator="lt",
            threshold=55.0,
        )
        result = evaluate_probe(refutable_probe, _table(), evaluated_at=_NOW)
        assert result is not None
        assert isinstance(result, FalsificationResult)
        assert result.passed is False, "必然可推翻的假设必须被推翻——refutation 机制必须真实工作"
        assert result.actual_value == pytest.approx(47.0)

    def test_tracked_hypothesis_with_refutable_probe_cannot_admit(self) -> None:
        """被推翻的假设不得进入 triage 队列（准入门槛）。"""
        from zhiwei.discover.hypotheses import FalsificationTracker, RiskHypothesis

        hypothesis = RiskHypothesis(
            id=new_id(),
            signal_id=new_id(),
            program_version_id=new_id(),
            detector_pack_id=new_id(),
            detector_pack_version=1,
            title="C08 dso 基线突变",
            created_at=_NOW,
            updated_at=_NOW,
            proposed_probes=(
                _probe(entity_scope="customer:C08", comparator="lt", threshold=55.0),
            ),
        )
        tracker = FalsificationTracker(hypothesis, min_probes_required=1)
        result = evaluate_probe(
            _probe(entity_scope="customer:C08", comparator="lt", threshold=55.0),
            _table(),
            evaluated_at=_NOW,
        )
        tracker.record_result(result)
        can_admit, reason = tracker.admission_check()
        assert not can_admit
        assert "refuted" in reason


class TestProbeGeneration:
    def _finding(self) -> PatternFinding:
        return PatternFinding(
            kind="baseline_deviation",
            entity_dim="customer",
            entity_value="C03",
            metric="dso_days",
            window_start="2025-02",
            window_end="2025-12",
            snr=20.6,
            band="easy",
            direction="rise",
            change=66.0,
            sigma=3.2,
            units="days",
            rows_observed=11,
            detector_version="1",
            formula_id="P4",
        )

    def test_generates_typed_probes(self) -> None:
        table = _table()
        probes = generate_probes_for_finding(self._finding(), table)
        assert len(probes) >= 1
        for probe in probes:
            assert isinstance(probe, NegativeProbe)
            assert probe.entity_scope == "customer:C03"
            assert probe.comparator in {"lt", "gt", "lte", "gte", "eq", "neq"}

    def test_probe_generation_is_deterministic_set(self) -> None:
        """probe 集合（结构、顺序、断言）确定性；probe_id 由 finding 内容派生。"""
        import json

        first = generate_probes_for_finding(self._finding(), _table())
        second = generate_probes_for_finding(self._finding(), _table())
        assert json.dumps([p.model_dump(mode="json") for p in first]) == json.dumps(
            [p.model_dump(mode="json") for p in second]
        )

    def test_probes_evaluated_end_to_end(self) -> None:
        """生成→求值全链路：产出的每个 probe 都能被确定性求值。"""
        table = _table()
        for probe in generate_probes_for_finding(self._finding(), table):
            result = evaluate_probe(probe, table, evaluated_at=_NOW)
            assert result.passed in (True, False)
            assert result.actual_value is not None


class TestProbeEvidenceIndependence:
    def test_one_independent_tag_per_probe_result(self) -> None:
        """每个 probe 结果作为独立 EvidenceRef 附加，不可合并或省略（spec §4.1）。"""
        results = [
            evaluate_probe(_probe(comparator="gt", threshold=50.0), _table(), evaluated_at=_NOW),
            evaluate_probe(_probe(comparator="lt", threshold=100.0), _table(), evaluated_at=_NOW),
        ]
        tags = probe_evidence_tags(results, created_at=_NOW)
        assert len(tags) == 2, "两个 probe 结果不得合并为单个 evidence"
        ref_ids = {tag.tag_id for tag in tags}
        assert len(ref_ids) == 2
        descriptions = {tag.description for tag in tags}
        assert len(descriptions) == 2, "每个 evidence 必须可追溯到各自的 probe"

    def test_tag_descriptions_carry_probe_identity(self) -> None:
        probe = _probe(comparator="gt", threshold=50.0)
        result = evaluate_probe(probe, _table(), evaluated_at=_NOW)
        (tag,) = probe_evidence_tags([result], created_at=_NOW)
        assert str(probe.probe_id) in tag.description
        assert uuid4()  # import guard

    def test_falsified_flag_is_visible_in_tag(self) -> None:
        refuting = evaluate_probe(
            _probe(entity_scope="customer:C08", comparator="lt", threshold=55.0),
            _table(),
            evaluated_at=_NOW,
        )
        (tag,) = probe_evidence_tags([refuting], created_at=_NOW)
        assert "refuted" in tag.description.lower() or "refuted" in tag.source_ref
