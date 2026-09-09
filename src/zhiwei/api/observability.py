"""S9-T6：Observability API——租户成本汇总与 failure taxonomy 清单（specs/s9 §6）。

工厂模式与 api/runs.py 同型：组合期注入 actor 依赖 / session factory / policy
enforcer，端点内不做任何授权语义决策（PEP 判定统一走 policy_gate）。

读路径纪律（ADR-012 决策 4）：成本数据是租户治理证据，读 cell 复用 S1 冻结矩阵的
`org.read_audit`（auditor）——不为 observability 新开 policy cell（新增 cell 需要
Rego 侧同步，属设计方决策）。无 workspace 上下文 / 跨租户 / deny 一律 fail closed。
"""

# 注意：本模块【不用】from __future__ import annotations——endpoint 签名里的
# `Annotated[ActorContext, Depends(actor_dependency)]` 引用工厂闭包变量，必须
# 在 def 期立即求值才能被 FastAPI 解析（PEP 563 字符串化会让闭包名在 FastAPI
# 的注解求值里不可见 → 依赖退化为 query 参数）。api/ 下全部 router 模块同此约定。

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import authorize_read, request_trace
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.costs import CostLedgerRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, ResourceType
from zhiwei.telemetry.failures import FailureCode


class CostReservationView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: str
    run_id: str
    amount_usd: str
    price_source: str
    price_confidence: str
    created_at: str


class CostReconciliationView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: str
    reserved_usd: str
    actual_usd: str
    variance_usd: str
    retry_cost_usd: str
    child_run_cost_usd: str
    tool_external_cost_usd: str
    created_at: str


class CostSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reservations: list[CostReservationView]
    reconciliations: list[CostReconciliationView]


class FailureCodeView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str


class FailureTaxonomy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    codes: list[FailureCodeView]


def _tenant(actor: ActorContext) -> TenantContext:
    """actor → 显式租户上下文（fail closed：org/workspace 缺一即拒绝）。"""
    if actor.organization_id is None or actor.workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization and workspace context required",
        )
    return TenantContext(
        organization_id=actor.organization_id, workspace_id=actor.workspace_id
    )


def create_observability_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/observability", tags=["observability"])

    @router.get("/costs", response_model=CostSummary)
    async def get_cost_summary(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> CostSummary:
        context = _tenant(actor)
        # 读路径 PEP 前置：deny/租户不匹配/OPA 不可达 → 403，不触碰数据。
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.ORG,
            policy_action=Action.READ_AUDIT,
            resource_id=context.organization_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repository = CostLedgerRepository(session, context)
            reservation_rows = await repository.list_reservation_rows()
            reconciliation_rows = await repository.list_reconciliation_rows()
        return CostSummary(
            reservations=[
                CostReservationView(
                    reservation_id=str(row.id),
                    run_id=str(row.run_id),
                    amount_usd=str(row.amount_usd),
                    price_source=row.price_source,
                    price_confidence=row.price_confidence,
                    created_at=row.created_at.isoformat(),
                )
                for row in reservation_rows
            ],
            reconciliations=[
                CostReconciliationView(
                    reservation_id=str(row.reservation_id),
                    reserved_usd=str(row.reserved_usd),
                    actual_usd=str(row.actual_usd),
                    variance_usd=str(row.variance_usd),
                    retry_cost_usd=str(row.retry_cost_usd),
                    child_run_cost_usd=str(row.child_run_cost_usd),
                    tool_external_cost_usd=str(row.tool_external_cost_usd),
                    created_at=row.created_at.isoformat(),
                )
                for row in reconciliation_rows
            ],
        )

    @router.get("/failures", response_model=FailureTaxonomy)
    async def get_failure_taxonomy(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> FailureTaxonomy:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.ORG,
            policy_action=Action.READ_AUDIT,
            resource_id=context.organization_id,
            trace_id=trace_id,
        )
        # 静态 machine code 清单：dashboard 的状态词汇是封闭契约，不从日志猜。
        return FailureTaxonomy(
            codes=[
                FailureCodeView(code=code.value)
                for code in sorted(FailureCode, key=lambda item: item.value)
            ]
        )

    return router
