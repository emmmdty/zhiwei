"""S2-T4 RED: Command models — typed commands for workflow actions."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.commands import (
    CancelRun,
    CommandKind,
    PauseRun,
    ResumeRun,
    SignalRun,
    StartRun,
)


class TestCommandBase:
    def test_command_base_is_frozen(self) -> None:
        run_id = new_id()
        cmd = StartRun(run_id=run_id, task_queue="q")
        with pytest.raises(ValidationError):
            cmd.run_id = new_id()  # type: ignore[misc]

    def test_command_base_has_kind(self) -> None:
        cmd = StartRun(run_id=new_id(), task_queue="q")
        assert cmd.kind == CommandKind.START_RUN

    def test_command_base_has_event_id_for_idempotency(self) -> None:
        cmd = StartRun(run_id=new_id(), task_queue="q")
        assert isinstance(cmd.event_id, UUID)

    def test_two_commands_same_fields_have_different_event_ids(self) -> None:
        run_id = new_id()
        cmd1 = StartRun(run_id=run_id, task_queue="q")
        cmd2 = StartRun(run_id=run_id, task_queue="q")
        assert cmd1.event_id != cmd2.event_id


class TestStartRun:
    def test_kind_is_start_run(self) -> None:
        cmd = StartRun(run_id=new_id(), task_queue="q")
        assert cmd.kind == CommandKind.START_RUN

    def test_roundtrip_json(self) -> None:
        cmd = StartRun(run_id=new_id(), task_queue="q", max_attempts=5)
        data = cmd.model_dump(mode="json")
        restored = StartRun.model_validate(data)
        assert restored.run_id == cmd.run_id
        assert restored.task_queue == cmd.task_queue
        assert restored.max_attempts == 5

    def test_max_attempts_default(self) -> None:
        cmd = StartRun(run_id=new_id(), task_queue="q")
        assert cmd.max_attempts == 3

    def test_graph_is_optional(self) -> None:
        cmd = StartRun(run_id=new_id(), task_queue="q")
        assert cmd.graph is None


class TestCancelRun:
    def test_kind_is_cancel_run(self) -> None:
        cmd = CancelRun(run_id=new_id())
        assert cmd.kind == CommandKind.CANCEL_RUN

    def test_roundtrip_json(self) -> None:
        cmd = CancelRun(run_id=new_id(), reason="test cancel")
        data = cmd.model_dump(mode="json")
        restored = CancelRun.model_validate(data)
        assert restored.run_id == cmd.run_id
        assert restored.reason == "test cancel"

    def test_reason_is_optional(self) -> None:
        cmd = CancelRun(run_id=new_id())
        assert cmd.reason is None


class TestSignalRun:
    def test_kind_is_signal_run(self) -> None:
        cmd = SignalRun(run_id=new_id(), signal_name="heartbeat", payload={})
        assert cmd.kind == CommandKind.SIGNAL_RUN

    def test_roundtrip_json(self) -> None:
        cmd = SignalRun(run_id=new_id(), signal_name="update", payload={"key": "val"})
        data = cmd.model_dump(mode="json")
        restored = SignalRun.model_validate(data)
        assert restored.signal_name == "update"
        assert restored.payload == {"key": "val"}

    def test_payload_is_required_dict(self) -> None:
        cmd = SignalRun(run_id=new_id(), signal_name="s", payload={"a": 1})
        assert isinstance(cmd.payload, dict)


class TestPauseRun:
    def test_kind_is_pause_run(self) -> None:
        cmd = PauseRun(run_id=new_id())
        assert cmd.kind == CommandKind.PAUSE_RUN

    def test_roundtrip_json(self) -> None:
        cmd = PauseRun(run_id=new_id(), reason="maintenance")
        data = cmd.model_dump(mode="json")
        restored = PauseRun.model_validate(data)
        assert restored.reason == "maintenance"


class TestResumeRun:
    def test_kind_is_resume_run(self) -> None:
        cmd = ResumeRun(run_id=new_id())
        assert cmd.kind == CommandKind.RESUME_RUN

    def test_roundtrip_json(self) -> None:
        cmd = ResumeRun(run_id=new_id())
        data = cmd.model_dump(mode="json")
        restored = ResumeRun.model_validate(data)
        assert restored.run_id == cmd.run_id


class TestDeterministicWorkflowSignalIds:
    def test_start_run_produces_deterministic_workflow_id(self) -> None:
        run_id = new_id()
        cmd = StartRun(run_id=run_id, task_queue="q")
        assert cmd.workflow_id == f"run-{run_id}"

    def test_cancel_run_produces_deterministic_workflow_id(self) -> None:
        run_id = new_id()
        cmd = CancelRun(run_id=run_id)
        assert cmd.workflow_id == f"run-{run_id}"

    def test_signal_run_produces_deterministic_workflow_id(self) -> None:
        run_id = new_id()
        cmd = SignalRun(run_id=run_id, signal_name="s", payload={})
        assert cmd.workflow_id == f"run-{run_id}"

    def test_pause_run_produces_deterministic_workflow_id(self) -> None:
        run_id = new_id()
        cmd = PauseRun(run_id=run_id)
        assert cmd.workflow_id == f"run-{run_id}"

    def test_resume_run_produces_deterministic_workflow_id(self) -> None:
        run_id = new_id()
        cmd = ResumeRun(run_id=run_id)
        assert cmd.workflow_id == f"run-{run_id}"


class TestCommandUnion:
    def test_all_kinds_covered(self) -> None:
        expected = {"start_run", "cancel_run", "signal_run", "pause_run", "resume_run"}
        actual = {v.value for v in CommandKind}
        assert actual == expected
