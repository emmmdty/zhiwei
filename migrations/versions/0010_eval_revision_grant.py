"""S2 收口：eval 密封所需的 migration revision 读取授权（GRANT SELECT on alembic_version）。

背景（已实证的预存故障）：`zhiwei eval seal-empty --check` / `eval seal` /
`eval run --seal` 在 app 会话内读 `alembic_version` 取 provenance revision
（src/zhiwei/cli/evals.py `_migration_revision`），但 0001 起没有任何迁移把
alembic_version 的 SELECT 授给 zhiwei_app——S0 Gate 当时通过依赖的是测试库
曾存在的 ALTER DEFAULT PRIVILEGES 残留（0003 迁移注释已记录该残留并防御），
库重建后残留消失，tests/integration/foundation/test_empty_run.py 两例
`permission denied for table alembic_version` 变红。

裁决：alembic_version 是单行全局 schema 版本元数据，不属于 tenant 数据面
（无 RLS、无行级敏感信息）；密封 artifact 的 provenance 必须绑定真实
revision（fail closed），因此授予 zhiwei_app 只读 SELECT 是最小授权：

- 仅 SELECT，不授 INSERT/UPDATE/DELETE（迁移写路径仍只属于 zhiwei_migrator）；
- downgrade 撤销 SELECT，恢复无授权原状；
- 无表结构变化，无 RLS 语义变化。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_eval_revision_grant"
down_revision: str | None = "0009_scim_group_member_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "alembic_version"


def upgrade() -> None:
    op.execute(f'GRANT SELECT ON TABLE "{_TABLE}" TO zhiwei_app')


def downgrade() -> None:
    op.execute(f'REVOKE SELECT ON TABLE "{_TABLE}" FROM zhiwei_app')
