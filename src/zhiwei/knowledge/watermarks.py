"""Sync watermarks: track the last successful sync point per connector+workspace."""

from __future__ import annotations

from uuid import UUID

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.knowledge.contracts import SyncWatermark


class WatermarkNotFoundError(Exception):
    """Raised when a requested watermark does not exist."""


class WatermarkManager:
    """Manages SyncWatermark lifecycle: create, advance, query.

    Watermarks are the sync progress indicator for incremental sync.
    Each connector+workspace pair has at most one active watermark.
    """

    def __init__(self) -> None:
        self._watermarks: dict[tuple[str, UUID, UUID], SyncWatermark] = {}

    def _key(self, connector: str, organization_id: UUID, workspace_id: UUID) -> tuple[str, UUID, UUID]:
        return (connector, organization_id, workspace_id)

    def get_or_create(
        self,
        connector: str,
        organization_id: UUID,
        workspace_id: UUID,
        *,
        initial_watermark: str = "0",
    ) -> SyncWatermark:
        """Get existing watermark or create with initial value."""
        key = self._key(connector, organization_id, workspace_id)
        if key in self._watermarks:
            return self._watermarks[key]

        watermark = SyncWatermark(
            id=new_id(),
            connector=connector,
            organization_id=organization_id,
            workspace_id=workspace_id,
            watermark=initial_watermark,
            last_synced_at=utc_now(),
            sync_count=0,
        )
        self._watermarks[key] = watermark
        return watermark

    def advance(
        self,
        connector: str,
        organization_id: UUID,
        workspace_id: UUID,
        *,
        new_watermark: str,
        last_event_id: str | None = None,
    ) -> SyncWatermark:
        """Advance the watermark after a successful sync.

        Returns the updated watermark. Raises WatermarkNotFoundError
        if no watermark exists for this connector+workspace pair.
        """
        key = self._key(connector, organization_id, workspace_id)
        if key not in self._watermarks:
            raise WatermarkNotFoundError(
                f"No watermark for {connector}/{organization_id}/{workspace_id}"
            )

        old = self._watermarks[key]
        updated = old.model_copy(
            update={
                "watermark": new_watermark,
                "last_synced_at": utc_now(),
                "last_event_id": last_event_id or old.last_event_id,
                "sync_count": old.sync_count + 1,
            }
        )
        self._watermarks[key] = updated
        return updated

    def get(
        self,
        connector: str,
        organization_id: UUID,
        workspace_id: UUID,
    ) -> SyncWatermark | None:
        """Get the current watermark, or None if not initialized."""
        key = self._key(connector, organization_id, workspace_id)
        return self._watermarks.get(key)

    def list_all(self, organization_id: UUID) -> list[SyncWatermark]:
        """List all watermarks for an organization."""
        return [
            wm
            for wm in self._watermarks.values()
            if wm.organization_id == organization_id
        ]
