"""S1-T4 修复：生产 mutation 的 PEP/audit 纵切编排（repair addendum §3.1/§3.2）。

职责边界（防屎山）：
- 单一编排：所有生产 mutation 端点经本模块求值 policy + 写审计，**禁止**把本逻辑复制进 router；
- policy 先于 mutation：gate 在业务事务开始前求值；denied → 独立审计事务（业务零写入）+
  403；allowed → 返回授权供端点在同一 tenant 事务内写 allowed 审计；
- 审计 scope 恒为 authenticated actor context；跨租户/猜 ID 不读目标租户、不构造 PolicyInput、
  不发 OPA 请求，本地拒绝 `tenant_scope_mismatch`，resource_version=0（unknown）；
- bootstrap（actor 无 org）被拒时**不写** denied 审计：目标 org 不存在，
  audit_events.organization_id NOT NULL + FK → organizations 无合法落点（schema 边界例外，
  repair addendum §3.1.9），该例外不得静默扩大；
- request_id/trace_id 由本模块 per-request 生成（uuid4().hex，request.state 缓存），
  不从 body/cookie/header 信任；
- 不实现授权语义（Rego 唯一事实），不复制 digest/outbox/tenant context（复用
  identity.audit / persistence 既有设施）。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.contracts.canonical import digest
from zhiwei.contracts.time import utc_now
from zhiwei.identity.audit import AuditRecord, append_audit, append_fail_closed_audit
from zhiwei.identity.domain import (
    ActorContext,
    NameConflictError,
    OrganizationExistsError,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    ResourceConflictError,
)
from zhiwei.persistence.repositories import IdempotencyConflict
from zhiwei.persistence.tenant import TenantContext
from zhiwei.policy.client import PolicyDecision
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.input import (
    Actor,
    PolicyInput,
    RequestContext,
    ResourceContext,
    ResourceRef,
    binding_from_membership,
)
from zhiwei.policy.roles import Action, Purpose, ResourceType, RoleScope

_REQUEST_ID_ATTR = "zhiwei_request_id"
_TRACE_ID_ATTR = "zhiwei_trace_id"

# 固定本地 reason 码（repair addendum §3.1.6）；OPA 路径 reason 原样保留不在此列
REASON_TENANT_SCOPE_MISMATCH = "tenant_scope_mismatch"
REASON_POLICY_INPUT_INVALID = "policy_input_invalid"
REASON_ENFORCEMENT_INTERNAL = "enforcement_internal_error"

# 业务拒绝（policy 放行后仍被业务状态拒绝）→ failed 审计的固定 reason 码
_FAILED_REASONS: dict[type[Exception], str] = {
    IdempotencyConflict: "idempotency_conflict",
    NameConflictError: "name_conflict",
    ResourceConflictError: "resource_conflict",
    OrganizationExistsError: "organization_exists",
    PrincipalNotFoundError: "principal_not_found",
    PrincipalDisabledError: "principal_disabled",
}


def request_trace(request: Request) -> tuple[str, str]:
    """per-request request_id/trace_id：首次调用生成并缓存于 request.state。

    同一请求内 policy input（context.trace_id）与 audit 共用同一对值；两次请求必不同；
    不读取 body/cookie/header（S1 无 tracing 传播，S2 起引入）。
    """
    request_id = getattr(request.state, _REQUEST_ID_ATTR, None)
    if request_id is None:
        request_id = uuid4().hex
        trace_id = uuid4().hex
        setattr(request.state, _REQUEST_ID_ATTR, request_id)
        setattr(request.state, _TRACE_ID_ATTR, trace_id)
        return request_id, trace_id
    return request_id, getattr(request.state, _TRACE_ID_ATTR)


def _actor_ref(principal_id: UUID) -> str:
    # S1 会话路径只产生 USER principal（repair addendum §3.1.11）
    return f"user:{principal_id}"


def _denied_payload_digest(
    request_id: str, resource_type: str, resource_id: UUID, action: str
) -> str:
    """被拒请求指纹：request_id + 目标资源 + 动作的规范化 digest（addendum §3.2）。"""
    return digest(
        {
            "request_id": request_id,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "action": action,
        }
    )


def _allowed_payload_digest(resource_type: str, resource_id: UUID, version: int) -> str:
    """业务变更指纹：resource_type + id + version（addendum §3.2）。"""
    return digest(
        {
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "resource_version": version,
        }
    )


def denied_audit_record(
    *,
    actor: ActorContext,
    organization_id: UUID,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID,
    decision: PolicyDecision,
    reason: str,
    request_id: str,
    trace_id: str,
) -> AuditRecord:
    """gate 拒绝路径的审计记录：resource_version=0（unknown，mutation 未应用）。

    decision_id/policy_revision 取自真实决策：OPA deny 带真实 metadata，本地拒绝
    （enforcer.deny）为 None——绝不伪造。
    """
    return AuditRecord(
        organization_id=organization_id,
        workspace_id=workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version=0,
        actor_ref=_actor_ref(actor.principal_id),
        effective_identity_ref=_actor_ref(actor.principal_id),
        decision_id=decision.decision_id,
        policy_revision=decision.revision,
        decision_reason=reason,
        result="denied",
        request_id=request_id,
        trace_id=trace_id,
        payload_digest=_denied_payload_digest(request_id, resource_type, resource_id, action),
    )


def allowed_audit_record(
    *,
    actor: ActorContext,
    organization_id: UUID,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID,
    resource_version: int,
    decision: PolicyDecision,
    request_id: str,
    trace_id: str,
) -> AuditRecord:
    """policy 放行的 mutation 审计记录；reason 取真实 OPA reason（不改写）。"""
    return AuditRecord(
        organization_id=organization_id,
        workspace_id=workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version=resource_version,
        actor_ref=_actor_ref(actor.principal_id),
        effective_identity_ref=_actor_ref(actor.principal_id),
        decision_id=decision.decision_id,
        policy_revision=decision.revision,
        decision_reason=decision.reason,
        result="allowed",
        request_id=request_id,
        trace_id=trace_id,
        payload_digest=_allowed_payload_digest(resource_type, resource_id, resource_version),
    )


def _build_policy_input(
    *,
    actor: ActorContext,
    organization_id: UUID,
    workspace_id: UUID | None,
    policy_type: ResourceType,
    policy_action: Action,
    resource_id: UUID,
    resource_version: int,
    purpose: Purpose,
    trace_id: str,
) -> PolicyInput:
    """ActorContext → PolicyInput；未知角色名抛 ValueError（gate 捕获 → 本地拒绝）。

    角色绑定来自已验证 memberships（ActorContext.role_bindings，resolve_context 填充），
    不信任 caller；S1 无 delegation → effective_identity=None。
    """
    role_bindings = tuple(
        binding_from_membership(
            binding.name,
            scope=RoleScope(binding.scope),
            organization_id=binding.organization_id,
            workspace_id=binding.workspace_id,
        )
        for binding in actor.role_bindings
    )
    return PolicyInput(
        actor=Actor(principal_id=actor.principal_id, kind=actor.kind, roles=role_bindings),
        effective_identity=None,
        organization_id=organization_id,
        workspace_id=workspace_id,
        resource=ResourceRef(
            type=policy_type, id=resource_id, version=str(resource_version)
        ),
        action=policy_action,
        purpose=purpose,
        classification=None,
        risk=None,
        delegation=(),
        resource_context=ResourceContext(),
        context=RequestContext(now=utc_now(), trace_id=trace_id),
    )


@dataclass(frozen=True)
class MutationAuthorization:
    """policy 放行结果：端点据此在同一 tenant 事务内写 allowed 审计。"""

    decision: PolicyDecision
    request_id: str
    trace_id: str


async def authorize_mutation(
    *,
    enforcer: PolicyEnforcer,
    sessions: async_sessionmaker[AsyncSession],
    actor: ActorContext,
    bootstrap: bool,
    organization_id: UUID,
    workspace_id: UUID | None,
    audit_action: str,
    resource_type: str,
    policy_type: ResourceType,
    policy_action: Action,
    resource_id: UUID,
    resource_version: int,
    purpose: Purpose,
    request_id: str,
    trace_id: str,
) -> MutationAuthorization:
    """对 mutation 做 PEP 求值；denied → 独立事务审计 + 403，业务事务不得开始。

    - 租户边界：非 bootstrap 时目标 org ≠ actor org → 本地拒绝（不读目标租户、不发 OPA）；
    - 审计 scope = authenticated actor context；bootstrap 被拒不写审计（§3.1.9 例外）；
    - OPA 拒绝/不可达/输入非法 → 403 detail `policy denied`；
      租户不匹配 → 403 detail `outside tenant scope`（与既有冻结 detail 一致）。
    """
    if not bootstrap and actor.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="organization context required"
        )
    if not bootstrap and organization_id != actor.organization_id:
        decision = enforcer.deny(REASON_TENANT_SCOPE_MISMATCH)
        await _write_denied_audit(
            sessions=sessions,
            actor=actor,
            organization_id=actor.organization_id or organization_id,
            workspace_id=actor.workspace_id,
            action=audit_action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=decision,
            reason=REASON_TENANT_SCOPE_MISMATCH,
            request_id=request_id,
            trace_id=trace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="outside tenant scope"
        )
    try:
        policy_input = _build_policy_input(
            actor=actor,
            organization_id=organization_id,
            workspace_id=workspace_id,
            policy_type=policy_type,
            policy_action=policy_action,
            resource_id=resource_id,
            resource_version=resource_version,
            purpose=purpose,
            trace_id=trace_id,
        )
        decision = await enforcer.authorize(policy_input)
    except ValueError:
        decision = enforcer.deny(REASON_POLICY_INPUT_INVALID)
    if not decision.allow:
        await _write_denied_audit(
            sessions=sessions,
            actor=actor,
            organization_id=actor.organization_id or organization_id,
            workspace_id=actor.workspace_id,
            action=audit_action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=decision,
            reason=decision.reason,
            request_id=request_id,
            trace_id=trace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="policy denied"
        )
    return MutationAuthorization(decision=decision, request_id=request_id, trace_id=trace_id)


async def _write_denied_audit(
    *,
    sessions: async_sessionmaker[AsyncSession],
    actor: ActorContext,
    organization_id: UUID,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID,
    decision: PolicyDecision,
    reason: str,
    request_id: str,
    trace_id: str,
) -> None:
    """denied 审计（独立事务）。bootstrap 被拒（actor 无 org）→ 跳过（schema 边界例外）。

    审计写失败 → 异常上抛（500）：mutation 绝不执行（repair addendum §3.1.7）。
    """
    if actor.organization_id is None:
        return
    record = denied_audit_record(
        actor=actor,
        organization_id=organization_id,
        workspace_id=workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision,
        reason=reason,
        request_id=request_id,
        trace_id=trace_id,
    )
    await append_fail_closed_audit(
        sessions,
        TenantContext(organization_id=organization_id, workspace_id=workspace_id),
        record,
    )


async def append_failed_mutation_audit(
    sessions: async_sessionmaker[AsyncSession],
    *,
    actor: ActorContext,
    organization_id: UUID,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID,
    error: Exception,
    request_id: str,
    trace_id: str,
) -> None:
    """policy 放行后被业务状态拒绝 → 独立事务 failed 审计（NULL metadata，固定 reason 码）。

    幂等重放（created=False）不经过本函数——端点只在业务拒绝时调用。
    """
    reason = _FAILED_REASONS.get(type(error), "business_rejection")
    record = AuditRecord(
        organization_id=organization_id,
        workspace_id=workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version=0,
        actor_ref=_actor_ref(actor.principal_id),
        effective_identity_ref=_actor_ref(actor.principal_id),
        decision_id=None,
        policy_revision=None,
        decision_reason=reason,
        result="failed",
        request_id=request_id,
        trace_id=trace_id,
        payload_digest=_denied_payload_digest(
            request_id, resource_type, resource_id, action
        ),
    )
    await append_fail_closed_audit(
        sessions,
        TenantContext(organization_id=organization_id, workspace_id=workspace_id),
        record,
    )


async def append_allowed_audit(
    session: AsyncSession,
    *,
    actor: ActorContext,
    organization_id: UUID,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID,
    resource_version: int,
    authorization: MutationAuthorization,
) -> None:
    """allowed mutation 审计：调用方当前 tenant 事务内追加（同提交/同回滚）。"""
    await append_audit(
        session,
        TenantContext(organization_id=organization_id, workspace_id=workspace_id),
        allowed_audit_record(
            actor=actor,
            organization_id=organization_id,
            workspace_id=workspace_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_version=resource_version,
            decision=authorization.decision,
            request_id=authorization.request_id,
            trace_id=authorization.trace_id,
        ),
    )
