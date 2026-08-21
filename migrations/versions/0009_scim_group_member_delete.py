"""S1-T5：SCIM group reconciliation 的删员授权（GRANT DELETE on group_members）。

Revision ID: 0009_scim_group_member_delete
Revises: 0008_bootstrap_claims

S1-T5 设计裁决（docs/handoffs/s1-t5-design.md §3）：0002 只给 group_members
SELECT, INSERT；SCIM Group PUT replace 的 member reconciliation 需要 remove
成员，必须补 DELETE 授权。仅授权增量，无表结构变化：

- group_members 的 FORCE RLS policy（0002 建立）覆盖表级全部行，GRANT 不影响
  RLS 过滤；zhiwei_app 非 owner、无 BYPASSRLS（0002 实证）；
- downgrade 撤销 DELETE，恢复 0002 的 SELECT, INSERT 授权原状；
- identity 引擎侧零改动：0003 已给 zhiwei_identity principals UPDATE(status) /
  external_identities SELECT,INSERT，SCIM 用户生命周期无新增授权需求。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_scim_group_member_delete"
down_revision: str | None = "0008_bootstrap_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "group_members"


def upgrade() -> None:
    op.execute(f'GRANT DELETE ON TABLE "{_TABLE}" TO zhiwei_app')


def downgrade() -> None:
    op.execute(f'REVOKE DELETE ON TABLE "{_TABLE}" FROM zhiwei_app')
