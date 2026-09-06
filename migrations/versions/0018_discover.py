"""S10-T4c：Discover workbench 持久化——hypotheses/cases/actions/resolutions
+ hypothesis_events（FORCE RLS）。

Revision ID: 0018_discover
Revises: 0017_cases

S8 收口补齐（specs/s8-discover-actions.md §4/§6、handoff
s8-discover-case-action-e2e-exception 解锁条件）：S8 冻结域（hypotheses/
cases/actions/resolutions）此前只有内存态 manager（交付报告登记的持久化缺口，
0017 对 S6 Case 同款处置），本迁移补上 workbench 读写面的 PG 持久层。设计要点：

- 四张业务表属 tenant 数据面：organization_id + workspace_id 复合外键、
  FORCE RLS（org+ws GUC 过滤，与 0012/0017 同策略）；
- discover_hypotheses：workbench 投影行（一 chain 一行，status/owner 原地
  状态机迁移）；detector output（evidence/score/probes/entities/watermarks）
  是不可变内容列——本迁移不授 UPDATE，另有 BEFORE UPDATE 守护触发器（纵深
  防御，0012 memory_records_guard 同型）；转移轨迹落 discover_hypothesis_events
  追加式台账（payload_digest 幂等键），不改写原 detector output；
- discover_cases：同 hypothesis 至多一条活跃 case（partial unique 索引在
  数据面强制——刷新/重试不复制 case）；status/owner/关联 id 列表列级 UPDATE；
- discover_actions：提交即 pending_approval（服务端门禁，无默认执行路径）；
  status/s2_decision_id/approved_by/approval_timestamp 列级 UPDATE；SoD 与
  决策配对以 CHECK 镜像（0011 approval_requests 同款：approved 双列配对、
  approver ≠ requester）；input_digest（S2 审批输入内容寻址，复用
  discover.actions._action_input_digest 单一事实源）唯一键让重复提交 409；
- discover_resolutions：HumanResolution 追加式记录（SELECT/INSERT，不可变；
  case 终态由应用层状态机保证，重复记录 409）；
- CHECK 约束与 ORM models.py 逐项镜像（S9-T1 教训）；
- downgrade 撤销全部触发器、授权与表。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0018_discover"
down_revision: str | None = "0017_cases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"

_HYPOTHESES = "discover_hypotheses"
_HYPOTHESIS_EVENTS = "discover_hypothesis_events"
_CASES = "discover_cases"
_ACTIONS = "discover_actions"
_RESOLUTIONS = "discover_resolutions"

_HYPOTHESES_UPDATE_COLUMNS = ("status", "owner", "updated_at")
_CASES_UPDATE_COLUMNS = (
    "status",
    "owner",
    "action_request_ids",
    "resolution_ids",
    "updated_at",
)
_ACTIONS_UPDATE_COLUMNS = (
    "status",
    "s2_decision_id",
    "approved_by",
    "approval_timestamp",
    "updated_at",
)

_HYPOTHESIS_STATUS_VALUES = (
    "proposed",
    "falsification_in_progress",
    "ready_for_triage",
    "rejected",
    "in_triage",
    "accepted",
    "dismissed",
    "superseded",
)
_CASE_STATUS_VALUES = ("open", "in_progress", "resolved", "dismissed", "archived")
_ACTION_TYPE_VALUES = ("query", "create", "modify", "delete", "notify", "export")
_ACTION_STATUS_VALUES = ("proposed", "pending_approval", "approved", "rejected")
_RESOLUTION_KIND_VALUES = (
    "accepted",
    "dismissed",
    "false_positive",
    "mitigated",
    "reopened",
    "superseded",
)


def _jsonb_list_default() -> Any:
    return sa.JSON().with_variant(pg.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        _HYPOTHESES,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("signal_id", pg.UUID(), nullable=False),
        sa.Column("program_version_id", pg.UUID(), nullable=False),
        sa.Column("detector_pack_id", pg.UUID(), nullable=False),
        sa.Column("detector_pack_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column(
            "affected_entities",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence_tags",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "suggested_validation_actions",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "source_watermarks",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "proposed_probes",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "falsification_results",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("dedup_key", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("created_by", pg.UUID(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _HYPOTHESIS_STATUS_VALUES) + ")",
            name="hypothesis_status",
        ),
        sa.CheckConstraint(
            "kind IN ('supporting', 'contradicting', 'missing')", name="hypothesis_kind"
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'high', 'critical')", name="hypothesis_severity"
        ),
        sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)", name="hypothesis_score"),
        sa.CheckConstraint("detector_pack_version > 0", name="hypothesis_detector_pack_version"),
        sa.CheckConstraint("length(title) >= 1", name="hypothesis_title"),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_discover_hypotheses_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("organization_id", "workspace_id", "id", name="uq_discover_hypotheses_scope_id"),
    )

    op.create_table(
        _HYPOTHESIS_EVENTS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("hypothesis_id", pg.UUID(), nullable=False, index=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            _jsonb_list_default(),
            nullable=False,
        ),
        sa.Column("payload_digest", sa.String(length=71), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_discover_hypothesis_events_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "hypothesis_id"],
            [
                f"{_HYPOTHESES}.organization_id",
                f"{_HYPOTHESES}.workspace_id",
                f"{_HYPOTHESES}.id",
            ],
            name="fk_discover_hypothesis_events_hypothesis",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "hypothesis_id",
            "action",
            "payload_digest",
            name="uq_discover_hypothesis_events_transition",
        ),
    )

    op.create_table(
        _CASES,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("hypothesis_id", pg.UUID(), nullable=False, index=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("dedup_key", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "hypothesis_ids",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "action_request_ids",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "resolution_ids",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_by", pg.UUID(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _CASE_STATUS_VALUES) + ")",
            name="discover_case_status",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'high', 'critical')", name="discover_case_severity"
        ),
        sa.CheckConstraint("length(title) >= 1", name="discover_case_title"),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_discover_cases_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "hypothesis_id"],
            [
                f"{_HYPOTHESES}.organization_id",
                f"{_HYPOTHESES}.workspace_id",
                f"{_HYPOTHESES}.id",
            ],
            name="fk_discover_cases_hypothesis",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("organization_id", "workspace_id", "id", name="uq_discover_cases_scope_id"),
    )
    # 刷新/重试不复制 case：同租户同 hypothesis 至多一条（数据面最后防线）
    op.create_index(
        "uq_discover_cases_hypothesis",
        _CASES,
        ["organization_id", "workspace_id", "hypothesis_id"],
        unique=True,
    )

    op.create_table(
        _ACTIONS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("hypothesis_id", pg.UUID(), nullable=False, index=True),
        sa.Column("case_id", pg.UUID(), nullable=False, index=True),
        sa.Column("action_type", sa.String(length=16), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column(
            "parameters",
            sa.JSON().with_variant(pg.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("requested_by", pg.UUID(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("s2_decision_id", pg.UUID(), nullable=True),
        sa.Column("approved_by", pg.UUID(), nullable=True),
        sa.Column("approval_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_digest", sa.String(length=71), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action_type IN (" + ", ".join(f"'{v}'" for v in _ACTION_TYPE_VALUES) + ")",
            name="discover_action_type",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _ACTION_STATUS_VALUES) + ")",
            name="discover_action_status",
        ),
        # 决策配对（0011 approval_decision_fields 同款）：approved 双列齐备
        sa.CheckConstraint(
            "(status = 'approved') = "
            "(approved_by IS NOT NULL AND approval_timestamp IS NOT NULL)",
            name="discover_action_decision_fields",
        ),
        # SoD（0011 approval_sod_requester 同款）：approver ≠ requester
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> requested_by",
            name="discover_action_sod_requester",
        ),
        # pending 之后必然绑定 S2 审批决定（discover 不维护第二套审批语义）
        sa.CheckConstraint(
            "(status IN ('pending_approval', 'approved')) = (s2_decision_id IS NOT NULL)",
            name="discover_action_s2_binding",
        ),
        sa.CheckConstraint("length(tool_name) >= 1", name="discover_action_tool_name"),
        sa.CheckConstraint("length(rationale) >= 1", name="discover_action_rationale"),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_discover_actions_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "hypothesis_id"],
            [
                f"{_HYPOTHESES}.organization_id",
                f"{_HYPOTHESES}.workspace_id",
                f"{_HYPOTHESES}.id",
            ],
            name="fk_discover_actions_hypothesis",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "case_id"],
            [f"{_CASES}.organization_id", f"{_CASES}.workspace_id", f"{_CASES}.id"],
            name="fk_discover_actions_case",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("organization_id", "workspace_id", "id", name="uq_discover_actions_scope_id"),
    )
    # 刷新/重试不复制 action：同 case 同审批输入（内容寻址）至多一条
    op.create_index(
        "uq_discover_actions_input_digest",
        _ACTIONS,
        ["organization_id", "workspace_id", "case_id", "input_digest"],
        unique=True,
    )

    op.create_table(
        _RESOLUTIONS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("case_id", pg.UUID(), nullable=False, index=True),
        sa.Column("hypothesis_id", pg.UUID(), nullable=False, index=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("resolved_by", pg.UUID(), nullable=False),
        sa.Column("approved_by", pg.UUID(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "evidence_refs",
            _jsonb_list_default(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("approval_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{v}'" for v in _RESOLUTION_KIND_VALUES) + ")",
            name="discover_resolution_kind",
        ),
        sa.CheckConstraint("length(rationale) >= 1", name="discover_resolution_rationale"),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_discover_resolutions_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "case_id"],
            [f"{_CASES}.organization_id", f"{_CASES}.workspace_id", f"{_CASES}.id"],
            name="fk_discover_resolutions_case",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_discover_resolutions_scope_id"
        ),
    )

    # hypothesis 内容列不可变 + 状态机迁移只写授权列（纵深防御：0012 同型守护）
    op.execute(
        """
        CREATE FUNCTION discover_hypotheses_guard() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.signal_id IS DISTINCT FROM OLD.signal_id
               OR NEW.program_version_id IS DISTINCT FROM OLD.program_version_id
               OR NEW.detector_pack_id IS DISTINCT FROM OLD.detector_pack_id
               OR NEW.detector_pack_version IS DISTINCT FROM OLD.detector_pack_version
               OR NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.title IS DISTINCT FROM OLD.title
               OR NEW.description IS DISTINCT FROM OLD.description
               OR NEW.severity IS DISTINCT FROM OLD.severity
               OR NEW.score IS DISTINCT FROM OLD.score
               OR NEW.affected_entities IS DISTINCT FROM OLD.affected_entities
               OR NEW.evidence_tags IS DISTINCT FROM OLD.evidence_tags
               OR NEW.suggested_validation_actions IS DISTINCT FROM OLD.suggested_validation_actions
               OR NEW.source_watermarks IS DISTINCT FROM OLD.source_watermarks
               OR NEW.proposed_probes IS DISTINCT FROM OLD.proposed_probes
               OR NEW.falsification_results IS DISTINCT FROM OLD.falsification_results
               OR NEW.dedup_key IS DISTINCT FROM OLD.dedup_key
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'discover hypothesis % detector output is immutable', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER discover_hypotheses_content_guard BEFORE UPDATE ON \"{_HYPOTHESES}\" "
        "FOR EACH ROW EXECUTE FUNCTION discover_hypotheses_guard()"
    )

    for table, update_columns in (
        (_HYPOTHESES, _HYPOTHESES_UPDATE_COLUMNS),
        (_CASES, _CASES_UPDATE_COLUMNS),
        (_ACTIONS, _ACTIONS_UPDATE_COLUMNS),
    ):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
            f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
        )
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM PUBLIC')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO zhiwei_app')
        # 列级 UPDATE：状态机迁移列（表级 UPDATE 维持 S0 冻结契约不授）
        op.execute(
            f'GRANT UPDATE ({", ".join(update_columns)}) ON TABLE "{table}" TO zhiwei_app'
        )

    for table in (_HYPOTHESIS_EVENTS, _RESOLUTIONS):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
            f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
        )
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM PUBLIC')
        # 台账/记录只追加：SELECT/INSERT 即完整能力，无 UPDATE/DELETE
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO zhiwei_app')


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS discover_hypotheses_content_guard ON discover_hypotheses")
    op.execute("DROP FUNCTION IF EXISTS discover_hypotheses_guard()")
    for table in (_RESOLUTIONS, _ACTIONS, _CASES, _HYPOTHESIS_EVENTS, _HYPOTHESES):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM zhiwei_app')
        op.execute(f'DROP POLICY IF EXISTS "{table}_tenant_isolation" ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.drop_index("uq_discover_actions_input_digest", table_name=_ACTIONS)
    op.drop_index("uq_discover_cases_hypothesis", table_name=_CASES)
    op.drop_table(_RESOLUTIONS)
    op.drop_table(_ACTIONS)
    op.drop_table(_CASES)
    op.drop_table(_HYPOTHESIS_EVENTS)
    op.drop_table(_HYPOTHESES)
