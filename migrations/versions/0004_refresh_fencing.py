"""S1-T2 验收修订：refresh ownership fencing（opaque owner token + calling phase）。

Revision ID: 0004_refresh_fencing
Revises: 0003_auth_sessions

独立验收发现（本迁移冻结的契约，RED 阶段随测试一起提交）：
- 旧 refresh_state 只有 idle/refreshing，owner 身份不可辨认：lease 过期后任何调用方
  都只能猜测刷新是否已发生；calling barrier 与 stale owner 的 fencing 无从表达；
- 本迁移只扩展状态机，不改变行为：refresh_state 取值改为 idle / leased / calling，
  新增 refresh_owner_token_hash（opaque owner token 的 SHA-256，DB 不存明文能力）；
- 不变量（DB 层 fail closed）：
  * refresh_state = 'idle' ⟺ refresh_owner_token_hash IS NULL（leased/calling 必须有 owner）；
  * revoked 行不得持有 lease 或 owner token（撤销打断在途 CAS）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_refresh_fencing"
down_revision: str | None = "0003_auth_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auth_sessions",
        sa.Column("refresh_owner_token_hash", sa.CHAR(length=64), nullable=True),
    )
    # 0003 以 op.f() 生成单前缀约束名；此处同样以 op.f() 显式命名（绕过 naming
    # convention 的二次前缀），保证 upgrade/downgrade 名字一致。
    op.drop_constraint(
        op.f("ck_auth_sessions_refresh_state"), "auth_sessions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_refresh_state"),
        "auth_sessions",
        "refresh_state IN ('idle', 'leased', 'calling')",
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_revoked_no_lease"), "auth_sessions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_revoked_no_lease"),
        "auth_sessions",
        "NOT (revoked_at IS NOT NULL AND "
        "(refresh_state <> 'idle' OR refresh_lease_expires_at IS NOT NULL "
        "OR refresh_owner_token_hash IS NOT NULL))",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_owner_token_consistency"),
        "auth_sessions",
        "NOT (refresh_state <> 'idle' AND refresh_owner_token_hash IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_auth_sessions_owner_token_consistency"), "auth_sessions", type_="check"
    )
    # 回退前归一化数据：旧约束只接受 idle/refreshing，残留的 leased/calling 行
    # （进程死亡遗留或测试中断）必须回到 idle 并清空 owner token/lease。
    op.execute(
        "UPDATE auth_sessions SET refresh_state = 'idle', "
        "refresh_owner_token_hash = NULL, refresh_lease_expires_at = NULL "
        "WHERE refresh_state <> 'idle'"
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_revoked_no_lease"), "auth_sessions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_revoked_no_lease"),
        "auth_sessions",
        "NOT (revoked_at IS NOT NULL AND "
        "(refresh_state <> 'idle' OR refresh_lease_expires_at IS NOT NULL))",
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_refresh_state"), "auth_sessions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_refresh_state"),
        "auth_sessions",
        "refresh_state IN ('idle', 'refreshing')",
    )
    op.drop_column("auth_sessions", "refresh_owner_token_hash")
