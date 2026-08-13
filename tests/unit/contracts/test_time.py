"""S0-T2 RED：UTC aware time helpers。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from zhiwei.contracts.time import ensure_utc, utc_now


def test_utc_now_returns_an_aware_utc_datetime() -> None:
    value = utc_now()
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


def test_ensure_utc_normalizes_an_aware_datetime() -> None:
    east_eight = timezone(timedelta(hours=8))
    value = datetime(2026, 8, 13, 20, 0, tzinfo=east_eight)
    assert ensure_utc(value) == datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_ensure_utc_returns_utc_timezone() -> None:
    east_eight = timezone(timedelta(hours=8))
    normalized = ensure_utc(datetime(2026, 8, 13, 20, 0, tzinfo=east_eight))
    assert normalized.tzinfo is UTC


def test_ensure_utc_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 8, 13, 12, 0))
