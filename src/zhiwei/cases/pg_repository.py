"""S6 Case 的 PG 仓储（0017）——S6-T3 InMemory 仓储之后的持久化补齐。

事实源：specs/s6-evidence-ask.md §4、src/zhiwei/cases/commands.py（生命周期
事件「caller 负责落账」的约定）、migrations/versions/0017_cases.py。

- ``create_case_from_run`` 是 API 创建路径：构造冻结 CREATED 状态的 Case 聚合
  （域模型负责不变量：title 非空、schema_version 正），cases 行 + case.created
  台账行同事务落库。不走 commands.create_case：其 CaseRepositoryProtocol.
  save_case 无 run 溯源位（origin_run_id 在 INSERT 时落列，事后补链需要本表
  未授予的 UPDATE）；
- 事件载荷与 commands._lifecycle_event 同形（台账契约不漂移），payload_digest
  以 canonical digest 计算，唯一键让重放幂等；
- 全部读写经调用方传入的租户事务 session（RLS 生效），仓储自身不管理事务；
  本表无 UPDATE 授权——重复 id 的 INSERT 由主键冲突拒绝（fail closed）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.cases.domain import Case, CaseStatus
from zhiwei.contracts.canonical import digest
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.models import CaseEventRow, CaseRow
from zhiwei.persistence.tenant import TenantContextRequired

_EVENT_SCHEMA_VERSION = 1


class PgCaseRepository:
    """cases/case_events 的租户仓储；一个实例绑定一个租户事务 session。"""

    def __init__(self, session: AsyncSession, context: Any) -> None:
        self._session = session
        self._context = context

    async def create_case_from_run(
        self,
        *,
        title: str,
        description: str = "",
        created_by: UUID,
        origin_run_id: UUID | None = None,
    ) -> CaseRow:
        """创建 CREATED 状态的 Case 并落 case.created 台账（同一事务）。

        workspace 作用域是本仓储的硬前提（RLS GUC 依赖它）：缺失即拒绝，
        不静默降级为 org 作用域（fail closed）。
        """
        organization_id = self._context.organization_id
        workspace_id = self._context.workspace_id
        if workspace_id is None:
            raise TenantContextRequired("workspace scope is required for case writes")
        now = utc_now()
        case = Case(
            id=uuid4(),
            organization_id=organization_id,
            workspace_id=workspace_id,
            title=title,
            description=description,
            status=CaseStatus.CREATED,
            answer_ids=(),
            evidence_bundle_ids=(),
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        event = _lifecycle_event(event_type="case.created", case=case)
        row = self._case_row(case, origin_run_id=origin_run_id)
        self._session.add(row)
        # cases 行先落（复合 FK 的 flush 排序不保证跨语句顺序，显式分两步）
        await self._session.flush()
        self._session.add(
            CaseEventRow(
                id=uuid4(),
                organization_id=organization_id,
                workspace_id=workspace_id,
                case_id=case.id,
                event_type=event["event_type"],
                from_status=event["from_status"],
                to_status=event["to_status"],
                payload=event,
                payload_digest=digest(event),
                schema_version=_EVENT_SCHEMA_VERSION,
            )
        )
        await self._session.flush()
        return row

    async def get_case_row(self, case_id: UUID) -> CaseRow | None:
        return await self._session.scalar(
            select(CaseRow).where(
                CaseRow.organization_id == self._context.organization_id,
                CaseRow.workspace_id == self._context.workspace_id,
                CaseRow.id == case_id,
            )
        )

    async def list_rows(self) -> list[CaseRow]:
        return list(
            (
                await self._session.scalars(
                    select(CaseRow)
                    .where(
                        CaseRow.organization_id == self._context.organization_id,
                        CaseRow.workspace_id == self._context.workspace_id,
                    )
                    .order_by(CaseRow.created_at)
                )
            ).all()
        )

    async def list_for_run(self, origin_run_id: UUID) -> list[CaseRow]:
        return list(
            (
                await self._session.scalars(
                    select(CaseRow)
                    .where(
                        CaseRow.organization_id == self._context.organization_id,
                        CaseRow.workspace_id == self._context.workspace_id,
                        CaseRow.origin_run_id == origin_run_id,
                    )
                    .order_by(CaseRow.created_at)
                )
            ).all()
        )

    def _case_row(self, case: Case, *, origin_run_id: UUID | None) -> CaseRow:
        return CaseRow(
            id=case.id,
            organization_id=case.organization_id,
            workspace_id=case.workspace_id,
            origin_run_id=origin_run_id,
            title=case.title,
            description=case.description,
            status=case.status.value,
            answer_ids=list(case.answer_ids),
            evidence_bundle_ids=list(case.evidence_bundle_ids),
            created_by=case.created_by,
            metadata_=dict(case.metadata),
            schema_version=case.schema_version,
        )


def _lifecycle_event(*, event_type: str, case: Case) -> dict[str, Any]:
    """与 zhiwei.cases.commands._lifecycle_event 同形（台账契约不漂移）。"""
    return {
        "event_type": event_type,
        "case_id": str(case.id),
        "from_status": case.status.value,
        "to_status": None,
        "occurred_at": utc_now().isoformat(),
    }
