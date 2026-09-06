"""S8 Discover workbench 的 PG 仓储（0018）——内存态 manager 之后的持久化补齐。

事实源：specs/s8-discover-actions.md §4/§6、migrations/versions/0018_discover.py、
src/zhiwei/cases/pg_repository.py（0017 同款仓储纪律）。

- ``ingest_hypothesis`` 是 pipeline ingest seam（Signal→RiskHypothesis 的落点；
  契约测试经它注入 pipeline 产物——D0–D6 数据面断言属 eval suite，不在此层）；
- triage 迁移：status/owner 列级 UPDATE + discover_hypothesis_events 台账
  （同事务）；detector output 内容列由迁移守护触发器与缺失的 UPDATE 授权双重
  不可变；
- case/action/resolution 全部以 id 引用链接（JSONB id 列表，0017 同款）；
  同 hypothesis 的 case 唯一性、同 case 的 action input digest 唯一性由数据面
  unique 索引兜底（fail closed）；
- 全部读写经调用方传入的租户事务 session（RLS 生效），仓储自身不管理事务。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.canonical import digest
from zhiwei.contracts.time import utc_now
from zhiwei.discover.hypotheses import HypothesisStatus, RiskHypothesis
from zhiwei.discover.signals import SignalSeverity
from zhiwei.persistence.models import (
    DiscoverActionRow,
    DiscoverCaseRow,
    DiscoverHypothesisEventRow,
    DiscoverHypothesisRow,
    DiscoverResolutionRow,
)
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired

_EVENT_SCHEMA_VERSION = 1
_ROW_SCHEMA_VERSION = 1

# 人工 triage 状态机（fail closed：不在表内的迁移由 API 层拒绝后落账）。
# in_triage → accepted/dismissed 是决策；dismissed → in_triage 是 reopen。
TRIAGE_TRANSITIONS: dict[HypothesisStatus, frozenset[HypothesisStatus]] = {
    HypothesisStatus.READY_FOR_TRIAGE: frozenset({HypothesisStatus.IN_TRIAGE}),
    HypothesisStatus.IN_TRIAGE: frozenset(
        {HypothesisStatus.ACCEPTED, HypothesisStatus.DISMISSED}
    ),
    HypothesisStatus.DISMISSED: frozenset({HypothesisStatus.IN_TRIAGE}),
}


class PgDiscoverRepository:
    """discover 五表的租户仓储；一个实例绑定一个租户事务 session。"""

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        self._session = session
        self._context = context

    # ── hypotheses ────────────────────────────────────────────────────

    async def ingest_hypothesis(
        self,
        hypothesis: RiskHypothesis,
        *,
        severity: SignalSeverity,
        created_by: UUID,
        dedup_key: str = "",
    ) -> DiscoverHypothesisRow:
        """pipeline ingest seam：RiskHypothesis → workbench 投影行。

        created_by 是 ingest 主体（后台 run 走 program service identity 的
        principal；契约测试用 seed actor）——不默认伪装。
        """
        workspace_id = self._require_workspace()
        row = DiscoverHypothesisRow(
            id=hypothesis.id,
            organization_id=self._context.organization_id,
            workspace_id=workspace_id,
            signal_id=hypothesis.signal_id,
            program_version_id=hypothesis.program_version_id,
            detector_pack_id=hypothesis.detector_pack_id,
            detector_pack_version=hypothesis.detector_pack_version,
            kind=hypothesis.kind.value,
            title=hypothesis.title,
            description=hypothesis.description,
            status=hypothesis.status.value,
            owner=hypothesis.owner,
            severity=severity.value,
            score=hypothesis.score,
            affected_entities=list(hypothesis.affected_entities),
            evidence_tags=[tag.model_dump(mode="json") for tag in hypothesis.evidence_tags],
            suggested_validation_actions=list(hypothesis.suggested_validation_actions),
            source_watermarks=[
                watermark.model_dump(mode="json") for watermark in hypothesis.source_watermarks
            ],
            proposed_probes=[probe.model_dump(mode="json") for probe in hypothesis.proposed_probes],
            falsification_results=[
                result.model_dump(mode="json") for result in hypothesis.falsification_results
            ],
            dedup_key=dedup_key,
            created_by=created_by,
            schema_version=_ROW_SCHEMA_VERSION,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_hypothesis_row(self, hypothesis_id: UUID) -> DiscoverHypothesisRow | None:
        return await self._session.scalar(
            select(DiscoverHypothesisRow).where(
                DiscoverHypothesisRow.organization_id == self._context.organization_id,
                DiscoverHypothesisRow.workspace_id == self._context.workspace_id,
                DiscoverHypothesisRow.id == hypothesis_id,
            )
        )

    async def list_hypothesis_rows(self) -> list[DiscoverHypothesisRow]:
        return list(
            (
                await self._session.scalars(
                    select(DiscoverHypothesisRow)
                    .where(
                        DiscoverHypothesisRow.organization_id
                        == self._context.organization_id,
                        DiscoverHypothesisRow.workspace_id == self._context.workspace_id,
                    )
                    .order_by(DiscoverHypothesisRow.created_at)
                )
            ).all()
        )

    async def apply_triage(
        self,
        row: DiscoverHypothesisRow,
        *,
        to_status: HypothesisStatus,
        owner: str,
        actor_ref: str,
    ) -> DiscoverHypothesisRow:
        """人工 triage 迁移：status/owner 原地迁移 + 台账行（同一事务）。

        非法迁移在本层再次拒绝（API 与仓储双层 fail closed）。
        """
        current = HypothesisStatus(row.status)
        allowed = TRIAGE_TRANSITIONS.get(current, frozenset())
        if to_status not in allowed:
            raise ValueError(
                f"illegal triage transition {current.value} -> {to_status.value}"
            )
        event_payload = {
            "event_type": "discover.hypothesis.triage",
            "hypothesis_id": str(row.id),
            "from_status": row.status,
            "to_status": to_status.value,
            "actor_ref": actor_ref,
            "occurred_at": utc_now().isoformat(),
        }
        row.status = to_status.value
        if owner:
            row.owner = owner
        row.updated_at = utc_now()
        self._session.add(
            DiscoverHypothesisEventRow(
                id=uuid4(),
                organization_id=self._context.organization_id,
                workspace_id=self._require_workspace(),
                hypothesis_id=row.id,
                action="triage",
                from_status=event_payload["from_status"],
                to_status=to_status.value,
                actor_ref=actor_ref,
                payload=event_payload,
                payload_digest=digest(event_payload),
                schema_version=_EVENT_SCHEMA_VERSION,
            )
        )
        await self._session.flush()
        return row

    # ── cases ─────────────────────────────────────────────────────────

    async def create_case_for_hypothesis(
        self,
        hypothesis_row: DiscoverHypothesisRow,
        *,
        created_by: UUID,
        title: str | None = None,
        description: str | None = None,
    ) -> DiscoverCaseRow:
        """从 hypothesis 创建 DiscoverCase（默认标题取 hypothesis title）。"""
        workspace_id = self._require_workspace()
        existing = await self._session.scalar(
            select(DiscoverCaseRow).where(
                DiscoverCaseRow.organization_id == self._context.organization_id,
                DiscoverCaseRow.workspace_id == workspace_id,
                DiscoverCaseRow.hypothesis_id == hypothesis_row.id,
            )
        )
        if existing is not None:
            # 刷新/重试不复制：唯一索引兜底之外的应用层拒绝（幂等重放语义）
            raise ValueError("case already exists for hypothesis")
        row = DiscoverCaseRow(
            id=uuid4(),
            organization_id=self._context.organization_id,
            workspace_id=workspace_id,
            hypothesis_id=hypothesis_row.id,
            title=title if title is not None else hypothesis_row.title,
            description=(
                description if description is not None else hypothesis_row.description
            ),
            status="open",
            severity=hypothesis_row.severity,
            owner=hypothesis_row.owner,
            dedup_key=hypothesis_row.dedup_key,
            hypothesis_ids=[str(hypothesis_row.id)],
            action_request_ids=[],
            resolution_ids=[],
            created_by=created_by,
            schema_version=_ROW_SCHEMA_VERSION,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_case_row(self, case_id: UUID) -> DiscoverCaseRow | None:
        return await self._session.scalar(
            select(DiscoverCaseRow).where(
                DiscoverCaseRow.organization_id == self._context.organization_id,
                DiscoverCaseRow.workspace_id == self._context.workspace_id,
                DiscoverCaseRow.id == case_id,
            )
        )

    async def list_action_rows_for_case(self, case_id: UUID) -> list[DiscoverActionRow]:
        return list(
            (
                await self._session.scalars(
                    select(DiscoverActionRow)
                    .where(
                        DiscoverActionRow.organization_id == self._context.organization_id,
                        DiscoverActionRow.workspace_id == self._context.workspace_id,
                        DiscoverActionRow.case_id == case_id,
                    )
                    .order_by(DiscoverActionRow.created_at)
                )
            ).all()
        )

    async def list_resolution_rows_for_case(self, case_id: UUID) -> list[DiscoverResolutionRow]:
        return list(
            (
                await self._session.scalars(
                    select(DiscoverResolutionRow)
                    .where(
                        DiscoverResolutionRow.organization_id == self._context.organization_id,
                        DiscoverResolutionRow.workspace_id == self._context.workspace_id,
                        DiscoverResolutionRow.case_id == case_id,
                    )
                    .order_by(DiscoverResolutionRow.created_at)
                )
            ).all()
        )

    # ── actions ───────────────────────────────────────────────────────

    async def create_action(
        self,
        *,
        action_id: UUID,
        hypothesis_id: UUID,
        case_id: UUID,
        action_type: str,
        tool_name: str,
        parameters: dict[str, Any],
        rationale: str,
        requested_by: UUID,
        s2_decision_id: UUID,
        input_digest: str,
    ) -> DiscoverActionRow:
        """pending_approval 的 action 行 + case 关联列表更新（同一事务）。

        行 id = ActionRequest id（域与投影同标识——approve 流程按本 id 消费
        S2 决定）；内容 digest 唯一索引兜底重复提交（应用层先行显式拒绝，
        见 API 层）。
        """
        workspace_id = self._require_workspace()
        case_row = await self.get_case_row(case_id)
        if case_row is None:
            raise ValueError(f"DiscoverCase {case_id} not found")
        existing_digests = await self._session.scalars(
            select(DiscoverActionRow.input_digest).where(
                DiscoverActionRow.organization_id == self._context.organization_id,
                DiscoverActionRow.workspace_id == workspace_id,
                DiscoverActionRow.case_id == case_id,
            )
        )
        if input_digest in set(existing_digests.all()):
            raise ValueError("duplicate action submission")
        row = DiscoverActionRow(
            id=action_id,
            organization_id=self._context.organization_id,
            workspace_id=workspace_id,
            hypothesis_id=hypothesis_id,
            case_id=case_id,
            action_type=action_type,
            tool_name=tool_name,
            parameters=parameters,
            rationale=rationale,
            requested_by=requested_by,
            status="pending_approval",
            s2_decision_id=s2_decision_id,
            approved_by=None,
            approval_timestamp=None,
            input_digest=input_digest,
            schema_version=_ROW_SCHEMA_VERSION,
        )
        self._session.add(row)
        case_row.action_request_ids = [*case_row.action_request_ids, str(row.id)]
        case_row.updated_at = utc_now()
        await self._session.flush()
        return row

    async def get_action_row(self, action_id: UUID) -> DiscoverActionRow | None:
        return await self._session.scalar(
            select(DiscoverActionRow).where(
                DiscoverActionRow.organization_id == self._context.organization_id,
                DiscoverActionRow.workspace_id == self._context.workspace_id,
                DiscoverActionRow.id == action_id,
            )
        )

    async def approve_action(
        self, row: DiscoverActionRow, *, approved_by: UUID
    ) -> DiscoverActionRow:
        """approved 迁移：status/approved_by/approval_timestamp 列级 UPDATE。

        调用方必须已完成 S2 SoD 决策（ApprovalRequestManager.approve）——
        本方法只消费已批准的决定（discover 不维护第二套审批语义）。
        """
        if row.status != "pending_approval":
            raise ValueError(f"cannot approve action in {row.status} status")
        row.status = "approved"
        row.approved_by = approved_by
        row.approval_timestamp = utc_now()
        row.updated_at = utc_now()
        await self._session.flush()
        return row

    # ── resolutions ───────────────────────────────────────────────────

    async def record_resolution(
        self,
        case_row: DiscoverCaseRow,
        *,
        hypothesis_id: UUID,
        kind: str,
        rationale: str,
        resolved_by: UUID,
        approved_by: UUID,
        notes: str,
        evidence_refs: list[str],
    ) -> DiscoverResolutionRow:
        """HumanResolution 记录 + case 终态迁移（同一事务）。

        Resolution 不改写原 detector output；重复记录由 case 状态机拒绝
        （已终态 case 不再接受 resolution）。
        """
        if case_row.status in ("resolved", "dismissed", "archived"):
            raise ValueError(f"case is already in terminal status: {case_row.status}")
        workspace_id = self._require_workspace()
        now = utc_now()
        row = DiscoverResolutionRow(
            id=uuid4(),
            organization_id=self._context.organization_id,
            workspace_id=workspace_id,
            case_id=case_row.id,
            hypothesis_id=hypothesis_id,
            kind=kind,
            rationale=rationale,
            resolved_by=resolved_by,
            approved_by=approved_by,
            notes=notes,
            evidence_refs=evidence_refs,
            approval_timestamp=now,
            schema_version=_ROW_SCHEMA_VERSION,
        )
        self._session.add(row)
        case_row.resolution_ids = [*case_row.resolution_ids, str(row.id)]
        case_row.status = "dismissed" if kind in ("dismissed", "false_positive") else "resolved"
        case_row.updated_at = now
        await self._session.flush()
        return row

    def _require_workspace(self) -> UUID:
        """workspace 作用域是本仓储的硬前提（RLS GUC 依赖它）：缺失即拒绝。"""
        workspace_id = self._context.workspace_id
        if workspace_id is None:
            raise TenantContextRequired("workspace scope is required for discover writes")
        return workspace_id


def hypothesis_feed_counts(row: DiscoverHypothesisRow) -> tuple[int, int, int]:
    """feed 计数投影：supporting/contradicting/missing（域模型 kind 词汇）。"""
    supporting = 0
    contradicting = 0
    missing = 0
    for tag in row.evidence_tags:
        kind = tag.get("kind") if isinstance(tag, dict) else None
        if kind == "supporting":
            supporting += 1
        elif kind == "contradicting":
            contradicting += 1
        elif kind == "missing":
            missing += 1
    return supporting, contradicting, missing


def hypothesis_freshness_hours(row: DiscoverHypothesisRow) -> float:
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - created).total_seconds() / 3600.0, 0.0)
