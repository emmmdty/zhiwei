"""S10-T4b：Run Evidence 投影 API——canonical 事件的 claim/verify/answer 读面
（S6 收口补齐，非 App 专属）。

事实源：specs/s6-evidence-ask.md §5（点击 Claim 打开 source locator/canonical
value/verify result）、specs/s6-evidence-ask.md §6（Runtime Verify handler 的
结果/失败以 canonical Task event 提交）、handoff s6-ask-evidence-e2e-exception。

P2b 重接（F-R6-01/F-R3-03，ADR-006 失权三通道接入生产读路径）：

- 端点 PEP：run_case_artifact.read（与 runs 读同 cell，前置；denied → 403
  fail closed）；
- 通道判定经 PEP：knowledge_source.read_provenance 允许 → AUDITOR 通道
  （全可见，reason=audit_channel）；否则 USER 通道——逐 ref 经
  resolve_evidence_views（ADR-006 公共入口），current_acl 按 ref.source_id 查
  PG source_objects 当前 ACL；占位 {ref_id, status, reason} 不携带任何内容
  字段，条目不消失（not silent removal）；
- 未解析 ref（PG 无对应 source object / 载荷不可解析）→ fail closed 占位
  （acl_unknown）——历史 run 的残引不可复算即不可见；
- EVAL_RECOMPUTE 通道无生产入口（eval 复算仅 offline sealed 模式内成立，
  S6 评审确认）——不在 API 面承载（台账登记）。

设计边界（不发明数据）：本端点是 S2 canonical event 真相的只读投影——只返回
reduced RunState 已携带的 claim/verify/answer 形态；可见性维度经 ADR-006
当前 ACL 复检后呈现（占位或元数据），不添加 canonical 之外的答案字段。

租户纪律与 runs.py GET 相同：RLS + 显式租户过滤，跨租户/未知 run 统一 404
（防枚举）。无 mutation（读不写审计）。
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import authorize_read, request_trace
from zhiwei.evidence.access import (
    REVOKED_PLACEHOLDER_STATUS,
    EvidencePrincipal,
    EvidenceView,
    PrincipalKind,
    resolve_evidence_views,
)
from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.refs import EvidenceRef
from zhiwei.identity.domain import ActorContext
from zhiwei.knowledge.acl import ACLContext
from zhiwei.knowledge.contracts import ACLSnapshot
from zhiwei.knowledge.pg_ledger import PgSourceLedger
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, ResourceType
from zhiwei.runtime.reducer import RunState

logger = logging.getLogger(__name__)

_CLAIM_TYPES = ("Fact", "Quote", "Inference", "Recommendation")

_ACLLookup = Callable[[EvidenceRef], "ACLSnapshot | None"]

_EVIDENCE_REF_ADAPTER: TypeAdapter[EvidenceRef] = TypeAdapter(EvidenceRef)


class ClaimEvidenceView(BaseModel):
    """一条 claim 的投影：claim_ref 必有；其余字段只在 canonical 载荷携带时出现。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_ref: str
    claim_type: str | None = None
    verified: bool | None = None
    quote_text: str | None = None
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    canonical_value: dict[str, Any] | None = None


class ConflictEvidenceView(BaseModel):
    """ADR-005 冲突记录投影（并列保留双方取值，不仲裁）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    values: dict[str, Any]
    evidence_refs: list[str] = Field(default_factory=list)
    detected_at: str | None = None


class RunEvidenceView(BaseModel):
    """一个 run 的 evidence/claim 投影（canonical 事件真相 + ADR-006 可见性）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    run_status: str
    answer_status: str | None = None
    answer: dict[str, Any] = Field(default_factory=dict)
    claims: list[ClaimEvidenceView] = Field(default_factory=list)
    verified_claims: list[str] = Field(default_factory=list)
    failed_claims: list[str] = Field(default_factory=list)
    verification: dict[str, Any] | None = None
    unknowns: list[str] = Field(default_factory=list)
    clarification: dict[str, Any] | None = None
    findings: list[Any] = Field(default_factory=list)
    conflicts: list[ConflictEvidenceView] = Field(default_factory=list)


def _string_list(raw: Any) -> list[str]:
    """canonical 里的列表字段 defensively 收敛为字符串列表（异形丢弃，不猜）。"""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _claim_view(raw: Any, verified: set[str], failed: set[str]) -> ClaimEvidenceView | None:
    """单条 claim → 投影；结构化载荷逐字透传，异形载荷丢弃（fail closed）。"""
    if isinstance(raw, str):
        if raw in verified:
            is_verified: bool | None = True
        elif raw in failed:
            is_verified = False
        else:
            is_verified = None
        return ClaimEvidenceView(claim_ref=raw, verified=is_verified)
    if isinstance(raw, dict):
        claim_ref = raw.get("claim_id") or raw.get("quote_text") or raw.get("text")
        if not isinstance(claim_ref, str) or not claim_ref:
            return None
        claim_type = raw.get("claim_type")
        if claim_type not in _CLAIM_TYPES:
            claim_type = None
        if claim_ref in verified:
            is_verified = True
        elif claim_ref in failed:
            is_verified = False
        else:
            is_verified = None
        evidence_refs = [
            ref for ref in raw.get("evidence_refs", []) if isinstance(ref, dict)
        ]
        if not evidence_refs:
            evidence_refs = [
                ref for ref in raw.get("supporting_inputs", []) if isinstance(ref, dict)
            ]
        canonical_value = raw.get("canonical_value")
        if not isinstance(canonical_value, dict):
            canonical_value = None
        quote_text = raw.get("quote_text")
        if not isinstance(quote_text, str):
            quote_text = None
        return ClaimEvidenceView(
            claim_ref=claim_ref,
            claim_type=claim_type,
            verified=is_verified,
            quote_text=quote_text,
            evidence_refs=evidence_refs,
            canonical_value=canonical_value,
        )
    return None


def _placeholder(ref_id: UUID | None, reason: str) -> dict[str, Any]:
    """fail closed 占位（{ref_id, status, reason}；任何内容字段不得出现）。"""
    return EvidenceView(
        ref_id=ref_id or UUID(int=0),
        status=REVOKED_PLACEHOLDER_STATUS,
        reason=reason,
    ).as_dict()


def _resolve_claim_refs(
    claims: list[ClaimEvidenceView],
    principal: EvidencePrincipal,
    acl_lookup: _ACLLookup | None,
) -> None:
    """逐 claim 解析 evidence_refs → ADR-006 可见性视图（原地替换）。

    acl_lookup=None = AUDITOR 通道（不查当前 ACL，域层 audit_channel 分支）；
    不可解析载荷 → fail closed 占位（acl_unknown），不静默移除。
    """
    parsed: list[tuple[ClaimEvidenceView, int, UUID | None, EvidenceRef | None]] = []
    for claim in claims:
        for index, raw in enumerate(claim.evidence_refs):
            ref_id: UUID | None = None
            ref: EvidenceRef | None = None
            if isinstance(raw, dict):
                raw_ref_id = raw.get("ref_id")
                try:
                    ref_id = UUID(str(raw_ref_id)) if raw_ref_id else None
                except ValueError:
                    ref_id = None
                try:
                    ref = _EVIDENCE_REF_ADAPTER.validate_python(raw)
                except Exception:
                    ref = None
            parsed.append((claim, index, ref_id, ref))

    for claim, index, ref_id, ref in parsed:
        if ref is None:
            claim.evidence_refs[index] = _placeholder(ref_id, "acl_unknown")

    resolvable = [(claim, index, ref) for claim, index, _, ref in parsed if ref is not None]
    if not resolvable:
        return
    bundle = EvidenceBundle(
        bundle_id=uuid4(),
        answer_id=uuid4(),
        evidence_refs=tuple(ref for _, _, ref in resolvable),
        claims=(),
        created_at=datetime.now(tz=UTC),
    )
    lookup: _ACLLookup = acl_lookup or (lambda _ref: None)
    views = resolve_evidence_views(bundle, principal, current_acl=lookup)
    view_by_ref = {view.ref_id: view for view in views}
    for claim, index, ref in resolvable:
        view = view_by_ref.get(ref.ref_id)
        if view is None:  # pragma: no cover - 域层对每个输入 ref 产出视图
            view = EvidenceView(
                ref_id=ref.ref_id,
                status=REVOKED_PLACEHOLDER_STATUS,
                reason="acl_unknown",
            )
        claim.evidence_refs[index] = view.as_dict()


def _collect_source_ids(state: RunState) -> list[UUID]:
    """canonical claims 里全部 evidence ref 的 source_id（当前 ACL 批量查询用）。

    evidence_refs 与 supporting_inputs 都收（_claim_view 把 supporting_inputs
    提升为 evidence_refs——Inference/Recommendation 的域形状挂在那里）。"""
    ids: list[UUID] = []
    canonical = state.canonical
    raw_claims = canonical.get("claims")
    answer = canonical.get("answer")
    if not isinstance(raw_claims, list) and isinstance(answer, dict):
        raw_claims = answer.get("claims")
    if not isinstance(raw_claims, list):
        return ids
    for raw in raw_claims:
        if not isinstance(raw, dict):
            continue
        for key in ("evidence_refs", "supporting_inputs"):
            for ref in raw.get(key, []):
                if isinstance(ref, dict) and isinstance(
                    ref.get("source_id"), (str, UUID)
                ):
                    try:
                        ids.append(UUID(str(ref["source_id"])))
                    except ValueError:
                        continue
    return ids


def _evidence_view(
    state: RunState,
    principal: EvidencePrincipal,
    acl_lookup: _ACLLookup | None,
) -> RunEvidenceView:
    canonical = state.canonical
    verified = set(_string_list(canonical.get("verified_claims")))
    failed = set(_string_list(canonical.get("failed_claims")))
    raw_answer = canonical.get("answer")
    answer: dict[str, Any] = raw_answer if isinstance(raw_answer, dict) else {}

    raw_claims = canonical.get("claims")
    if not isinstance(raw_claims, list):
        raw_claims = answer.get("claims")
    claims: list[ClaimEvidenceView] = []
    if isinstance(raw_claims, list):
        for raw in raw_claims:
            view = _claim_view(raw, verified, failed)
            if view is not None:
                claims.append(view)
    _resolve_claim_refs(claims, principal, acl_lookup)

    verification = canonical.get("verification")
    clarification = canonical.get("clarification")
    raw_findings = canonical.get("findings")
    conflicts = [
        ConflictEvidenceView(
            field=record.field,
            values=dict(record.values),
            evidence_refs=list(record.evidence_refs),
            detected_at=record.detected_at.isoformat(),
        )
        for record in state.conflicts
    ]
    answer_status = answer.get("status")
    return RunEvidenceView(
        run_id=state.run_id,
        run_status=state.status,
        answer_status=answer_status if isinstance(answer_status, str) else None,
        answer=answer,
        claims=claims,
        verified_claims=_string_list(canonical.get("verified_claims")),
        failed_claims=_string_list(canonical.get("failed_claims")),
        verification=verification if isinstance(verification, dict) else None,
        unknowns=_string_list(canonical.get("unknowns")),
        clarification=clarification if isinstance(clarification, dict) else None,
        findings=raw_findings if isinstance(raw_findings, list) else [],
        conflicts=conflicts,
    )


async def _principal_groups(
    session: AsyncSession,
    organization_id: UUID,
    workspace_id: UUID,
    principal_id: UUID,
) -> frozenset[str]:
    """principal 所属 workspace 分组名（group_members/groups，租户作用域）。"""
    from sqlalchemy import select

    from zhiwei.persistence.models import Group, GroupMember

    rows = await session.execute(
        select(Group.name).join(
            GroupMember, GroupMember.group_id == Group.id
        ).where(
            Group.organization_id == organization_id,
            Group.workspace_id == workspace_id,
            GroupMember.principal_id == principal_id,
        )
    )
    return frozenset(rows.scalars().all())


def create_evidence_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """Evidence 投影 router（P2b：PEP + ADR-006 失权通道）。

    policy_enforcer 必须由组合根提供（fail closed，缺失在构造期拒绝）。
    """
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1/runs", tags=["evidence"])

    @router.get("/{run_id}/evidence", response_model=RunEvidenceView)
    async def get_run_evidence(
        request_scope: Request,
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> RunEvidenceView:
        organization_id = actor.organization_id
        workspace_id = actor.workspace_id
        if organization_id is None or workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization and workspace context required",
            )
        context = TenantContext(
            organization_id=organization_id, workspace_id=workspace_id
        )
        _, trace_id = request_trace(request_scope)
        # 端点 PEP：与 runs 读同 cell（denied/租户不匹配/OPA 不可达 → 403）
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.READ,
            resource_id=run_id,
            trace_id=trace_id,
        )
        # 通道判定经 PEP（ADR-006）：read_provenance cell = auditor 角色；
        # deny（含本地拒绝）→ USER 通道
        auditor = False
        try:
            await authorize_read(
                enforcer=policy_enforcer,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                policy_type=ResourceType.KNOWLEDGE_SOURCE,
                policy_action=Action.READ_PROVENANCE,
                resource_id=run_id,
                trace_id=trace_id,
            )
            auditor = True
        except HTTPException:
            auditor = False

        acl_lookup = None
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(run_id)
            if state.graph is None and state.status == "created":
                # 与 runs.py GET 同语义：跨租户/未知 run 统一 404 防枚举
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
                )
            if not auditor:
                # USER 通道：当前 ACL 权威按 ref.source_id 批量查询（撤权即失效）
                ledger = PgSourceLedger(session, context)
                acl_by_source = await ledger.get_current_acls(_collect_source_ids(state))
                acl_lookup = lambda ref: acl_by_source.get(ref.source_id)  # noqa: E731
            principal = EvidencePrincipal(
                kind=PrincipalKind.AUDITOR if auditor else PrincipalKind.USER,
                acl_context=ACLContext(
                    principal_id=actor.principal_id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    # principal 所属 workspace 分组（ADR-006 group 授权形态的
                    # 身份侧事实，group_members 身份解析）
                    allowed_groups=await _principal_groups(
                        session, organization_id, workspace_id, actor.principal_id
                    ),
                ),
            )
            view = _evidence_view(state, principal, acl_lookup)
        return view

    return router
