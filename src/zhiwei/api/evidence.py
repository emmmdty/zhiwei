"""S10-T4b：Run Evidence 投影 API——canonical 事件的 claim/verify/answer 读面
（S6 收口补齐，非 App 专属）。

事实源：specs/s6-evidence-ask.md §5（点击 Claim 打开 source locator/canonical
value/verify result）、specs/s6-evidence-ask.md §6（Runtime Verify handler 的
结果/失败以 canonical Task event 提交）、handoff s6-ask-evidence-e2e-exception。

设计边界（不发明数据）：本端点是 S2 canonical event 真相的只读投影——只返回
reduced RunState 已携带的 claim/verify/answer 形态：

- claims：analyze/verify/synthesize 任务输出里的 claim 记录（claim id/text）。
  canonical 载荷带结构化 claim（claim_type/evidence_refs/canonical_value）时逐
  字透传（source locator 与 canonical value digest 只在 canonical 事件真的携带
  时出现）；字符串形态的 claim 记录如实按 opaque claim_ref 投影；
- verified：只按 verify 节点输出（verified_claims/failed_claims）解析绑定；
  无法解析（如 Ask 契约场景的通用标记）时如实为 null，不虚构 per-claim 绑定；
- answer/unknowns/clarification/verification/conflicts/findings：canonical 状态
  逐字投影——abstain/partial/clarify 的诚实呈现由消费方按原样渲染。

租户纪律与 runs.py GET 相同：RLS + 显式租户过滤，跨租户/未知 run 统一 404
（防枚举）。无 mutation，不接 policy gate（读路径 PEP 即租户作用域校验）。
"""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.reducer import RunState

_CLAIM_TYPES = ("Fact", "Quote", "Inference", "Recommendation")


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
    """一个 run 的 evidence/claim 投影（canonical 事件真相，无进程内合成）。"""

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


def _evidence_view(state: RunState) -> RunEvidenceView:
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


def create_evidence_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
) -> APIRouter:
    """Evidence 投影 router（读面；构造期无外部依赖可拒绝）。"""
    router = APIRouter(prefix="/api/v1/runs", tags=["evidence"])

    @router.get("/{run_id}/evidence", response_model=RunEvidenceView)
    async def get_run_evidence(
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> RunEvidenceView:
        if actor.organization_id is None or actor.workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization and workspace context required",
            )
        context = TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(run_id)
            if state.graph is None and state.status == "created":
                # 与 runs.py GET 同语义：跨租户/未知 run 统一 404 防枚举
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
                )
        return _evidence_view(state)

    return router
