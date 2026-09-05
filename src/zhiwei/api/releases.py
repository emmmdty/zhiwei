"""S9-T4：Release API——生命周期推进（SoD）、canary 路由与 new-runs-only 回滚。

工厂模式与 api/runs.py / api/observability.py 同型：组合期注入 actor 依赖 /
session factory / policy enforcer，端点内不做授权语义决策（PEP 判定统一走
policy_gate）。

策略 cell 映射（冻结矩阵 docs/PERMISSIONS.md §3.1，不新增 cell）：advance 的
策略 cell 取发布行（agent_publish）内 PEP 可承载的动作——

- builder（agent_builder）→ agent_publish.request（builder 对 publish 只能 request）；
- reviewer / approver / release_manager → agent_publish.rollback（矩阵行
  「复核并发布/回滚」的 workspace_admin cell）。

【已知设计缺口，需设计方裁决】review_publish / tool_approval.approve 这两个
语义上更精确的 cell 在 PolicyInput 边界强制 SoD 证据（last_content_author /
requester），而 policy_gate.authorize_mutation 固定传空 ResourceContext——
任何人经 PEP 走这两个 cell 都必然被边界拒绝（policy_input_invalid）。在
policy_gate 支持携带 resource_context 事实之前，发布行治理统一走 rollback
cell；域层 require_transition_permission 的角色 SoD 不受影响（双重防线中
域层这道完整有效）。

角色解析：release 角色由 actor.role_bindings 的平台角色确定；workspace_admin
同时映射 reviewer/approver/release_manager（权限取并集——PERMISSIONS §3.1），
approver 平台角色亦映射 approver release 角色。
"""

# 注意：本模块【不用】from __future__ import annotations——endpoint 签名里的
# `Annotated[ActorContext, Depends(actor_dependency)]` 引用工厂闭包变量，必须
# 在 def 期立即求值才能被 FastAPI 解析（见 api/observability.py 同款说明）。

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.agents.release import (
    ALLOWED_RELEASE_TRANSITIONS,
    ReleaseManifest,
    ReleaseNotFound,
    ReleaseRecord,
    ReleaseService,
    ReleaseState,
    ReleaseTransitionDenied,
)
from zhiwei.agents.rollout import (
    RollbackNotApplicable,
    RollbackPolicy,
    RolloutNotConfigured,
    RolloutPolicy,
)
from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    authorize_read,
    request_trace,
)
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType


class CreateReleaseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: UUID
    agent_version: int
    pack_digest: str
    model_digest: str
    knowledge_digest: str
    memory_digest: str
    capability_digest: str
    policy_digest: str
    eval_digests: list[str] = []
    approver: str | None = None
    rollout: RolloutPolicy
    rollback: RollbackPolicy


class AdvanceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_state: str


class RouteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID | None = None
    user_id: UUID | None = None
    suspended: bool = False


class RollbackRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    to_version: int
    in_flight_run_ids: list[UUID] = []


class ReleaseView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_id: str
    agent_id: str
    agent_version: int
    state: str
    manifest_digest: str
    default_version: int | None


# release 角色 → 平台角色绑定（docs/PERMISSIONS.md §3.1 冻结矩阵）
_RELEASE_ROLE_PLATFORM_ROLES = {
    "builder": {"agent_builder"},
    "reviewer": {"workspace_admin"},
    "approver": {"approver", "workspace_admin"},
    "release_manager": {"workspace_admin"},
}

# release 角色 → 冻结策略 cell（映射论证与已知缺口见模块 docstring）
_RELEASE_ROLE_POLICY_CELLS = {
    "builder": (ResourceType.AGENT_PUBLISH, Action.REQUEST),
    "reviewer": (ResourceType.AGENT_PUBLISH, Action.ROLLBACK),
    "approver": (ResourceType.AGENT_PUBLISH, Action.ROLLBACK),
    "release_manager": (ResourceType.AGENT_PUBLISH, Action.ROLLBACK),
}


def _tenant(actor: ActorContext) -> TenantContext:
    if actor.organization_id is None or actor.workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization and workspace context required",
        )
    return TenantContext(
        organization_id=actor.organization_id, workspace_id=actor.workspace_id
    )


def _release_roles(actor: ActorContext) -> set[str]:
    names = {binding.name for binding in actor.role_bindings}
    return {
        role for role, platforms in _RELEASE_ROLE_PLATFORM_ROLES.items() if names & platforms
    }


def _resolve_transition_role(
    actor: ActorContext, current: ReleaseState, target: ReleaseState
) -> str:
    """从 actor 绑定解析传给域层的角色串。

    持有所需角色 → 传之（域层复核 SoD）；不持有 → 传 actor 现有 release 角色
    （无则固定占位串），由域层按未授权/未知角色拒绝 → 409 + failed 审计。
    """
    required = ALLOWED_RELEASE_TRANSITIONS.get((current, target), frozenset())
    held = _release_roles(actor)
    overlap = sorted(held & required)
    if overlap:
        return overlap[0]
    if held:
        return sorted(held)[0]
    return "none"


def _refusal(reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail={"reason": reason, "message": message}
    )


def _view(record: ReleaseRecord) -> ReleaseView:
    return ReleaseView(
        release_id=str(record.release_id),
        agent_id=str(record.agent_id),
        agent_version=record.agent_version,
        state=record.state.value,
        manifest_digest=record.manifest.content_digest,
        default_version=record.rollout.default_version,
    )


def create_releases_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1/releases", tags=["releases"])

    @router.get("", response_model=list[ReleaseView])
    async def list_releases(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ReleaseView]:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        # 读路径 PEP（ADR-012 决策 4）：读 cell 取冻结矩阵「agent_publish.read_manifest」
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.READ_MANIFEST,
            resource_id=context.organization_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            records = await ReleaseService(session, context).list()
        return [_view(record) for record in records]

    @router.get("/{release_id}", response_model=ReleaseView)
    async def get_release(
        release_id: UUID,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ReleaseView:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.READ_MANIFEST,
            resource_id=release_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            try:
                record = await ReleaseService(session, context).get(release_id)
            except ReleaseNotFound:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="release not found"
                ) from None
        return _view(record)

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=ReleaseView)
    async def create_release(
        request: CreateReleaseRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ReleaseView:
        context = _tenant(actor)
        # manifest 构造是请求校验（422），先于策略求值；构造失败无 mutation 义务
        try:
            manifest = ReleaseManifest(
                agent_id=request.agent_id,
                agent_version=request.agent_version,
                pack_digest=request.pack_digest,
                model_digest=request.model_digest,
                knowledge_digest=request.knowledge_digest,
                memory_digest=request.memory_digest,
                capability_digest=request.capability_digest,
                policy_digest=request.policy_digest,
                eval_digests=tuple(request.eval_digests),
                approver=request.approver or str(actor.principal_id),
                rollout=request.rollout,
                rollback=request.rollback,
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        request_id, trace_id = request_trace(request_scope)
        # builder 的发布请求（矩阵：agent_builder 对 publish 只能 request）
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="agent.release.create",
            resource_type="agent_release",
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.REQUEST,
            resource_id=manifest.agent_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            try:
                record = await ReleaseService(session, context).create_draft(manifest)
            except Exception as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="agent.release.create",
                    resource_type="agent_release",
                    resource_id=manifest.agent_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="agent.release.create",
                resource_type="agent_release",
                resource_id=record.release_id,
                resource_version=1,
                authorization=authorization,
            )
        return _view(record)

    @router.post("/{release_id}/advance", response_model=ReleaseView)
    async def advance_release(
        release_id: UUID,
        request: AdvanceRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ReleaseView:
        context = _tenant(actor)
        try:
            target = ReleaseState(request.target_state)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="unknown release state",
            ) from exc
        async with tenant_session(sessions, context) as session:
            try:
                current = (await ReleaseService(session, context).get(release_id)).state
            except ReleaseNotFound:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="release not found"
                ) from None
        required = ALLOWED_RELEASE_TRANSITIONS.get((current, target))
        if not required:
            # 结构上不存在的转移（跳级/回退/终态出边）：422 级客户端错误，
            # 无 mutation 意图，与 runs 决策枚举校验同型（不做 denied 审计）
            raise _refusal(
                "release_transition_denied",
                f"release transition {current.value} -> {target.value} is not allowed",
            )
        policy_type, policy_action = _RELEASE_ROLE_POLICY_CELLS[sorted(required)[0]]
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="agent.release.advance",
            resource_type="agent_release",
            policy_type=policy_type,
            policy_action=policy_action,
            resource_id=release_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        role = _resolve_transition_role(actor, current, target)
        async with tenant_session(sessions, context) as session:
            try:
                record = await ReleaseService(session, context).advance(
                    release_id, target=target, role=role
                )
            except ReleaseTransitionDenied as error:
                # SoD 拒绝：业务失败审计（独立事务）+ 409 机器可读拒绝面
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="agent.release.advance",
                    resource_type="agent_release",
                    resource_id=release_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise _refusal("release_transition_denied", str(error)) from error
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="agent.release.advance",
                resource_type="agent_release",
                resource_id=release_id,
                resource_version=1,
                authorization=authorization,
            )
        return _view(record)

    @router.post("/{release_id}/route")
    async def route_release(
        release_id: UUID,
        request: RouteRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> dict[str, Any]:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.READ_MANIFEST,
            resource_id=release_id,
            trace_id=trace_id,
        )
        workspace_id = request.workspace_id or context.workspace_id
        user_id = request.user_id or actor.principal_id
        assert workspace_id is not None, "tenant context guarantees a workspace"
        async with tenant_session(sessions, context) as session:
            try:
                version = await ReleaseService(session, context).route(
                    release_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    suspended=request.suspended,
                )
            except ReleaseNotFound:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="release not found"
                ) from None
            except RolloutNotConfigured as error:
                raise _refusal("rollout_not_configured", str(error)) from error
        return {"release_id": str(release_id), "version": version}

    @router.post("/{release_id}/rollback")
    async def rollback_release(
        release_id: UUID,
        request: RollbackRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> dict[str, Any]:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        # 矩阵：agent_publish.rollback → workspace_admin（复核并发布/回滚）
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="agent.release.rollback",
            resource_type="agent_release",
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.ROLLBACK,
            resource_id=release_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            try:
                outcome = await ReleaseService(session, context).rollback(
                    release_id,
                    to_version=request.to_version,
                    in_flight_run_ids=request.in_flight_run_ids,
                )
            except ReleaseNotFound:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="release not found"
                ) from None
            except RollbackNotApplicable as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="agent.release.rollback",
                    resource_type="agent_release",
                    resource_id=release_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise _refusal("rollback_not_applicable", str(error)) from error
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="agent.release.rollback",
                resource_type="agent_release",
                resource_id=release_id,
                resource_version=1,
                authorization=authorization,
            )
        return {
            "release_id": str(release_id),
            "applies_to": outcome.applies_to,
            "executed": outcome.executed,
            "in_flight_disposition": outcome.in_flight_disposition,
            "in_flight_run_ids": [str(run_id) for run_id in outcome.in_flight_run_ids],
            "default_version": outcome.policy.default_version,
        }

    return router
