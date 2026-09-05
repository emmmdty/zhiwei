"""S10：Agent Studio draft 持久面——agent_definitions 增补 draft revision 列。

Revision ID: 0016_studio_agents
Revises: 0015_release_claims

设计取舍（plan Task 2 允许自选，理由记录如下）：

- draft revision 落 agent_definitions 本行（revision 计数 + draft 内容列），不建
  独立 append 表：CAS 需要单一权威行做「条件 UPDATE（revision 旧值匹配）→ 原子
  自增」，append 表要靠 MAX(revision) + 唯一约束竞争实现同一语义且把当前态读取
  变成聚合查询；revision 历史/diff 是 T3 发布流的关注点，届时可另起表而不动本契约。
- 新增列全部带 server default：既有行（含 S9 合同测试的种子行）零回填即通过；
- RLS 沿用 0001 的表级策略（org+ws GUC 过滤覆盖新增列，无需新 policy）；授权按
  列扩展——zhiwei_app 在 0001 已获 UPDATE(name, lifecycle)，draft 内容列与
  revision/updated_at 一起以列级 UPDATE 授予；INSERT/SELECT 已有。DELETE 仍不授
  （draft 退役属生命周期语义，不提供绕过 release 状态机的删除路径）；
- CHECK (revision > 0) 与 ORM CheckConstraint(name="revision") 镜像（命名约定
  ck_agent_definitions_revision），S9-T1 教训：迁移与 ORM 漂移会让 autogenerate
  复现约束。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0016_studio_agents"
down_revision: str | None = "0015_release_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_definitions"
_DRAFT_UPDATE_COLUMNS = (
    "description",
    "instructions",
    "task_graph",
    "capabilities",
    "revision",
    "updated_at",
)
_REVISION_CHECK = "ck_agent_definitions_revision"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        _TABLE,
        sa.Column("instructions", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(_TABLE, sa.Column("task_graph", pg.JSONB(), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column(
            "capabilities", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        f'ALTER TABLE "{_TABLE}" ADD CONSTRAINT {_REVISION_CHECK} CHECK (revision > 0)'
    )
    op.execute(
        f'GRANT UPDATE ({", ".join(_DRAFT_UPDATE_COLUMNS)}) ON TABLE "{_TABLE}" TO zhiwei_app'
    )


def downgrade() -> None:
    op.execute(f'REVOKE UPDATE ({", ".join(_DRAFT_UPDATE_COLUMNS)}) ON TABLE "{_TABLE}" FROM zhiwei_app')
    op.execute(f'ALTER TABLE "{_TABLE}" DROP CONSTRAINT {_REVISION_CHECK}')
    op.drop_column(_TABLE, "updated_at")
    op.drop_column(_TABLE, "revision")
    op.drop_column(_TABLE, "capabilities")
    op.drop_column(_TABLE, "task_graph")
    op.drop_column(_TABLE, "instructions")
    op.drop_column(_TABLE, "description")
