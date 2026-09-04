"""S4-T8：Capability Hub API——Provider/Version/Binding CRUD + admission actions。

事实源：S4 spec §6（Web journey）、§3（Resource and lifecycle）、T8 plan。

- GET /api/v1/capabilities/providers — list provider versions
- POST /api/v1/capabilities/providers — register (import) a provider
- GET /api/v1/capabilities/providers/{id} — get provider detail
- POST /api/v1/capabilities/providers/{id}/actions — lifecycle transitions
  (inspect/test/admit/publish/suspend/revoke)
- GET /api/v1/capabilities/versions — list capability versions
- GET /api/v1/capabilities/versions/{id} — get version detail
- GET /api/v1/capabilities/versions/{id}/diff — version diff
- GET /api/v1/capabilities/bindings — list bindings
- POST /api/v1/capabilities/bindings — create binding
- DELETE /api/v1/capabilities/bindings/{id} — remove binding
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from zhiwei.capabilities.domain import (
    CapabilityBinding,
    CapabilityStatus,
    CapabilityVersion,
    ProviderVersion,
    RiskLevel,
)
from zhiwei.capabilities.repositories import (
    CapabilityBindingRepository,
    CapabilityRepository,
)
from zhiwei.capabilities.versions import CapabilityVersionManager
from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.domain import ActorContext

logger = logging.getLogger(__name__)


class _TenantContext:
    """Minimal tenant context extracted from actor."""

    def __init__(self, actor: ActorContext) -> None:
        if actor.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization context required",
            )
        if actor.workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="workspace context required",
            )
        self.organization_id = actor.organization_id
        self.workspace_id = actor.workspace_id


class ProviderRecord(BaseModel):
    """Provider version record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    provider_id: UUID
    name: str
    version: int
    description: str
    status: str
    classification: str
    source_url: str | None
    risk_level: str
    content_digest: str


class CapabilityVersionRecord(BaseModel):
    """Capability version record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    capability_type: str
    name: str
    version: int
    status: str
    risk_level: str
    content_digest: str
    test_digest: str
    parent_id: UUID | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BindingRecord(BaseModel):
    """Capability binding record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    agent_definition_id: UUID
    agent_version_id: UUID
    capability_version_id: UUID
    status: str


class RegisterProviderRequest(BaseModel):
    """POST body for registering (importing) a provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    source_url: str | None = None
    classification: str = "PUBLIC"
    risk_level: str = "low"
    content: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LifecycleActionRequest(BaseModel):
    """POST body for provider lifecycle transitions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str


class VersionDiffRecord(BaseModel):
    """Version diff projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_version: int
    to_version: int
    content_changed: bool
    risk_changed: bool
    status_changed: bool


class CreateBindingRequest(BaseModel):
    """POST body for creating a capability binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_definition_id: UUID
    agent_version_id: UUID
    capability_version_id: UUID


class _RepoStore:
    """In-memory per-tenant repo store (simulates DB RLS)."""

    def __init__(self) -> None:
        self._repos: dict[tuple[UUID, UUID], CapabilityRepository] = {}
        self._binding_repos: dict[tuple[UUID, UUID], CapabilityBindingRepository] = {}
        self._version_managers: dict[tuple[UUID, UUID], CapabilityVersionManager] = {}
        self._provider_versions: dict[UUID, ProviderVersion] = {}
        self._cap_versions: dict[UUID, CapabilityVersion] = {}
        self._bindings: dict[UUID, CapabilityBinding] = {}

    def get_repo(self, ctx: _TenantContext) -> CapabilityRepository:
        key = (ctx.organization_id, ctx.workspace_id)
        if key not in self._repos:
            self._repos[key] = CapabilityRepository(ctx.organization_id, ctx.workspace_id)
        return self._repos[key]

    def get_binding_repo(self, ctx: _TenantContext) -> CapabilityBindingRepository:
        key = (ctx.organization_id, ctx.workspace_id)
        if key not in self._binding_repos:
            self._binding_repos[key] = CapabilityBindingRepository(
                ctx.organization_id, ctx.workspace_id
            )
        return self._binding_repos[key]

    def get_version_manager(self, ctx: _TenantContext) -> CapabilityVersionManager:
        key = (ctx.organization_id, ctx.workspace_id)
        if key not in self._version_managers:
            self._version_managers[key] = CapabilityVersionManager()
        return self._version_managers[key]

    def store_provider(self, provider: ProviderVersion) -> None:
        self._provider_versions[provider.id] = provider

    def get_provider(self, provider_id: UUID) -> ProviderVersion | None:
        return self._provider_versions.get(provider_id)

    def list_providers(self, ctx: _TenantContext) -> list[ProviderVersion]:
        return list(self._provider_versions.values())

    def store_cap_version(self, version: CapabilityVersion) -> None:
        self._cap_versions[version.id] = version

    def get_cap_version(self, version_id: UUID) -> CapabilityVersion | None:
        return self._cap_versions.get(version_id)

    def store_binding(self, binding: CapabilityBinding) -> None:
        self._bindings[binding.id] = binding

    def get_binding(self, binding_id: UUID) -> CapabilityBinding | None:
        return self._bindings.get(binding_id)

    def remove_binding(self, binding_id: UUID) -> bool:
        if binding_id in self._bindings:
            del self._bindings[binding_id]
            return True
        return False

    def list_bindings(self, ctx: _TenantContext) -> list[CapabilityBinding]:
        return [
            b
            for b in self._bindings.values()
            if b.organization_id == ctx.organization_id
            and b.workspace_id == ctx.workspace_id
        ]


_store = _RepoStore()


def create_capabilities_router(
    *,
    actor_dependency: Callable[[], ActorContext],
) -> APIRouter:
    """Create the capabilities API router."""
    router = APIRouter(prefix="/api/v1/capabilities", tags=["capabilities"])

    @router.get("/providers", response_model=list[ProviderRecord])
    async def list_providers(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ProviderRecord]:
        ctx = _TenantContext(actor)
        providers = _store.list_providers(ctx)
        return [
            ProviderRecord(
                id=p.id,
                provider_id=p.provider_id,
                name=p.name,
                version=p.version,
                description=p.description,
                status=p.status.value,
                classification=p.classification,
                source_url=p.source_url,
                risk_level=p.risk_level.value,
                content_digest=p.content_digest,
            )
            for p in providers
        ]

    @router.post(
        "/providers",
        status_code=status.HTTP_201_CREATED,
        response_model=ProviderRecord,
    )
    async def register_provider(
        request: RegisterProviderRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ProviderRecord:
        ctx = _TenantContext(actor)
        provider_id = new_id()
        now = datetime.now(UTC)
        try:
            risk = RiskLevel(request.risk_level)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid risk_level: {request.risk_level}",
            ) from exc
        provider = ProviderVersion(
            id=new_id(),
            provider_id=provider_id,
            name=request.name,
            version=1,
            description=request.description,
            status=CapabilityStatus.DISCOVERED,
            classification=request.classification,
            source_url=request.source_url,
            content=request.content,
            metadata=request.metadata,
            risk_level=risk,
            created_at=now,
            updated_at=now,
        )
        _store.store_provider(provider)
        # Also register in capability version manager
        vm = _store.get_version_manager(ctx)
        cap_version = vm.register(
            capability_type="provider",
            name=request.name,
            risk_level=risk,
            content_digest=provider.content_digest,
            metadata={"provider_version_id": str(provider.id)},
        )
        _store.store_cap_version(cap_version)
        return ProviderRecord(
            id=provider.id,
            provider_id=provider.provider_id,
            name=provider.name,
            version=provider.version,
            description=provider.description,
            status=provider.status.value,
            classification=provider.classification,
            source_url=provider.source_url,
            risk_level=provider.risk_level.value,
            content_digest=provider.content_digest,
        )

    @router.get("/providers/{provider_id}", response_model=ProviderRecord)
    async def get_provider(
        provider_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ProviderRecord:
        provider = _store.get_provider(provider_id)
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="provider not found",
            )
        return ProviderRecord(
            id=provider.id,
            provider_id=provider.provider_id,
            name=provider.name,
            version=provider.version,
            description=provider.description,
            status=provider.status.value,
            classification=provider.classification,
            source_url=provider.source_url,
            risk_level=provider.risk_level.value,
            content_digest=provider.content_digest,
        )

    @router.post(
        "/providers/{provider_id}/actions",
        response_model=ProviderRecord,
    )
    async def provider_action(
        provider_id: UUID,
        request: LifecycleActionRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ProviderRecord:
        provider = _store.get_provider(provider_id)
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="provider not found",
            )
        action = request.action
        # Action → target status (API layer manages provider status directly;
        # capability version status follows provider for consistency).
        transition_map = {
            "quarantine": CapabilityStatus.QUARANTINED,
            "inspect": CapabilityStatus.INSPECTED,
            "test": CapabilityStatus.TESTED,
            "admit": CapabilityStatus.APPROVED,
            "publish": CapabilityStatus.PUBLISHED,
            "suspend": CapabilityStatus.SUSPENDED,
            "revoke": CapabilityStatus.REVOKED,
        }
        if action not in transition_map:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown action: {action}",
            )
        target_status = transition_map[action]
        # Update provider status directly (provider is the source of truth for API layer)
        updated = provider.model_copy(
            update={"status": target_status, "updated_at": datetime.now(UTC)}
        )
        _store._provider_versions[provider_id] = updated
        # Also update the associated capability version status to match
        for cv in list(_store._cap_versions.values()):
            if cv.metadata.get("provider_version_id") == str(provider_id):
                _store._cap_versions[cv.id] = cv.model_copy(
                    update={"status": target_status, "updated_at": datetime.now(UTC)}
                )
        return ProviderRecord(
            id=updated.id,
            provider_id=updated.provider_id,
            name=updated.name,
            version=updated.version,
            description=updated.description,
            status=updated.status.value,
            classification=updated.classification,
            source_url=updated.source_url,
            risk_level=updated.risk_level.value,
            content_digest=updated.content_digest,
        )

    @router.get("/versions", response_model=list[CapabilityVersionRecord])
    async def list_versions(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[CapabilityVersionRecord]:
        versions = list(_store._cap_versions.values())
        return [
            CapabilityVersionRecord(
                id=v.id,
                capability_type=v.capability_type,
                name=v.name,
                version=v.version,
                status=v.status.value,
                risk_level=v.risk_level.value,
                content_digest=v.content_digest,
                test_digest=v.test_digest,
                parent_id=v.parent_id,
                metadata=v.metadata,
            )
            for v in versions
        ]

    @router.get("/versions/{version_id}", response_model=CapabilityVersionRecord)
    async def get_version(
        version_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> CapabilityVersionRecord:
        version = _store.get_cap_version(version_id)
        if version is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="capability version not found",
            )
        return CapabilityVersionRecord(
            id=version.id,
            capability_type=version.capability_type,
            name=version.name,
            version=version.version,
            status=version.status.value,
            risk_level=version.risk_level.value,
            content_digest=version.content_digest,
            test_digest=version.test_digest,
            parent_id=version.parent_id,
            metadata=version.metadata,
        )

    @router.get("/versions/{version_id}/diff", response_model=VersionDiffRecord)
    async def version_diff(
        version_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> VersionDiffRecord:
        version = _store.get_cap_version(version_id)
        if version is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="capability version not found",
            )
        if version.version <= 1:
            return VersionDiffRecord(
                from_version=0,
                to_version=version.version,
                content_changed=True,
                risk_changed=True,
                status_changed=True,
            )
        # Find previous version of same capability
        prev_versions = [
            v
            for v in _store._cap_versions.values()
            if v.capability_type == version.capability_type
            and v.name == version.name
            and v.version == version.version - 1
        ]
        if not prev_versions:
            return VersionDiffRecord(
                from_version=version.version - 1,
                to_version=version.version,
                content_changed=True,
                risk_changed=True,
                status_changed=True,
            )
        prev = prev_versions[0]
        return VersionDiffRecord(
            from_version=prev.version,
            to_version=version.version,
            content_changed=prev.content_digest != version.content_digest,
            risk_changed=prev.risk_level != version.risk_level,
            status_changed=prev.status != version.status,
        )

    @router.get("/bindings", response_model=list[BindingRecord])
    async def list_bindings(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[BindingRecord]:
        ctx = _TenantContext(actor)
        repo = _store.get_binding_repo(ctx)
        bindings = repo.list_all()
        return [
            BindingRecord(
                id=b.id,
                organization_id=b.organization_id,
                workspace_id=b.workspace_id,
                agent_definition_id=b.agent_definition_id,
                agent_version_id=b.agent_version_id,
                capability_version_id=b.capability_version_id,
                status=b.status,
            )
            for b in bindings
        ]

    @router.post(
        "/bindings",
        status_code=status.HTTP_201_CREATED,
        response_model=BindingRecord,
    )
    async def create_binding(
        request: CreateBindingRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> BindingRecord:
        ctx = _TenantContext(actor)
        cap_version = _store.get_cap_version(request.capability_version_id)
        if cap_version is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="capability version not found",
            )
        if cap_version.status != CapabilityStatus.PUBLISHED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="can only bind published capability versions",
            )
        now = datetime.now(UTC)
        binding = CapabilityBinding(
            id=new_id(),
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            agent_definition_id=request.agent_definition_id,
            agent_version_id=request.agent_version_id,
            capability_version_id=request.capability_version_id,
            status="active",
            created_at=now,
            updated_at=now,
        )
        repo = _store.get_binding_repo(ctx)
        repo.add(binding)
        return BindingRecord(
            id=binding.id,
            organization_id=binding.organization_id,
            workspace_id=binding.workspace_id,
            agent_definition_id=binding.agent_definition_id,
            agent_version_id=binding.agent_version_id,
            capability_version_id=binding.capability_version_id,
            status=binding.status,
        )

    @router.delete(
        "/bindings/{binding_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_binding(
        binding_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> None:
        ctx = _TenantContext(actor)
        repo = _store.get_binding_repo(ctx)
        if not repo.get(binding_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="binding not found",
            )
        repo.remove(binding_id)

    return router
