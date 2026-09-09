"""S1-T4 修复：AuditRecord/DB CHECK 契约（repair addendum §3.1.6/§3.2）。

Revision ID: 0006_audit_contract
Revises: 0005_audit_structured

背景：0005 只保证 v2 行「字段齐全」，未冻结 metadata 配对与格式边界——decision_id /
policy_revision 可只填其一、decision_reason 可为 NULL、payload_digest 可为任意串、
resource_version 可为负；Pydantic 边界与 DB 不一致，direct INSERT 可绕过全部规则。

本迁移追加六条 CHECK（全部限定 v2 行；v1 行保持 0005 冻结契约不变），与
AuditRecord / AuditEventData 的 Pydantic 校验逐条一致：

- ck_audit_events_v2_decision_reason：v2 → decision_reason 非空且非空串；
- ck_audit_events_v2_decision_pairing：(decision_id IS NULL) = (policy_revision IS NULL)——
  禁止只有 decision_id 或只有 policy_revision；
- ck_audit_events_v2_allowed_metadata：result='allowed' → 两者同时非空（真实 OPA 决策）；
- ck_audit_events_v2_failed_metadata：result='failed' → 两者同时为 NULL（本地/业务失败
  绝不伪造 OPA metadata）；
- ck_audit_events_v2_payload_digest：payload_digest ~ '^sha256:[0-9a-f]{64}$'（digest_bytes
  输出形状）；
- ck_audit_events_v2_resource_version：resource_version >= 0（0 = unknown 哨兵，只用于
  denied/failed 路径）。

既有集群兼容性：当前生产代码从未把 v2 审计接入 mutation（9f134bf 交接确认），
audit_events 无生产 v2 行；0005 的 v1_shape 约束保证 v1 行新列全 NULL，与 v2 限定
CHECK 互不冲突。downgrade 全撤，可逆。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_audit_contract"
down_revision: str | None = "0005_audit_structured"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 六条 CHECK 全部限定 v2 行（audit_schema_version = 2）；v1 行不受影响。
    op.create_check_constraint(
        op.f("ck_audit_events_v2_decision_reason"),
        "audit_events",
        "audit_schema_version = 1 OR (decision_reason IS NOT NULL "
        "AND length(decision_reason) > 0)",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_v2_decision_pairing"),
        "audit_events",
        "audit_schema_version = 1 OR ((decision_id IS NULL) = (policy_revision IS NULL))",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_v2_allowed_metadata"),
        "audit_events",
        "audit_schema_version = 1 OR result <> 'allowed' OR "
        "(decision_id IS NOT NULL AND policy_revision IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_v2_failed_metadata"),
        "audit_events",
        "audit_schema_version = 1 OR result <> 'failed' OR "
        "(decision_id IS NULL AND policy_revision IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_v2_payload_digest"),
        "audit_events",
        "audit_schema_version = 1 OR payload_digest ~ '^sha256:[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_v2_resource_version"),
        "audit_events",
        "audit_schema_version = 1 OR resource_version >= 0",
    )


def downgrade() -> None:
    for constraint in (
        "ck_audit_events_v2_decision_reason",
        "ck_audit_events_v2_decision_pairing",
        "ck_audit_events_v2_allowed_metadata",
        "ck_audit_events_v2_failed_metadata",
        "ck_audit_events_v2_payload_digest",
        "ck_audit_events_v2_resource_version",
    ):
        op.drop_constraint(op.f(constraint), "audit_events", type_="check")
