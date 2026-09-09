"""S1-T4 二轮修复：v2 审计 metadata 非空契约（空串拒绝，0006 历史不动）。

Revision ID: 0007_audit_metadata_nonempty
Revises: 0006_audit_contract

0006 只冻结 decision_id/policy_revision 的 NULL 配对与 result 一致性，空串仍可绕过
（'' 满足 (IS NULL) = (IS NULL) 且 IS NOT NULL）。二轮验收裁决：v2 行两列必须同为
NULL 或同为长度 ≥ 1 的非空字符串；空串、单边空串、单边 NULL 一律拒绝。

本迁移追加两条 CHECK（全部限定 v2 行；v1 行保持 0005 冻结契约不变），与
AuditRecord / AuditEventData 的 Pydantic min_length=1 校验逐字一致：

- ck_audit_events_v2_decision_id_nonempty：v2 → decision_id IS NULL 或 length > 0；
- ck_audit_events_v2_policy_revision_nonempty：v2 → policy_revision IS NULL 或 length > 0。

0006 为已提交历史，不做任何修改；downgrade 全撤本迁移的两条 CHECK，可逆。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_audit_metadata_nonempty"
down_revision: str | None = "0006_audit_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_audit_events_v2_decision_id_nonempty"),
        "audit_events",
        "audit_schema_version = 1 OR (decision_id IS NULL OR length(decision_id) > 0)",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_v2_policy_revision_nonempty"),
        "audit_events",
        "audit_schema_version = 1 OR (policy_revision IS NULL OR length(policy_revision) > 0)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_audit_events_v2_decision_id_nonempty"), "audit_events", type_="check")
    op.drop_constraint(
        op.f("ck_audit_events_v2_policy_revision_nonempty"), "audit_events", type_="check"
    )
