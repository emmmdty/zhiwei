"""S10 fix-A：run 模板投影持久化——runs.template 列（R1 REJECT D2）。

Revision ID: 0019_run_template
Revises: 0018_discover

背景：POST /runs 的 body template 是 run 的 planner 意图标识（caller-declared
绑定），此前只在创建响应里回显、没有持久化——刷新/断网恢复后 run 投影丢失该
事实，web ViewManifest 的 templateId→App 绑定解析在生产路径永远落空（前端如实
渲染 "No app binding"）。

设计要点：

- template 是「创建期的 caller 声明」，不是外键关系：模板 id 是 planner 意图
  词汇（fixture 模板名 / pack 模板 id），不是租户数据行，建 FK 反而把意图词汇
  错当成实体；NULL = 无 planner 意图（eval executor 直连命令路径的 run、
  0019 之前的存量行）——诚实缺席，不猜默认值；
- 列属既有 tenant 表（runs）：RLS 策略/角色授权不受加列影响（行级 org+ws GUC
  过滤与列无关）；SELECT/INSERT 是表级授权，加列零变更；不授 UPDATE——
  创建期事实，写后不可变（MUTABLE_COLUMNS 契约不变）；
- ORM models.py 逐项镜像（S9-T1 教训）；downgrade 撤销列。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_run_template"
down_revision: str | None = "0018_discover"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("template", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "template")
