"""S1-T4：结构化审计字段（设计/验收确认方案，见 docs/handoffs/s1-t4-design-gap.md）。

Revision ID: 0005_audit_structured
Revises: 0004_refresh_fencing

背景：既有 AuditEvent 只有 actor_ref/resource/payload_digest，无法结构化保存冻结设计
（总设计 §9.4、PERMISSIONS §14）要求的 effective identity、resource version、OPA
decision/revision、result、request/trace id。payload_digest 是不可逆摘要，不构成
「记录包含这些字段」，禁止把字段拼进 actor_ref 或藏进 digest。

本迁移追加列并冻结 digest 版本分派：
- audit_schema_version：v1（既有公式，逐字节不变）与 v2（覆盖全部语义字段）；
- 存量行 audit_schema_version=1、新列 NULL，旧 digest 不重算仍可验证；
- v2 行完整性由 CHECK 约束在 DB 层 fail closed：effective_identity_ref /
  resource_version / result / request_id / trace_id 必填；decision_id /
  policy_revision / decision_reason 允许 NULL——fail-closed 本地拒绝绝不伪造 OPA
  metadata（T3 契约）；
- result 枚举 allowed | denied | failed（v1 行 NULL 不检查）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_audit_structured"
down_revision: str | None = "0004_refresh_fencing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column(
            "audit_schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
    )
    op.add_column("audit_events", sa.Column("effective_identity_ref", sa.Text(), nullable=True))
    op.add_column("audit_events", sa.Column("resource_version", sa.Integer(), nullable=True))
    op.add_column("audit_events", sa.Column("decision_id", sa.Text(), nullable=True))
    op.add_column("audit_events", sa.Column("policy_revision", sa.Text(), nullable=True))
    op.add_column("audit_events", sa.Column("decision_reason", sa.Text(), nullable=True))
    op.add_column("audit_events", sa.Column("result", sa.Text(), nullable=True))
    op.add_column("audit_events", sa.Column("request_id", sa.Text(), nullable=True))
    op.add_column("audit_events", sa.Column("trace_id", sa.Text(), nullable=True))

    op.create_check_constraint(
        op.f("ck_audit_events_audit_schema_version"),
        "audit_events",
        "audit_schema_version IN (1, 2)",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_result"),
        "audit_events",
        "result IS NULL OR result IN ('allowed', 'denied', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_v2_complete"),
        "audit_events",
        "audit_schema_version = 1 OR ("
        "effective_identity_ref IS NOT NULL AND resource_version IS NOT NULL "
        "AND result IS NOT NULL AND request_id IS NOT NULL AND trace_id IS NOT NULL)",
    )
    # v1 行必须保持 v1 形状（独立审查 O1）：新列全部 NULL，杜绝在 v1 行上伪造 v2 样式字段
    op.create_check_constraint(
        op.f("ck_audit_events_v1_shape"),
        "audit_events",
        "audit_schema_version = 2 OR ("
        "effective_identity_ref IS NULL AND resource_version IS NULL "
        "AND decision_id IS NULL AND policy_revision IS NULL "
        "AND decision_reason IS NULL AND result IS NULL "
        "AND request_id IS NULL AND trace_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_audit_events_v1_shape"), "audit_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_audit_events_v2_complete"), "audit_events", type_="check"
    )
    op.drop_constraint(op.f("ck_audit_events_result"), "audit_events", type_="check")
    op.drop_constraint(
        op.f("ck_audit_events_audit_schema_version"), "audit_events", type_="check"
    )
    for column in (
        "trace_id",
        "request_id",
        "result",
        "decision_reason",
        "policy_revision",
        "decision_id",
        "resource_version",
        "effective_identity_ref",
        "audit_schema_version",
    ):
        op.drop_column("audit_events", column)
