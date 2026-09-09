"""S11-T3 三段式升级 expand 阶段（docs/operations/upgrade.md §2.2）。

outbox.dispatch_deadline TIMESTAMPTZ（NULLable）：崩溃窗口 #2（commit 后、dispatch
前进程死亡）的「pending 超龄命令」判定锚点。expand 阶段只加列——旧代码不感知，
新旧混跑安全；回填与 NOT NULL 收紧在 0026 contract（destructive，需显式 checkpoint）。

downgrade：drop column，兼容性无损（旧代码从未读写该列）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_expand_dispatch_deadline"
down_revision: str | None = "0024_memory_redaction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column("dispatch_deadline", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox", "dispatch_deadline")
