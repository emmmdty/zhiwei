"""S11-T3 单元契约：backward reader 与升级清单（docs/operations/upgrade.md §2.3/§2.4）。

冻结断言（A 档契约，先于实现）：

1. reader 接受 schema_version ∈ {1,2}；未知版本拒绝（fail closed）；
2. `dispatch_deadline IS NULL` 行 → v1 代：fallback `available_at + 300s` 且标注 era；
3. 非 NULL 行 → v2 代，deadline 原样保留；
4. UpgradeManifest round-trip：worker_build_id/previous_revision/target_revision 可校验。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zhiwei.operations.upgrade import (
    OutboxRowView,
    UpgradeManifest,
    read_outbox_rows_cross_era,
)

GRACE = timedelta(seconds=300)


def _row(
    *,
    schema_version: int,
    deadline: datetime | None,
    available_at: datetime | None = None,
) -> OutboxRowView:
    return OutboxRowView(
        id="00000000-0000-0000-0000-000000000001",
        topic="runtime.command",
        schema_version=schema_version,
        available_at=available_at or datetime(2025, 1, 1, tzinfo=UTC),
        dispatch_deadline=deadline,
        payload_schema_version=1,
    )


def test_reader_accepts_known_schema_versions() -> None:
    rows = [_row(schema_version=1, deadline=None), _row(schema_version=2, deadline=None)]
    views = read_outbox_rows_cross_era(rows, now=datetime(2025, 1, 2, tzinfo=UTC))
    assert len(views) == 2


def test_reader_rejects_unknown_schema_version() -> None:
    rows = [_row(schema_version=99, deadline=None)]
    with pytest.raises(ValueError, match="schema_version"):
        read_outbox_rows_cross_era(rows, now=datetime(2025, 1, 2, tzinfo=UTC))


def test_legacy_row_gets_fallback_deadline_and_v1_era() -> None:
    available = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    rows = [_row(schema_version=1, deadline=None, available_at=available)]
    views = read_outbox_rows_cross_era(rows, now=available)
    assert views[0].era == "v1"
    assert views[0].effective_deadline == available + GRACE


def test_modern_row_keeps_deadline_and_v2_era() -> None:
    deadline = datetime(2025, 1, 1, 12, 5, 0, tzinfo=UTC)
    rows = [_row(schema_version=2, deadline=deadline)]
    views = read_outbox_rows_cross_era(rows, now=deadline)
    assert views[0].era == "v2"
    assert views[0].effective_deadline == deadline


def test_manifest_roundtrip_and_revision_check() -> None:
    manifest = UpgradeManifest(
        previous_revision="0019",
        target_revision="0026",
        worker_build_id="release-0.1.0",
        requires_checkpoint=True,
        opensearch_rebuild=False,
    )
    restored = UpgradeManifest.model_validate_json(manifest.model_dump_json())
    assert restored == manifest
    assert restored.check_matches(current_revision="0019", worker_build_id="release-0.1.0")
    assert not restored.check_matches(current_revision="0024", worker_build_id="release-0.1.0")
    assert not restored.check_matches(current_revision="0019", worker_build_id="other")
