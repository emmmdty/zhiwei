"""S1-T2 验收修订：refresh ownership fencing（opaque owner token + calling phase）。

Revision ID: 0004_refresh_fencing
Revises: 0003_auth_sessions

独立验收发现（本迁移冻结的契约，RED 阶段随测试一起提交）：
- 旧 refresh_state 只有 idle/refreshing，owner 身份不可辨认：lease 过期后任何调用方
  都只能猜测刷新是否已发生；calling barrier 与 stale owner 的 fencing 无从表达；
- 本迁移只扩展状态机，不改变行为：refresh_state 取值改为 idle / leased / calling，
  新增 refresh_owner_token_hash（opaque owner token 的 SHA-256，DB 不存明文能力）；
- 不变量（DB 层 fail closed，验收修订 5 加强为双向 + lease）：
  * refresh_state = 'idle' ⟺ refresh_owner_token_hash IS NULL 且
    refresh_lease_expires_at IS NULL（idle 不得残留 owner/lease）；
  * leased/calling ⟹ owner token 与 lease 皆非 NULL；
  * revoked 行不得持有 lease 或 owner token（撤销打断在途 CAS）。
- 数据归一化（验收修订 5）：0003 的 legacy 'refreshing' 行无法判断 IdP 是否已调用，
  升级必须先 fail-closed 本地 revoke（revoked_at=now、state=idle、lease/owner 空、
  version+1），绝不把不确定的 legacy refreshing 会话恢复为 active；idle 行残留的
  lease 是中断异常遗留（0003 语义下 lease 只随 refreshing 出现），清空恢复 idle
  不变量。归一化必须先于新 CHECK 约束创建。
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
    # 数据归一化必须先于新约束：legacy refreshing（刷新在途，IdP 是否已调用不可知）
    # → fail-closed 本地 revoke；idle 残留 lease → 清空（idle 语义下无在途刷新）。
    op.execute(
        "UPDATE auth_sessions SET revoked_at = now(), refresh_state = 'idle', "
        "refresh_lease_expires_at = NULL, refresh_owner_token_hash = NULL, "
        "version = version + 1, updated_at = now() "
        "WHERE refresh_state = 'refreshing'"
    )
    op.execute(
        "UPDATE auth_sessions SET refresh_lease_expires_at = NULL, updated_at = now() "
        "WHERE refresh_state = 'idle' AND refresh_lease_expires_at IS NOT NULL"
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
    # 0003 schema 下该约束不存在（0004 首次引入），直接按加强后的双向定义创建
    op.create_check_constraint(
        op.f("ck_auth_sessions_owner_token_consistency"),
        "auth_sessions",
        "(refresh_state = 'idle' AND refresh_owner_token_hash IS NULL "
        "AND refresh_lease_expires_at IS NULL) OR "
        "(refresh_state IN ('leased', 'calling') "
        "AND refresh_owner_token_hash IS NOT NULL "
        "AND refresh_lease_expires_at IS NOT NULL)",
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
