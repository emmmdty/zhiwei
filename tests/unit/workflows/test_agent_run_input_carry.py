"""F-R2-09：continue-as-new 携带状态保留 delegation_chain（跨 CAN 存活）。"""

from __future__ import annotations

from zhiwei.workflows.agent_run import AgentRunWorkflow, AgentRunWorkflowInput


def _workflow_with_runtime_state() -> AgentRunWorkflow:
    """绕过 @workflow.defn 运行时（单测只验 _carry_input 的字段透传）。"""
    wf = AgentRunWorkflow.__new__(AgentRunWorkflow)
    object.__setattr__(wf, "_completed", [])
    object.__setattr__(wf, "_failed", [])
    object.__setattr__(wf, "_skipped", [])
    object.__setattr__(wf, "_attempt_counts", {})
    object.__setattr__(wf, "_dispatched_total", 0)
    object.__setattr__(wf, "_cancel_requested", False)
    object.__setattr__(wf, "_cancel_reason", None)
    object.__setattr__(wf, "_paused", False)
    object.__setattr__(wf, "_pause_dirty", False)
    object.__setattr__(wf, "_resume_dirty", False)
    object.__setattr__(wf, "_seen_signal_ids", [])
    object.__setattr__(wf, "_approval_decisions", {})
    return wf


def _input(delegation_chain: tuple[str, ...]) -> AgentRunWorkflowInput:
    return AgentRunWorkflowInput(
        run_id="run-x",
        organization_id="org",
        workspace_id="ws",
        graph={"nodes": {}, "edges": {}},
        task_queue="q",
        delegation_chain=delegation_chain,
    )


class TestCarryPreservesDelegationChain:
    def test_chain_survives_continue_as_new(self) -> None:
        wf = _workflow_with_runtime_state()
        carried = wf._carry_input(_input(("run-a", "run-b")))
        assert carried.delegation_chain == ("run-a", "run-b")

    def test_empty_chain_stays_empty(self) -> None:
        wf = _workflow_with_runtime_state()
        carried = wf._carry_input(_input(()))
        assert carried.delegation_chain == ()
