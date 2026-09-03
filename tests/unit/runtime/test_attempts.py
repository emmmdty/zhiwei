"""S2-T2 RED: Attempt lifecycle management tests."""

from __future__ import annotations

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.attempts import AttemptError, AttemptManager


class TestAttemptManager:
    def test_create_attempt(self) -> None:
        mgr = AttemptManager()
        attempt = mgr.create(task_id="t1", attempt_number=1)
        assert attempt.task_id == "t1"
        assert attempt.attempt_number == 1
        assert attempt.status == "pending"

    def test_commit_attempt(self) -> None:
        mgr = AttemptManager()
        attempt = mgr.create(task_id="t1", attempt_number=1)
        committed = mgr.commit(attempt.id)
        assert committed.status == "committed"

    def test_abort_attempt(self) -> None:
        mgr = AttemptManager()
        attempt = mgr.create(task_id="t1", attempt_number=1)
        aborted = mgr.abort(attempt.id)
        assert aborted.status == "aborted"

    def test_cannot_commit_nonexistent_attempt(self) -> None:
        mgr = AttemptManager()
        with pytest.raises(AttemptError):
            mgr.commit(new_id())

    def test_cannot_abort_nonexistent_attempt(self) -> None:
        mgr = AttemptManager()
        with pytest.raises(AttemptError):
            mgr.abort(new_id())

    def test_cannot_commit_already_committed(self) -> None:
        mgr = AttemptManager()
        attempt = mgr.create(task_id="t1", attempt_number=1)
        mgr.commit(attempt.id)
        with pytest.raises(AttemptError):
            mgr.commit(attempt.id)

    def test_cannot_abort_already_committed(self) -> None:
        mgr = AttemptManager()
        attempt = mgr.create(task_id="t1", attempt_number=1)
        mgr.commit(attempt.id)
        with pytest.raises(AttemptError):
            mgr.abort(attempt.id)

    def test_cannot_commit_already_aborted(self) -> None:
        mgr = AttemptManager()
        attempt = mgr.create(task_id="t1", attempt_number=1)
        mgr.abort(attempt.id)
        with pytest.raises(AttemptError):
            mgr.commit(attempt.id)

    def test_get_attempt(self) -> None:
        mgr = AttemptManager()
        attempt = mgr.create(task_id="t1", attempt_number=1)
        fetched = mgr.get(attempt.id)
        assert fetched.id == attempt.id
        assert fetched.task_id == "t1"

    def test_get_nonexistent_attempt_raises(self) -> None:
        mgr = AttemptManager()
        with pytest.raises(AttemptError):
            mgr.get(new_id())

    def test_attempts_for_task(self) -> None:
        mgr = AttemptManager()
        a1 = mgr.create(task_id="t1", attempt_number=1)
        a2 = mgr.create(task_id="t1", attempt_number=2)
        mgr.create(task_id="t2", attempt_number=1)
        t1_attempts = mgr.attempts_for_task("t1")
        assert len(t1_attempts) == 2
        assert {a.id for a in t1_attempts} == {a1.id, a2.id}

    def test_attempt_has_unique_id(self) -> None:
        mgr = AttemptManager()
        a1 = mgr.create(task_id="t1", attempt_number=1)
        a2 = mgr.create(task_id="t1", attempt_number=2)
        assert a1.id != a2.id
