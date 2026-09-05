"""S6 ask-v1 suite 契约：代码定义的 Ask 行为场景 → 生产 Runtime task graph。

事实源：specs/s6-evidence-ask.md §4/§6（cross-source task、clarification、conflict、
unanswerable、Fact vs Inference、partial/abstain）、solution-packs/ask/task_graph.yaml。

契约面：
- 六个行为单位唯一注册，scenario graph 是合法 DAG 且拓扑对齐 Ask SolutionPack；
- fixture handler 注册表覆盖全部场景 task_type（零模型调用）；
- 每个单位绑定一个从 canonical projection 断言的 invariant；
- Fact 无有效 Evidence 的 wire bundle 必须被生产 VerifyHandler 判不可验证；
- 未知 invariant / 未知单位 fail closed。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from zhiwei.evals.ask_contracts import (
    ASK_V1_SUITE,
    ASK_V1_UNITS,
    AskScenario,
    build_ask_contract_registry,
    check_invariant,
    invalid_fact_bundle_dict,
    scenario_bundle_dict,
    scenario_for_unit,
)
from zhiwei.evals.domain import RegisteredUnit
from zhiwei.runtime.reducer import ConflictRecord, RunState, TaskState


def _state(
    *,
    status: str = "completed",
    canonical: dict | None = None,
    conflicts: list[ConflictRecord] | None = None,
    tasks: dict[str, TaskState] | None = None,
) -> RunState:
    return RunState(
        run_id=uuid4(),
        status=status,
        canonical=canonical or {},
        conflicts=conflicts or [],
        tasks=tasks or {},
    )


class TestAskSuiteRegistry:
    def test_suite_name(self) -> None:
        assert ASK_V1_SUITE == "ask-v1"

    def test_six_behavior_units_registered(self) -> None:
        assert len(ASK_V1_UNITS) == 6
        keys = {(u.sample_id, u.unit_id) for u in ASK_V1_UNITS}
        assert len(keys) == 6

    def test_every_unit_resolves_to_scenario(self) -> None:
        for unit in ASK_V1_UNITS:
            scenario = scenario_for_unit(unit)
            assert isinstance(scenario, AskScenario)
            assert scenario.unit == unit

    def test_unknown_unit_fails_closed(self) -> None:
        with pytest.raises(LookupError):
            scenario_for_unit(RegisteredUnit(sample_id="ask/graph", unit_id="nope"))

    def test_scenario_graphs_are_valid_dags(self) -> None:
        for unit in ASK_V1_UNITS:
            scenario = scenario_for_unit(unit)
            scenario.graph.validate_dag()

    def test_registry_covers_all_scenario_task_types(self) -> None:
        registry = build_ask_contract_registry()
        for unit in ASK_V1_UNITS:
            scenario = scenario_for_unit(unit)
            registry.validate_completeness(
                {node.task_type for node in scenario.graph.nodes.values()}
            )

    def test_scenario_topology_mirrors_ask_pack(self) -> None:
        """场景图必须保持 Ask SolutionPack 的 intake→plan→retrieve→analyze→verify→
        synthesize→emit→finish 拓扑（节点 id 带场景前缀以隔离行为）。"""
        scenario = scenario_for_unit(ASK_V1_UNITS[0])
        suffixes = {tid.split("/", 1)[1] for tid in scenario.graph.nodes}
        assert {
            "intake",
            "plan",
            "retrieve_docs",
            "retrieve_code",
            "retrieve_db",
            "analyze_findings",
            "verify_evidence",
            "synthesize_answer",
            "emit_answer",
            "finish",
        } <= suffixes


class TestAskInvariants:
    def test_cross_source_requires_all_three_source_kinds(self) -> None:
        good = _state(
            canonical={
                "findings": [
                    {"source": "documents", "text": "d"},
                    {"source": "code", "text": "c"},
                    {"source": "db", "text": "b"},
                ],
                "answer": {"status": "completed"},
            }
        )
        assert check_invariant("cross_source_findings_present", good, []) == []
        bad = _state(
            canonical={
                "findings": [{"source": "documents", "text": "d"}],
                "answer": {"status": "completed"},
            }
        )
        assert check_invariant("cross_source_findings_present", bad, []) != []

    def test_unanswerable_abstains_without_claims(self) -> None:
        good = _state(
            canonical={
                "answer": {"status": "abstained", "claims": []},
                "unknowns": ["数据中不存在该条目"],
            }
        )
        assert check_invariant("unanswerable_abstains", good, []) == []
        bad = _state(canonical={"answer": {"status": "completed", "claims": [1]}})
        assert check_invariant("unanswerable_abstains", bad, []) != []

    def test_conflict_preserved_not_arbitrated(self) -> None:
        conflict = ConflictRecord(
            field="finding_value",
            values={"a/retrieve_docs": 4, "a/retrieve_code": 6},
        )
        good = _state(
            conflicts=[conflict],
            canonical={"synthesize_downgraded": True, "unresolved_conflict_fields": ["finding_value"]},
        )
        assert check_invariant("conflict_preserved_not_arbitrated", good, []) == []
        arbitrated = _state(
            conflicts=[],
            canonical={"synthesize_downgraded": False},
        )
        assert check_invariant("conflict_preserved_not_arbitrated", arbitrated, []) != []

    def test_unverified_fact_blocks_final(self) -> None:
        good = _state(
            canonical={
                "verification": {"verification_ok": False, "exit_code": 5},
                "answer": {"status": "blocked_unverified"},
            }
        )
        assert check_invariant("unverified_fact_blocks_final", good, []) == []
        finalized = _state(
            canonical={
                "verification": {"verification_ok": False, "exit_code": 5},
                "answer": {"status": "completed"},
            }
        )
        assert check_invariant("unverified_fact_blocks_final", finalized, []) != []

    def test_insufficient_sources_require_clarification(self) -> None:
        good = _state(
            canonical={
                "answer": {"status": "needs_clarification"},
                "clarification": {"questions": ["需要哪个时间范围的口径？"]},
            }
        )
        assert check_invariant("insufficient_sources_require_clarification", good, []) == []
        bad = _state(
            canonical={"answer": {"status": "completed"}, "clarification": {"questions": []}}
        )
        assert (
            check_invariant("insufficient_sources_require_clarification", bad, []) != []
        )

    def test_partial_reports_unknowns(self) -> None:
        good = _state(
            canonical={
                "answer": {"status": "partial"},
                "findings": [{"source": "documents", "text": "d"}],
                "unknowns": ["db 源不可用"],
            }
        )
        assert check_invariant("partial_reports_unknowns", good, []) == []
        silently_complete = _state(
            canonical={
                "answer": {"status": "completed"},
                "findings": [{"source": "documents", "text": "d"}],
                "unknowns": ["db 源不可用"],
            }
        )
        assert check_invariant("partial_reports_unknowns", silently_complete, []) != []

    def test_unknown_invariant_fails_closed(self) -> None:
        with pytest.raises(LookupError):
            check_invariant("made_up_invariant", _state(), [])


class TestAskFixtureHandlers:
    def test_invalid_fact_bundle_is_wire_level_claim_violation(self) -> None:
        """wire 层可下发的 dict：Fact claim 绑定 reference_only ref——
        在 claim 层违规（生产 VerifyHandler 必须判不可验证）。"""
        from zhiwei.evidence.bundles import EvidenceBundle

        with pytest.raises(Exception, match="reference_only"):
            EvidenceBundle.model_validate(invalid_fact_bundle_dict())

    def test_scenario_bundle_is_wire_valid(self) -> None:
        from zhiwei.evidence.bundles import EvidenceBundle

        bundle = EvidenceBundle.model_validate(scenario_bundle_dict("cross-source"))
        assert bundle.schema_version == 1

    def _run_handler(self, task_type: str, task_id: str) -> dict:  # type: ignore[type-arg]
        from zhiwei.runtime.handlers.base import TaskInput

        registry = build_ask_contract_registry()
        output = registry.get(task_type).execute(
            TaskInput(task_id=task_id, attempt_id=uuid4(), input_values={})
        )
        return output.output_values

    def test_analyze_and_synthesize_behaviors_are_task_scoped(self) -> None:
        abstained = self._run_handler("Synthesize", "unanswerable-abstain/synthesize_answer")
        assert abstained["answer"]["status"] == "abstained"
        clarified = self._run_handler("Synthesize", "needs-clarification/synthesize_answer")
        assert clarified["answer"]["status"] == "needs_clarification"

    def test_verify_handler_rejects_invalid_fact_bundle(self) -> None:
        """Ask verify 节点复用生产 VerifyHandler：wire 级 claim 违规 → 不可验证。"""
        from zhiwei.runtime.handlers.base import TaskInput
        from zhiwei.runtime.handlers.verify import VerifyHandler

        output = VerifyHandler().execute(
            TaskInput(
                task_id="fact-without-evidence/verify_evidence",
                attempt_id=uuid4(),
                input_values={"bundle": invalid_fact_bundle_dict()},
            )
        )
        assert output.output_values["verification_ok"] is False

    def test_retrieve_handlers_emit_source_scoped_findings(self) -> None:
        expected = {
            "AskRetrieveDocs": "documents",
            "AskRetrieveCode": "code",
            "AskRetrieveDB": "db",
        }
        for task_type, source in expected.items():
            values = self._run_handler(task_type, "cross-source/retrieve_docs")
            assert values["findings"], task_type
            assert values["findings"][0]["source"] == source
