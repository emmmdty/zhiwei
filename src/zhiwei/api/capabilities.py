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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zhiwei.api.policy_gate import authorize_read, request_trace
from zhiwei.capabilities.admission import AdmissionManager
from zhiwei.capabilities.admission_commands import ApprovalPEP
from zhiwei.capabilities.domain import (
    CapabilityBinding,
    CapabilityStatus,
    CapabilityVersion,
    ProviderVersion,
    RiskLevel,
)
from zhiwei.capabilities.inspection.pipeline import run_admission_inspections
from zhiwei.capabilities.repositories import (
    CapabilityBindingRepository,
    CapabilityRepository,
)
from zhiwei.capabilities.versions import (
    CapabilityVersionManager,
    InvalidTransitionError,
    NotFoundError,
    VersionConflictError,
    prepare_upstream_candidate,
)
from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.domain import ActorContext
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, ResourceType, Risk

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
    """In-memory per-tenant repo store (simulates DB RLS).

    F-R3-01 止血：provider/capability version 域模型无租户字段，归属由 store 层
    side-index 在写入时归因（来源 = 注册 actor 的 tenant context）；一切读取必须
    携带 tenant 谓词，跨租户/未归因记录一律不可见（fail closed，无存在性泄漏）。
    """

    def __init__(self) -> None:
        self._repos: dict[tuple[UUID, UUID], CapabilityRepository] = {}
        self._binding_repos: dict[tuple[UUID, UUID], CapabilityBindingRepository] = {}
        self._version_managers: dict[tuple[UUID, UUID], CapabilityVersionManager] = {}
        self._provider_versions: dict[UUID, ProviderVersion] = {}
        self._cap_versions: dict[UUID, CapabilityVersion] = {}
        self._bindings: dict[UUID, CapabilityBinding] = {}
        self._provider_tenants: dict[UUID, tuple[UUID, UUID]] = {}
        self._cap_version_tenants: dict[UUID, tuple[UUID, UUID]] = {}
        self._admission_managers: dict[tuple[UUID, UUID], AdmissionManager] = {}

    @staticmethod
    def _tenant_key(organization_id: UUID, workspace_id: UUID) -> tuple[UUID, UUID]:
        return (organization_id, workspace_id)

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

    def get_admission_manager(self, ctx: _TenantContext) -> AdmissionManager:
        """审批记录的租户内权威存储（F-R9-01：publish readiness 求值输入）。"""
        key = (ctx.organization_id, ctx.workspace_id)
        if key not in self._admission_managers:
            self._admission_managers[key] = AdmissionManager()
        return self._admission_managers[key]

    def store_provider(
        self,
        provider: ProviderVersion,
        *,
        organization_id: UUID,
        workspace_id: UUID,
    ) -> None:
        self._provider_tenants[provider.id] = self._tenant_key(
            organization_id, workspace_id
        )
        self._provider_versions[provider.id] = provider

    def get_provider(
        self,
        provider_id: UUID,
        *,
        organization_id: UUID,
        workspace_id: UUID,
    ) -> ProviderVersion | None:
        # 归属谓词在存在性之前生效：跨租户与不存在同形（404，不泄漏存在性）
        if self._provider_tenants.get(provider_id) != self._tenant_key(
            organization_id, workspace_id
        ):
            return None
        return self._provider_versions.get(provider_id)

    def list_providers(self, ctx: _TenantContext) -> list[ProviderVersion]:
        key = self._tenant_key(ctx.organization_id, ctx.workspace_id)
        return [
            p
            for p in self._provider_versions.values()
            if self._provider_tenants.get(p.id) == key
        ]

    def store_cap_version(
        self,
        version: CapabilityVersion,
        *,
        organization_id: UUID,
        workspace_id: UUID,
    ) -> None:
        self._cap_version_tenants[version.id] = self._tenant_key(
            organization_id, workspace_id
        )
        self._cap_versions[version.id] = version

    def get_cap_version(
        self,
        version_id: UUID,
        *,
        organization_id: UUID,
        workspace_id: UUID,
    ) -> CapabilityVersion | None:
        if self._cap_version_tenants.get(version_id) != self._tenant_key(
            organization_id, workspace_id
        ):
            return None
        return self._cap_versions.get(version_id)

    def list_cap_versions(self, ctx: _TenantContext) -> list[CapabilityVersion]:
        key = self._tenant_key(ctx.organization_id, ctx.workspace_id)
        return [
            v
            for v in self._cap_versions.values()
            if self._cap_version_tenants.get(v.id) == key
        ]

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
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """Create the capabilities API router.

    F-R9-01 止血：生命周期动作为供应链信任根，角色裁决经 policy_enforcer
    （P1 决策式 PEP；mutation 审计随 P2b 纵切接线）。
    """
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
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
        http_request: Request,
        body: RegisterProviderRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ProviderRecord:
        ctx = _TenantContext(actor)
        provider_id = new_id()
        pv_id = new_id()
        now = datetime.now(UTC)
        try:
            risk = RiskLevel(body.risk_level)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid risk_level: {body.risk_level}",
            ) from exc
        # 导入即准入链第一环（矩阵 import_check_test cell：capability_publisher）；
        # 决策 resource 对齐实际持久化的 ProviderVersion（审计/decision log 可寻址）
        _, trace_id = request_trace(http_request)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            policy_type=ResourceType.CAPABILITY_VERSION,
            policy_action=Action.IMPORT_CHECK_TEST,
            resource_id=pv_id,
            trace_id=trace_id,
            risk=Risk(body.risk_level),
        )
        provider = ProviderVersion(
            id=pv_id,
            provider_id=provider_id,
            name=body.name,
            version=1,
            description=body.description,
            status=CapabilityStatus.DISCOVERED,
            classification=body.classification,
            source_url=body.source_url,
            content=body.content,
            metadata=body.metadata,
            risk_level=risk,
            created_at=now,
            updated_at=now,
        )
        _store.store_provider(
            provider,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
        )
        # Also register in capability version manager
        vm = _store.get_version_manager(ctx)
        # upstream update → candidate（F-R9-03）：同名已发布版本存在且内容变化时，
        # 新版本以 parent_id 挂靠并携带 drift 报告；绑定面不受影响（S4 spec §4）。
        candidate_params = prepare_upstream_candidate(
            manager=vm, name=body.name, content_digest=provider.content_digest
        )
        cap_version = vm.register(
            capability_type="provider",
            name=body.name,
            risk_level=risk,
            content_digest=provider.content_digest,
            metadata={"provider_version_id": str(provider.id), **candidate_params.get("metadata", {})},
            parent_id=candidate_params.get("parent_id"),
        )
        _store.store_cap_version(
            cap_version,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
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

    @router.get("/providers/{provider_id}", response_model=ProviderRecord)
    async def get_provider(
        provider_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ProviderRecord:
        ctx = _TenantContext(actor)
        provider = _store.get_provider(
            provider_id,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
        )
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
        http_request: Request,
        body: LifecycleActionRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ProviderRecord:
        ctx = _TenantContext(actor)
        provider = _store.get_provider(
            provider_id,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
        )
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="provider not found",
            )
        action = body.action
        # Action → target status（生命周期只经 CapabilityVersionManager 状态机）
        transition_map: dict[str, CapabilityStatus] = {
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
        # 准入主体：provider 关联的 capability version（准入链作用对象）
        cap_versions = [
            cv
            for cv in _store.list_cap_versions(ctx)
            if cv.metadata.get("provider_version_id") == str(provider_id)
        ]
        if not cap_versions:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="capability version not found",
            )
        # 动作 → 矩阵 cell（角色语义唯一事实源是 Rego；PEP 决策，deny → 403）
        action_policy: dict[str, Action] = {
            "quarantine": Action.IMPORT_CHECK_TEST,
            "inspect": Action.IMPORT_CHECK_TEST,
            "test": Action.IMPORT_CHECK_TEST,
            "admit": Action.ADMIT_LOW_MEDIUM,
            "publish": Action.ADMIT_LOW_MEDIUM,
            "suspend": Action.SUSPEND,
            "revoke": Action.REVOKE,
        }
        _, trace_id = request_trace(http_request)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            policy_type=ResourceType.CAPABILITY_VERSION,
            policy_action=action_policy[action],
            resource_id=cap_versions[0].id,
            trace_id=trace_id,
            risk=Risk(provider.risk_level.value),
        )
        vm = _store.get_version_manager(ctx)
        now = datetime.now(UTC)
        if action == "publish":
            # publish 经 ApprovalPEP：结构预检（409）→ readiness（403）→ CAS 转换
            admission_pep = ApprovalPEP(_store.get_admission_manager(ctx))
            for cv in cap_versions:
                try:
                    result = admission_pep.execute_publish(
                        cv.id,
                        cv.test_digest,
                        cv.content_digest,
                        cv.risk_level,
                        expected_version=vm.row_version(cv.id),
                        version_manager=vm,
                    )
                except InvalidTransitionError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=str(exc),
                    ) from exc
                except VersionConflictError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=str(exc),
                    ) from exc
                except NotFoundError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="capability version not found",
                    ) from exc
                if not result.success:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=result.error or "publish not approved",
                    )
            target_status = CapabilityStatus.PUBLISHED
        else:
            target_status = transition_map[action]
            for cv in cap_versions:
                try:
                    if action == "inspect":
                        # F-R9-02：inspect 执行四类检查并持久化报告——「已 inspected」
                        # 必须构成安全事实，而非单纯状态改写。
                        report = run_admission_inspections(provider)
                        vm.attach_metadata(cv.id, {"inspection": report})
                    if action == "test":
                        inspection_report = cv.metadata.get("inspection")
                        if (
                            not isinstance(inspection_report, dict)
                            or inspection_report.get("passed") is not True
                        ):
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=(
                                    "cannot enter tested: inspection report missing "
                                    "or has blocking findings"
                                ),
                            )
                    vm.transition(cv.id, target_status)
                except InvalidTransitionError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=str(exc),
                    ) from exc
                except NotFoundError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="capability version not found",
                    ) from exc
        # 镜像状态：provider 为 API 面投影，capability version 以状态机为准
        for cv in cap_versions:
            updated_version = vm.get(cv.id)
            if updated_version is not None:
                _store._cap_versions[cv.id] = updated_version
        updated = provider.model_copy(
            update={"status": target_status, "updated_at": now}
        )
        _store._provider_versions[provider_id] = updated
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
        ctx = _TenantContext(actor)
        versions = _store.list_cap_versions(ctx)
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
        ctx = _TenantContext(actor)
        version = _store.get_cap_version(
            version_id,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
        )
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
        ctx = _TenantContext(actor)
        version = _store.get_cap_version(
            version_id,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
        )
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
        # Find previous version of same capability（同租户内扫描，防跨租户 diff 归因）
        prev_versions = [
            v
            for v in _store.list_cap_versions(ctx)
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
        # 绑定目标版本必须在本租户内：跨租户版本与不存在同形 404（防跨租户绑定）
        cap_version = _store.get_cap_version(
            request.capability_version_id,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
        )
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
