"""S11-T3 三段式升级 contract 阶段（docs/operations/upgrade.md §2.2）。

migrate(回填)：旧行 dispatch_deadline = available_at + 300s（与 backward reader 的
fallback 常量一致——upgrade.py GRACE_SECONDS 唯一事实）。
contract：SET NOT NULL + server_default（insert 时刻 + GRACE）——**destructive**：
NULL 从此不可能。旧代码 INSERT 不带该列时由 server_default 兜底，不失败；
「旧代码必须退场」的纪律体现在：旧代码无法感知/设置该列语义（超龄判定锚只对
新代码有效）。执行需显式 checkpoint（upgrade.py 编排层 / ops upgrade-run --contract）。

downgrade：解除 NOT NULL 并 DROP DEFAULT——回到 expand 后的兼容窗口。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_contract_dispatch_deadline"
down_revision: str | None = "0025_expand_dispatch_deadline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# backward reader fallback（upgrade.py）与回填常量必须一致
GRACE_SECONDS = 300


def upgrade() -> None:
    # migrate(回填)：旧行获得锚点。FORCE RLS 对 owner 也生效（0001），逐行 UPDATE
    # 无法用单一 GUC 覆盖多租户行——迁移期维护窗口内临时禁用 RLS，回填后立即恢复
    # ENABLE + FORCE（DDL 权限属于 owner，不引入 superuser 依赖）。
    op.execute("ALTER TABLE outbox DISABLE ROW LEVEL SECURITY")
    op.execute(
        f"UPDATE outbox SET dispatch_deadline = available_at + interval '{GRACE_SECONDS} seconds'"
        " WHERE dispatch_deadline IS NULL"
    )
    op.execute("ALTER TABLE outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outbox FORCE ROW LEVEL SECURITY")
    # contract：收紧 NOT NULL，同时给 server_default——旧代码的 INSERT 路径不写该列，
    # 默认值（insert 时刻 + GRACE）保证兼容；available_at 的 server_default 同为
    # now()，两者同瞬求值，语义与回填常量一致。
    op.alter_column("outbox", "dispatch_deadline", nullable=False)
    op.execute(
        # 常量与 GRACE_SECONDS 同源（f-string 内插；PG 会把 interval '300 seconds'
        # 归一化为 '00:05:00'——models.py 的 server_default 与之语义一致）
        f"ALTER TABLE outbox ALTER COLUMN dispatch_deadline"
        f" SET DEFAULT (now() + interval '{GRACE_SECONDS} seconds')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE outbox ALTER COLUMN dispatch_deadline DROP DEFAULT")
    op.alter_column("outbox", "dispatch_deadline", existing_type=sa.DateTime(timezone=True),
                    nullable=True)
