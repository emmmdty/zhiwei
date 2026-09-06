"""S10-T4b：Case API——S6 Case 聚合的通用 REST 面（S6 收口补齐，非 App 专属）。

事实源：specs/s6-evidence-ask.md §4/§5（用户可把 Answer/selected Evidence 创建
或附加到 Case；Case lifecycle 冻结状态机）、S6-T3 聚合（zhiwei.cases）、
handoff s6-ask-evidence-e2e-exception（解锁条件：Case 真实 API + 前端消费面）。

- POST /api/v1/runs/{run_id}/cases：从 run 创建 Case（run 必须终态——真相在
  PG canonical reduce，与 runs.py GET 同语义；未知/跨租户 run 404 防枚举，
  非终态 409）；mutation 经生产 policy 纵切（RUN_CASE_ARTIFACT ×
  manage_visible_cases，policy 先于业务事务）；
- GET /api/v1/cases、GET /api/v1/cases/{id}、GET /api/v1/runs/{run_id}/cases：
  租户隔离读面（RLS + 显式租户过滤），不复制 run/answer/evidence 正文——
  Case 按 id 引用（S6 §4）；
- 创建落 case.created 生命周期台账（0017 case_events，同事务）。
"""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    authorize_mutation,
    request_trace,
)
from zhiwei.cases.pg_repository import PgCaseRepository
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.models import CaseRow
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType

_AUDIT_ACTION = "workspace.case.create"


class CaseView(BaseModel):
    """Case 投影（0017 cases 行；id 引用，不携带 run/evidence 正文）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    run_id: UUID | None
    organization_id: UUID
    workspace_id: UUID
    title: str
    description: str
    status: str
    created_by: UUID
    answer_ids: list[str] = Field(default_factory=list)
    evidence_bundle_ids: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class CreateCaseRequest(BaseModel):
    """POST /runs/{run_id}/cases 的请求体（fail closed：未知字段拒绝）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    description: str = ""


def _tenant(actor: ActorContext) -> TenantContext:
    if actor.organization_id is None or actor.workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization and workspace context required",
        )
    return TenantContext(
        organization_id=actor.organization_id, workspace_id=actor.workspace_id
    )


def _view(row: CaseRow) -> CaseView:
    return CaseView(
        id=row.id,
        run_id=row.origin_run_id,
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        title=row.title,
        description=row.description,
        status=row.status,
        created_by=row.created_by,
        answer_ids=[str(a) for a in (row.answer_ids or [])],
        evidence_bundle_ids=[str(e) for e in (row.evidence_bundle_ids or [])],
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def create_cases_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """policy_enforcer 是生产纵切的必需注入（缺失在构造期拒绝，fail closed）。"""
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")

    router = APIRouter(prefix="/api/v1", tags=["cases"])

    @router.post("/runs/{run_id}/cases", status_code=status.HTTP_201_CREATED)
    async def create_case_from_run(
        run_id: UUID,
        request: CreateCaseRequest,
        request_scope: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
        ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> CaseView:
        del idempotency_key  # PEP 契约要求 mutation 携带（幂等落账由台账唯一键承担）
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        # policy 先于业务事务（workspaces 同款纵切）：resource 用预生成 case id，
        # version=1 是「本次创建意图」的声明（unknown 不伪装成 0 走 denied 语义）
        case_id = uuid4()
        await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION,
            resource_type="case",
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.MANAGE_VISIBLE_CASES,
            resource_id=case_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            # run 归属与终态：真相在 PG canonical reduce（与 runs.py GET 同语义；
            # RLS 保证跨租户 run 与未知 run 同为空投影 → 统一 404 防枚举）
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(run_id)
            if state.graph is None and state.status == "created":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
                )
            if not state.is_terminal:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="run is not terminal; cases can only be created from terminal runs",
                )
            repository = PgCaseRepository(session, context)
            row = await repository.create_case_from_run(
                title=request.title,
                description=request.description,
                created_by=actor.principal_id,
                origin_run_id=run_id,
            )
        return _view(row)

    @router.get("/cases", response_model=list[CaseView])
    async def list_cases(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[CaseView]:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            rows = await PgCaseRepository(session, context).list_rows()
        return [_view(row) for row in rows]

    @router.get("/runs/{run_id}/cases", response_model=list[CaseView])
    async def list_cases_for_run(
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[CaseView]:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            rows = await PgCaseRepository(session, context).list_for_run(run_id)
        return [_view(row) for row in rows]

    @router.get("/cases/{case_id}", response_model=CaseView)
    async def get_case(
        case_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> CaseView:
        context = _tenant(actor)
        async with tenant_session(sessions, context) as session:
            row = await PgCaseRepository(session, context).get_case_row(case_id)
        if row is None:
            # 跨租户与未知 case 统一 404（防枚举，workspaces 同语义）
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="case not found"
            )
        return _view(row)

    return router
