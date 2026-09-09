"""ContextManifest/TransitionManifest 的 canonical PG 写入器（F-R2-12/F-R5-04）。

manifest 落账走 CanonicalUnitOfWork：canonical event + audit + outbox 同事务，
与 persistence.model_first_use 同款机制——不建第二套事件存储（runtime_events
模块 docstring 的明文约束），manifest_id 进幂等键（run 作用域内防重）。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.context.manifests import ContextManifest, TransitionManifest
from zhiwei.contracts.envelope import SchemaRegistry
from zhiwei.models.egress import (
    MANIFEST_PAYLOAD_SCHEMA_VERSION,
    SWITCH_MANIFEST_EVENT_TYPE,
    WIRE_MANIFEST_EVENT_TYPE,
    SwitchManifestPayload,
    WireManifestPayload,
)
from zhiwei.persistence.events import EventCommand
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired, tenant_session
from zhiwei.persistence.unit_of_work import CanonicalUnitOfWork

_MANIFEST_ACTOR_REF = "system:models"


def _manifest_schema_registry() -> SchemaRegistry:
    registry = SchemaRegistry()
    registry.register(
        WIRE_MANIFEST_EVENT_TYPE,
        MANIFEST_PAYLOAD_SCHEMA_VERSION,
        WireManifestPayload,
    )
    registry.register(
        SWITCH_MANIFEST_EVENT_TYPE,
        MANIFEST_PAYLOAD_SCHEMA_VERSION,
        SwitchManifestPayload,
    )
    return registry


class CanonicalManifestSink:
    """ManifestSink 的生产实现；绑定 session 工厂，每次落账一个原子事务。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        context: TenantContext,
    ) -> None:
        if context.workspace_id is None:
            raise TenantContextRequired("canonical events require workspace context")
        self._sessions = sessions
        self._context = context

    async def record_wire_manifest(
        self, manifest: ContextManifest, *, run_id: UUID
    ) -> bool:
        payload = WireManifestPayload(
            manifest_id=manifest.manifest_id,
            body_sha256=manifest.body_sha256,
            body_len=manifest.body_len,
            url=manifest.url,
            method=manifest.method,
            redacted_headers=dict(manifest.redacted_headers),
            header_names=list(manifest.header_names),
            captured_at=manifest.captured_at,
            sequence_no=manifest.sequence_no,
        ).model_dump()
        return await self._append(WIRE_MANIFEST_EVENT_TYPE, manifest, payload, run_id)

    async def record_transition_manifest(
        self, manifest: TransitionManifest, *, run_id: UUID
    ) -> bool:
        payload = SwitchManifestPayload(
            manifest_id=manifest.manifest_id,
            transition_type=manifest.transition_type,
            wire_body_digest=manifest.wire_body_digest,
            before_state_digest=manifest.before_state_digest,
            after_state_digest=manifest.after_state_digest,
            occurred_at=manifest.occurred_at,
        ).model_dump()
        return await self._append(SWITCH_MANIFEST_EVENT_TYPE, manifest, payload, run_id)

    async def _append(
        self,
        event_type: str,
        manifest: ContextManifest | TransitionManifest,
        payload: dict[str, object],
        run_id: UUID,
    ) -> bool:
        async with tenant_session(self._sessions, self._context) as session:
            uow = CanonicalUnitOfWork(
                session, self._context, schema_registry=_manifest_schema_registry()
            )
            result = await uow.append_event(
                EventCommand(
                    run_id=run_id,
                    event_type=event_type,
                    payload_schema_version=MANIFEST_PAYLOAD_SCHEMA_VERSION,
                    payload=payload,
                    actor_ref=_MANIFEST_ACTOR_REF,
                    idempotency_key=f"{event_type}:{manifest.manifest_id}",
                )
            )
            return result.created
