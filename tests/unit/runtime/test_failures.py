"""S2-T5 RED: Failure taxonomy tests."""

from __future__ import annotations

from zhiwei.runtime.failures import (
    FailureCategory,
    FailureTaxonomy,
)


def _make_taxonomy() -> FailureTaxonomy:
    return FailureTaxonomy()


class TestFailureTaxonomy:
    """Typed failure reasons."""

    def test_record_failure(self) -> None:
        tax = _make_taxonomy()
        record = tax.record(
            run_id="r1",
            task_id="t1",
            category=FailureCategory.TOOL_EXECUTION,
            message="connection refused",
        )
        assert record.category == FailureCategory.TOOL_EXECUTION
        assert record.message == "connection refused"

    def test_all_categories_represented(self) -> None:
        for cat in FailureCategory:
            assert isinstance(cat.value, str)

    def test_failure_has_timestamp(self) -> None:
        tax = _make_taxonomy()
        record = tax.record(
            run_id="r1",
            task_id="t1",
            category=FailureCategory.TIMEOUT,
            message="timed out",
        )
        assert record.timestamp is not None


class TestEffectUnknownHandling:
    """effect_unknown handling: write event, do NOT auto-retry."""

    def test_effect_unknown_records_failure(self) -> None:
        tax = _make_taxonomy()
        record = tax.record(
            run_id="r1",
            task_id="t1",
            category=FailureCategory.EFFECT_UNKNOWN,
            message="uncertain outcome",
        )
        assert record.category == FailureCategory.EFFECT_UNKNOWN
        assert record.auto_retry is False

    def test_effect_unknown_no_auto_retry(self) -> None:
        tax = _make_taxonomy()
        record = tax.record(
            run_id="r1",
            task_id="t1",
            category=FailureCategory.EFFECT_UNKNOWN,
            message="uncertain outcome",
        )
        assert record.auto_retry is False

    def test_failures_for_task(self) -> None:
        tax = _make_taxonomy()
        tax.record(run_id="r1", task_id="t1", category=FailureCategory.TOOL_EXECUTION, message="err1")
        tax.record(run_id="r1", task_id="t1", category=FailureCategory.TIMEOUT, message="err2")
        tax.record(run_id="r1", task_id="t2", category=FailureCategory.TIMEOUT, message="err3")
        t1_failures = tax.failures_for_task("t1")
        assert len(t1_failures) == 2


class TestCancellationStopsNewTasks:
    """Cancellation stops new tasks, records in-flight effect state."""

    def test_cancellation_recorded(self) -> None:
        tax = _make_taxonomy()
        record = tax.record(
            run_id="r1",
            task_id="t1",
            category=FailureCategory.CANCELLED,
            message="cancelled by operator",
        )
        assert record.category == FailureCategory.CANCELLED
