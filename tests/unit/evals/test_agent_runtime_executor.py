"""S2-T6 契约：runtime-contract 场景注册表与 invariant 分派（执行路径由
tests/integration/temporal + CLI Gate 覆盖——真实环境，不做单元级旁路）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.evals.domain import RegisteredUnit, SampleStatus
from zhiwei.evals.executors.agent_runtime import build_contract_registry
from zhiwei.evals.runtime_contracts import (
    RUNTIME_CONTRACT_SCENARIOS,
    RUNTIME_CONTRACT_UNITS,
    check_invariant,
    scenario_for_unit,
)
from zhiwei.runtime.events import (
    RunCompleted,
    RunCreated,
    RunStarted,
    TaskCompleted,
    TaskScheduled,
    TaskStarted,
)
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistryError
from zhiwei.runtime.reducer import reduce


def _ts(offset: int = 0) -> datetime:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    from datetime import timedelta

    return base + timedelta(seconds=offset)


class _DupHandler(TaskHandler):
    """独立实例源：验证重复注册被拒（不污染 build_contract_registry 的全新实例）。"""

    @property
    def primitive_type(self) -> str:
        return "Fixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput()


class TestScenarioRegistry:
    def test_every_unit_has_a_scenario_and_vice_versa(self) -> None:
        unit_keys = {(u.sample_id, u.unit_id) for u in RUNTIME_CONTRACT_UNITS}
        scenario_keys = {(s.unit.sample_id, s.unit.unit_id) for s in RUNTIME_CONTRACT_SCENARIOS}
        assert unit_keys == scenario_keys
        assert len(unit_keys) == 7

    def test_scenario_lookup_fails_closed_on_unknown_unit(self) -> None:
        with pytest.raises(LookupError, match="unknown runtime contract unit"):
            scenario_for_unit(RegisteredUnit(sample_id="nope", unit_id="nope"))

    def test_every_scenario_graph_is_a_valid_dag(self) -> None:
        for scenario in RUNTIME_CONTRACT_SCENARIOS:
            scenario.graph.validate_dag()

    def test_every_scenario_task_type_has_a_registered_handler(self) -> None:
        registry = build_contract_registry()
        for scenario in RUNTIME_CONTRACT_SCENARIOS:
            for node in scenario.graph.nodes.values():
                assert registry.has_handler(node.task_type), (
                    f"{scenario.unit.unit_id}: no handler for {node.task_type}"
                )

    def test_registry_rejects_duplicate_registration(self) -> None:
        registry = build_contract_registry()
        with pytest.raises(TaskHandlerRegistryError, match="already registered"):
            registry.register(_DupHandler())


class TestInvariantDispatch:
    def test_unknown_invariant_fails_closed(self) -> None:
        with pytest.raises(LookupError, match="unknown runtime invariant"):
            check_invariant("nope", reduce([]), [])

    def test_all_completed_invariant_passes_on_completed_state(self) -> None:
        graph = RUNTIME_CONTRACT_SCENARIOS[0].graph
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
        ]
        for task_id in sorted(graph.nodes):
            events.append(TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id=task_id))
            events.append(TaskStarted(run_id=run_id, timestamp=_ts(3), task_id=task_id,
                                      attempt_id=new_id()))
            events.append(TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id=task_id,
                                        output_values={}))
        events.append(RunCompleted(run_id=run_id, timestamp=_ts(5)))
        state = reduce(events)
        assert check_invariant("all_tasks_completed_run_completed", state, events) == []

    def test_all_completed_invariant_reports_violations(self) -> None:
        graph = RUNTIME_CONTRACT_SCENARIOS[0].graph
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
        ]
        state = reduce(events)
        errors = check_invariant("all_tasks_completed_run_completed", state, events)
        assert errors, "pending tasks must be reported as violations"
        assert any("!=" for e in errors for _ in [0])


class TestOutcomeMapping:
    def test_outcome_status_enum_covers_runtime_results(self) -> None:
        # executor 只产出终态 outcome；error/failed/completed 都在 S0 域内
        assert {s.value for s in SampleStatus} >= {"completed", "failed", "error"}


