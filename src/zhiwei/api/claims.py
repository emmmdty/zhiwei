"""S9-T4：Claim Registry API——注册、复核驱动的升级与租户内检索。

工厂模式与 api/releases.py 同型。服务层升级路径从 object store 独立复算密封件
（不信任调用方验证结论），拒绝面返回结构化 detail：{"reason": ...} 机器可读，
release checker 与前端按 reason 分支而不是解析消息文本。

策略 cell（冻结矩阵，不新增 cell）：register/upgrade 是 publish 侧治理动作，
统一走 agent_publish.request（builder 提交/补交密封证据）。【已知设计缺口，
同 api/releases.py】语义上更精确的 review_publish 在 PolicyInput 边界强制
last_content_author SoD 证据，而 authorize_mutation 固定传空 ResourceContext，
经 PEP 必然被边界拒绝；在 policy_gate 支持携带 resource_context 之前降级使用
request cell。真正的升级门禁是服务层从 object store 独立复算密封件 + 域层
upgrade_claim 的口径/digest 防线，与 PEP 正交。读 cell 用
agent_publish.read_manifest。
"""

# 注意：本模块【不用】from __future__ import annotations（同 api/observability.py）。

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.agents.claims import (
    ClaimAlreadyRegistered,
    ClaimNotFound,
    ClaimRegistryService,
    ClaimScope,
    ClaimStatus,
    ClaimUpgradeDenied,
)
from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    authorize_read,
    request_trace,
)
from zhiwei.evals.runs import EvalRunNotFound, EvalStateError
from zhiwei.evals.sealing import SealVerificationError
from zhiwei.identity.domain import ActorContext
from zhiwei.object_store.manifests import ArtifactVerificationError
from zhiwei.object_store.ports import ObjectStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType


class RegisterClaimRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    statement: str
    scope: ClaimScope


class UpgradeClaimRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str
    # None = 手工升级（implemented/retired）；提供时服务层独立复算密封件
    eval_run_id: UUID | None = None


class ClaimView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    statement: str
    scope: dict[str, Any]
    status: str
    bound_value: str | None
    evidence: dict[str, Any] | None


def _tenant(actor: ActorContext) -> TenantContext:
    if actor.organization_id is None or actor.workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization and workspace context required",
        )
    return TenantContext(
        organization_id=actor.organization_id, workspace_id=actor.workspace_id
    )


def _refusal(reason: str, message: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"reason": reason, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _view(record) -> ClaimView:  # type: ignore[no-untyped-def]
    return ClaimView(
        claim_id=record.claim_id,
        statement=record.statement,
        scope=record.scope.model_dump(mode="json"),
        status=record.status.value,
        bound_value=record.bound_value,
        evidence=record.evidence.model_dump(mode="json") if record.evidence else None,
    )


def create_claims_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
    object_store: ObjectStore,
) -> APIRouter:
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1/claims", tags=["claims"])

    @router.get("", response_model=list[ClaimView])
    async def list_claims(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ClaimView]:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
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
            records = await ClaimRegistryService(session, context, object_store).list()
        return [_view(record) for record in records]

    @router.get("/{claim_id}", response_model=ClaimView)
    async def get_claim(
        claim_id: str,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ClaimView:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
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
            try:
                record = await ClaimRegistryService(session, context, object_store).get(claim_id)
            except ClaimNotFound:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="claim not found"
                ) from None
        return _view(record)

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=ClaimView)
    async def register_claim(
        request: RegisterClaimRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ClaimView:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="agent.claim.register",
            resource_type="claim",
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.REQUEST,
            resource_id=context.organization_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            try:
                record = await ClaimRegistryService(session, context, object_store).register(
                    claim_id=request.claim_id,
                    statement=request.statement,
                    scope=request.scope,
                )
            except ClaimAlreadyRegistered as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="agent.claim.register",
                    resource_type="claim",
                    resource_id=context.organization_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise _refusal(
                    "claim_already_registered",
                    str(error),
                    claim_id=request.claim_id,
                ) from error
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="agent.claim.register",
                resource_type="claim",
                resource_id=context.organization_id,
                resource_version=1,
                authorization=authorization,
            )
        return _view(record)

    @router.post("/{claim_id}/upgrade", response_model=ClaimView)
    async def upgrade_claim_endpoint(
        claim_id: str,
        request: UpgradeClaimRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ClaimView:
        context = _tenant(actor)
        try:
            target = ClaimStatus(request.target)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="unknown claim status",
            ) from exc
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="agent.claim.upgrade",
            resource_type="claim",
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.REQUEST,
            resource_id=context.organization_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            try:
                record = await ClaimRegistryService(session, context, object_store).upgrade(
                    claim_id, target=target, eval_run_id=request.eval_run_id
                )
            except ClaimNotFound:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="claim not found"
                ) from None
            except EvalRunNotFound as error:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"reason": "eval_run_not_found", "message": str(error)},
                ) from error
            except ClaimUpgradeDenied as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="agent.claim.upgrade",
                    resource_type="claim",
                    resource_id=context.organization_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise _refusal(
                    "claim_upgrade_denied", str(error), claim_id=claim_id
                ) from error
            except (EvalStateError, SealVerificationError, ArtifactVerificationError) as error:
                raise _refusal("seal_verification_failed", str(error)) from error
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="agent.claim.upgrade",
                resource_type="claim",
                resource_id=context.organization_id,
                resource_version=1,
                authorization=authorization,
            )
        return _view(record)

    return router
