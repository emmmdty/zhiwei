"""S10-T6 RED：change-brief-v1 suite 注册表、pack runtime 与 executor 契约。

事实源：specs/s10-studio-third-app.md §4/§7、plan Task 6、ADR-013 决策 2。

通用性证明的契约面（code diff 只落在 pack / eval 资产层 / renderer 注册）：
- suite 从冻结语料 evals/change-brief/ 构造 registered units（fail closed：未知字段、
  unit 漂移、suite 名不符一律加载期拒绝）；
- executor 绑定生产路径（RunCommandService → AgentRunWorkflow → pack task graph），
  handler 注册表由 pack runtime（solution-packs/change-brief/runtime/）经公共
  handler-registry 机制构造——与 ask_contracts.build_ask_contract_registry 同型；
- 判分器是纯函数：对 reduced RunState 的 canonical brief 按冻结 expected 断言，
  unknown-symbol 场景必须产出诚实 unknowns，绝不编造快照之外的符号。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from zhiwei.agents.pack_files import load_pack_dir, validate_pack_bundle
from zhiwei.evals.change_brief_suites import (
    CHANGE_BRIEF_V1,
    EXECUTOR_KIND,
    PRODUCTION_CHANGE_BRIEF_PATH,
    ChangeBriefUnit,
    registered_change_brief_units,
    resolve_change_brief_suite,
)
from zhiwei.evals.domain import RegisteredUnit
from zhiwei.runtime.reducer import RunState


def _suite():
    return resolve_change_brief_suite(CHANGE_BRIEF_V1)


def _unit(unit_id: str) -> ChangeBriefUnit:
    suite = _suite()
    for unit in suite.units:
        if unit.unit_id == unit_id:
            return unit
    raise LookupError(unit_id)


def _state(canonical: dict, *, status: str = "completed") -> RunState:
    return RunState(run_id=uuid4(), status=status, canonical=canonical, conflicts=[], tasks={})


class TestChangeBriefSuiteRegistry:
    def test_suite_name_matches_gate_command(self) -> None:
        assert CHANGE_BRIEF_V1 == "change-brief-v1"

    def test_resolve_unknown_suite_fails_closed(self) -> None:
        with pytest.raises(LookupError, match="未知 change-brief suite"):
            resolve_change_brief_suite("change-brief-v2")

    def test_six_units_registered_unique_and_aligned(self) -> None:
        units = registered_change_brief_units()
        assert len(units) == 6
        keys = {(u.sample_id, u.unit_id) for u in units}
        assert len(keys) == 6
        # 全部为 single 单位：independence unit 即 sample 本身。
        assert all(u.unit_id == u.sample_id for u in units)

    def test_units_match_frozen_corpus_files(self) -> None:
        suite = _suite()
        assert {u.unit_id for u in suite.units} == {
            "github-commit",
            "pull-request",
            "mixed-refs",
            "no-impact",
            "risky-change",
            "unknown-symbol",
        }
        assert suite.corpus_digest.startswith("sha256:")
        for unit in suite.units:
            assert isinstance(unit, ChangeBriefUnit)
            assert unit.trigger.repository
            assert unit.trigger.commit_or_pr.kind in {"commit", "pull_request"}
            assert len(unit.files_changed) > 0

    def test_corpus_digest_is_content_addressed(self) -> None:
        from zhiwei.contracts.canonical import digest_bytes

        suite = _suite()
        joined = b"".join(
            (suite.corpus_path / f"{unit.unit_id}.yaml").read_bytes()
            for unit in sorted(suite.units, key=lambda u: u.unit_id)
        )
        assert suite.corpus_digest == digest_bytes(joined)

    def test_executor_kind_and_production_path_declared(self) -> None:
        suite = _suite()
        assert suite.executor_kind == "change-brief-pack"
        assert EXECUTOR_KIND == "change-brief-pack"
        for seam in (
            "RunCommandService",
            "AgentRunWorkflow",
            "Retrieve",
            "Analyze",
            "VerifyHandler",
            "Synthesize",
            "EmitArtifact",
        ):
            assert seam in PRODUCTION_CHANGE_BRIEF_PATH

    def test_pack_bundle_conformance_clean(self) -> None:
        suite = _suite()
        bundle = load_pack_dir(suite.pack_dir)
        assert validate_pack_bundle(bundle, suite.pack_dir) == ()


class TestPackRuntimeWiring:
    def test_pack_runtime_modules_expose_declared_surface(self) -> None:
        from zhiwei.evals.executors.change_brief import load_pack_runtime

        runtime = load_pack_runtime(_suite().pack_dir)
        assert callable(runtime["impact_analysis"].analyze_impact)
        assert callable(runtime["planner"].plan_retrieval)
        assert callable(runtime["synthesis"].synthesize_brief)

    def test_skill_entry_derives_impact_from_snapshot(self) -> None:
        """skill entry 契约：analyze_impact(repository, commit_or_pr, candidates)。"""
        from zhiwei.evals.executors.change_brief import load_pack_runtime

        runtime = load_pack_runtime(_suite().pack_dir)
        unit = _unit("github-commit")
        candidates = runtime["planner"].plan_retrieval(unit.model_dump(mode="json"))
        impact = runtime["impact_analysis"].analyze_impact(
            unit.trigger.repository,
            unit.trigger.commit_or_pr.model_dump(),
            candidates,
        )
        names = {symbol["name"] for symbol in impact["affected_symbols"]}
        assert names == {"KnowledgePlanner", "KnowledgeQuery", "plan_retrieval"}
        assert impact["unknowns"] == []
        assert {ref["file_path"] for ref in impact["code_refs"]} == {
            "src/zhiwei/knowledge/planner.py",
            "src/zhiwei/knowledge/query.py",
        }

    def test_unknown_changed_symbol_becomes_explicit_unknown(self) -> None:
        from zhiwei.evals.executors.change_brief import load_pack_runtime

        runtime = load_pack_runtime(_suite().pack_dir)
        unit = _unit("unknown-symbol")
        candidates = runtime["planner"].plan_retrieval(unit.model_dump(mode="json"))
        impact = runtime["impact_analysis"].analyze_impact(
            unit.trigger.repository,
            unit.trigger.commit_or_pr.model_dump(),
            candidates,
        )
        names = {symbol["name"] for symbol in impact["affected_symbols"]}
        assert "compute_age" not in names, "快照之外的符号绝不允许编造进影响面"
        assert any("compute_age" in u for u in impact["unknowns"])

    def test_registry_covers_pack_primitives(self) -> None:
        from zhiwei.agents.pack_files import load_pack_dir
        from zhiwei.evals.executors.change_brief import build_change_brief_registry

        suite = _suite()
        bundle = load_pack_dir(suite.pack_dir)
        assert bundle.task_graph is not None
        registry = build_change_brief_registry(suite)
        registry.validate_completeness({task.type for task in bundle.task_graph.tasks})

    def test_registry_fails_closed_on_unknown_primitive(self) -> None:
        """通用性负例：注册表对 pack 之外的 primitive 必须 fail closed。"""
        from zhiwei.evals.executors.change_brief import build_change_brief_registry
        from zhiwei.runtime.handlers.registry import TaskHandlerRegistryError

        registry = build_change_brief_registry(_suite())
        with pytest.raises(TaskHandlerRegistryError):
            registry.validate_completeness({"Teleport"})

    def test_unit_graphs_mirror_pack_topology_and_are_dags(self) -> None:
        from zhiwei.agents.pack_files import load_pack_dir
        from zhiwei.evals.executors.change_brief import build_change_brief_graph

        suite = _suite()
        bundle = load_pack_dir(suite.pack_dir)
        assert bundle.task_graph is not None
        pack_suffixes = {task.id for task in bundle.task_graph.tasks}
        pack_types = {task.type for task in bundle.task_graph.tasks}
        for unit in suite.units:
            graph = build_change_brief_graph(unit.unit_id, bundle)
            graph.validate_dag()
            suffixes = {tid.split("/", 1)[1] for tid in graph.nodes}
            assert suffixes == pack_suffixes
            assert {node.task_type for node in graph.nodes.values()} == pack_types


class TestChangeBriefScorer:
    def test_conforming_brief_passes_for_every_unit(self) -> None:
        """判分语义：生产派生的 brief 必须通过每个冻结 fixture 的 expected 断言。"""
        from zhiwei.evals.executors.change_brief import (
            load_pack_runtime,
            score_change_brief,
        )

        runtime = load_pack_runtime(_suite().pack_dir)
        for unit in _suite().units:
            candidates = runtime["planner"].plan_retrieval(unit.model_dump(mode="json"))
            impact = runtime["impact_analysis"].analyze_impact(
                unit.trigger.repository,
                unit.trigger.commit_or_pr.model_dump(),
                candidates,
            )
            brief = runtime["synthesis"].synthesize_brief(
                impact,
                {"verification_ok": True, "exit_code": 0, "check_count": 3},
            )
            state = _state(
                {
                    "brief": brief,
                    "verification_result": {"verification_ok": True, "exit_code": 0},
                    "artifact_id": f"artifact:{unit.unit_id}/emit_brief",
                    "artifact_kind": "verified-brief",
                }
            )
            _, failures = score_change_brief(unit.expected, state)
            assert failures == [], (unit.unit_id, failures)

    def test_scorer_rejects_fabricated_affected_symbols(self) -> None:
        from zhiwei.evals.executors.change_brief import (
            load_pack_runtime,
            score_change_brief,
        )

        runtime = load_pack_runtime(_suite().pack_dir)
        unit = _unit("unknown-symbol")
        candidates = runtime["planner"].plan_retrieval(unit.model_dump(mode="json"))
        impact = runtime["impact_analysis"].analyze_impact(
            unit.trigger.repository,
            unit.trigger.commit_or_pr.model_dump(),
            candidates,
        )
        fabricated = dict(impact)
        fabricated["affected_symbols"] = [
            *impact["affected_symbols"],
            {"name": "compute_age", "kind": "function",
             "file_path": "src/zhiwei/knowledge/freshness.py",
             "line_start": 62, "line_end": 90},
        ]
        brief = runtime["synthesis"].synthesize_brief(
            fabricated, {"verification_ok": True, "exit_code": 0, "check_count": 3}
        )
        _, failures = score_change_brief(unit.expected, _state({"brief": brief}))
        assert any("fabricated" in f or "compute_age" in f for f in failures)

    def test_scorer_rejects_silent_empty_unknowns(self) -> None:
        """unknown-symbol 场景静默丢掉 unknowns = 不诚实 brief，判 0 分。"""
        from zhiwei.evals.executors.change_brief import (
            load_pack_runtime,
            score_change_brief,
        )

        runtime = load_pack_runtime(_suite().pack_dir)
        unit = _unit("unknown-symbol")
        candidates = runtime["planner"].plan_retrieval(unit.model_dump(mode="json"))
        impact = runtime["impact_analysis"].analyze_impact(
            unit.trigger.repository,
            unit.trigger.commit_or_pr.model_dump(),
            candidates,
        )
        silent = dict(impact)
        silent["unknowns"] = []
        brief = runtime["synthesis"].synthesize_brief(
            silent, {"verification_ok": True, "exit_code": 0, "check_count": 3}
        )
        _, failures = score_change_brief(unit.expected, _state({"brief": brief}))
        assert failures, "静默空 unknowns 必须判失败"

    def test_scorer_requires_completed_run_and_verification(self) -> None:
        from zhiwei.evals.executors.change_brief import (
            load_pack_runtime,
            score_change_brief,
        )

        runtime = load_pack_runtime(_suite().pack_dir)
        unit = _unit("github-commit")
        candidates = runtime["planner"].plan_retrieval(unit.model_dump(mode="json"))
        impact = runtime["impact_analysis"].analyze_impact(
            unit.trigger.repository,
            unit.trigger.commit_or_pr.model_dump(),
            candidates,
        )
        brief = runtime["synthesis"].synthesize_brief(
            impact, {"verification_ok": True, "exit_code": 0, "check_count": 3}
        )
        _, failures = score_change_brief(unit.expected, _state({"brief": brief}, status="failed"))
        assert failures

        unverified = _state(
            {
                "brief": brief,
                "verification_result": {"verification_ok": False, "exit_code": 5},
                "artifact_id": "artifact:x",
            }
        )
        _, failures = score_change_brief(unit.expected, unverified)
        assert any("verification" in f for f in failures)


class TestCorpusSchemaFailClosed:
    def test_unknown_fixture_field_rejected(self) -> None:
        from zhiwei.evals.change_brief_suites import TriggerPayload

        with pytest.raises(ValueError):
            TriggerPayload.model_validate(
                {
                    "repository": "zhiwei-core",
                    "commit_or_pr": {"kind": "commit", "ref": "abc"},
                    "experimental_flag": True,
                }
            )

    def test_unknown_unit_fails_closed(self) -> None:
        """executor 的单位解析 fail closed：未注册的 (sample_id, unit_id) 拒绝。"""
        from zhiwei.evals.executors.change_brief import resolve_unit

        suite = _suite()
        with pytest.raises(LookupError):
            resolve_unit(suite, RegisteredUnit(sample_id="no-such-unit", unit_id="no-such-unit"))
        registered = RegisteredUnit(sample_id="github-commit", unit_id="github-commit")
        assert resolve_unit(suite, registered).unit_id == "github-commit"
