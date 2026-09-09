"""F-R2-09：TemporalWorkflowSender 把 delegation_chain 装入 AgentRunWorkflowInput。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from zhiwei.workers.temporal_sender import TemporalWorkflowSender


class _StubTemporalClient:
    def __init__(self) -> None:
        self.started: list[tuple[str, Any]] = []

    async def start_workflow(self, workflow: Any, input: Any, **kwargs: Any) -> None:
        self.started.append((workflow, input))


def _input(delegation_chain: list[str]) -> dict[str, Any]:
    return {
        "graph": {"nodes": {"t1": {"task_id": "t1", "task_type": "Plan",
                                   "required_capability": "cap"}},
                  "edges": {}},
        "task_queue": "q",
        "delegation_chain": delegation_chain,
    }


@pytest.mark.asyncio
class TestDelegationChainReachesWorkflowInput:
    async def test_chain_is_carried(self) -> None:
        client = _StubTemporalClient()
        sender = TemporalWorkflowSender(client)  # type: ignore[arg-type]
        await sender.start_workflow(
            workflow_id=f"run-{uuid4()}",
            workflow_type="agent-run",
            input=_input(["run-a", "run-b"]),
            organization_id=uuid4(),
            workspace_id=uuid4(),
        )
        _, workflow_input = client.started[0]
        assert workflow_input.delegation_chain == ("run-a", "run-b")

    async def test_missing_chain_defaults_empty(self) -> None:
        client = _StubTemporalClient()
        sender = TemporalWorkflowSender(client)  # type: ignore[arg-type]
        payload = _input(["run-a"])
        payload.pop("delegation_chain")
        await sender.start_workflow(
            workflow_id=f"run-{uuid4()}",
            workflow_type="agent-run",
            input=payload,
            organization_id=uuid4(),
            workspace_id=uuid4(),
        )
        _, workflow_input = client.started[0]
        assert workflow_input.delegation_chain == ()
