"""S1-T4 四轮修复：Organization bootstrap 持久 claim（identity-global 最终围栏）。

Revision ID: 0008_bootstrap_claims
Revises: 0007_audit_metadata_nonempty

背景：bootstrap 的幂等 scope 绑定 owner，但「org 存在性检查」与「幂等 claim」之间
存在 TOCTOU 窗口，同一 principal 可并发创建多个 org；membership 被删除后资格还会
「重置」——principal 移出后又能 bootstrap 新 org。持久 claim 是事务内最终围栏：
一个 principal 最多 claim 一个由自己 bootstrap 创建的 Organization，membership
生命周期不影响 claim。

- organization_bootstrap_claims：principal_id PRIMARY KEY（identity-global）、
  organization_id UNIQUE。**不属于 tenant 数据面**：无 RLS、无 org/ws 列，不给
  zhiwei_app / zhiwei_identity / PUBLIC 任何直接表权限（显式 REVOKE，防
  ALTER DEFAULT PRIVILEGES 残留，与 0003 同款纵深防御）——只能经窄函数访问；
- zhiwei_claim_organization_bootstrap(principal_id uuid, organization_id uuid)
  -> boolean：transaction-level advisory lock 按 principal 串行化（等效原子 CAS），
  UNIQUE 约束是第二层防线；claim 已存在且 target 相同 → true；不同 → false；
  **禁止更新/迁移既有 claim**。SECURITY DEFINER（owner=zhiwei_migrator）、
  search_path=pg_catalog,public、无动态 SQL、对象全限定；REVOKE PUBLIC，
  只 GRANT EXECUTE 给 zhiwei_app；
- backfill：历史 `organization.create:<principal>` idempotency 记录（owner-bound
  bootstrap scope，commands._organization_scope）迁移为 claim，迁移后校验零遗漏；
  同一 principal 已有多个不同 bootstrap targets → **fail closed**（明确错误，禁止
  静默选「第一个」）；scope 后缀不可解析为 UUID → 同样 fail closed；
- downgrade 全撤表与函数，可逆。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_bootstrap_claims"
down_revision: str | None = "0007_audit_metadata_nonempty"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOOTSTRAP_SCOPE_PREFIX = "organization.create:"
_TABLE = "organization_bootstrap_claims"
_FUNCTION = "zhiwei_claim_organization_bootstrap"
_FUNCTION_SIGNATURE = f"public.{_FUNCTION}(uuid, uuid)"
_FUNCTION_CREATE = f"public.{_FUNCTION}(p_principal uuid, p_organization uuid)"

_UUID_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_organization_bootstrap_claims_schema_version")
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_organization_bootstrap_claims_principal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_organization_bootstrap_claims_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("principal_id", name="pk_organization_bootstrap_claims"),
        sa.UniqueConstraint(
            "organization_id", name="uq_organization_bootstrap_claims_organization_id"
        ),
    )

    # 表不属于 tenant 数据面：不给任何角色直接权限（纵深防御，防默认 ACL 残留）。
    # 注意 REVOKE 顺序：PUBLIC 先于具体角色，避免 PUBLIC 的 ANY 权限残留遮蔽判断。
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_TABLE}" FROM PUBLIC')
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_TABLE}" FROM zhiwei_app')
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_TABLE}" FROM zhiwei_identity')

    _backfill_from_idempotency()

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION_CREATE}
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_existing uuid;
        BEGIN
            -- 按 principal 串行化：同一 principal 的并发 bootstrap 只能一个提交；
            -- 锁在事务提交/回滚时自动释放（xact 级）。锁键与行数据解耦，无死锁环：
            -- 持锁路径只写本 principal 自己的 claim 行。
            PERFORM pg_advisory_xact_lock(
                hashtextextended('organization_bootstrap:' || p_principal::text, 0)
            );
            SELECT c.organization_id INTO v_existing
            FROM public.organization_bootstrap_claims AS c
            WHERE c.principal_id = p_principal;
            IF FOUND THEN
                RETURN v_existing = p_organization;
            END IF;
            -- 无既有 claim：写入。UNIQUE(principal_id) 是第二层防线（正常情况下
            -- 已被 advisory lock 串行化）；INSERT 失败由调用事务整体回滚。
            INSERT INTO public.organization_bootstrap_claims
                (principal_id, organization_id, schema_version)
            VALUES (p_principal, p_organization, 1);
            RETURN true;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM zhiwei_identity")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION_SIGNATURE} TO zhiwei_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION_SIGNATURE}")
    op.drop_table(_TABLE)


def _backfill_from_idempotency() -> None:
    """历史 owner-bound bootstrap 幂等记录 → claim；歧义/不可解析一律 fail closed。"""
    connection = op.get_bind()
    scope_expr = f"substr(scope, length('{_BOOTSTRAP_SCOPE_PREFIX}') + 1)"

    malformed = connection.execute(
        sa.text(
            f"SELECT scope FROM idempotency_records "
            f"WHERE scope LIKE '{_BOOTSTRAP_SCOPE_PREFIX}%' "
            f"AND ({scope_expr})::text !~ :pattern"
        ),
        {"pattern": _UUID_PATTERN},
    ).fetchall()
    if malformed:
        samples = ", ".join(row[0] for row in malformed[:5])
        raise RuntimeError(
            "ambiguous bootstrap history: unparseable owner principal in "
            f"organization.create idempotency scopes ({samples}); refusing to backfill "
            "organization_bootstrap_claims"
        )

    ambiguous = connection.execute(
        sa.text(
            f"SELECT ({scope_expr})::uuid AS principal_id, "
            "count(DISTINCT organization_id) AS org_count "
            "FROM idempotency_records "
            f"WHERE scope LIKE '{_BOOTSTRAP_SCOPE_PREFIX}%' "
            "GROUP BY 1 HAVING count(DISTINCT organization_id) > 1"
        )
    ).fetchall()
    if ambiguous:
        samples = ", ".join(f"{row[0]}: {row[1]} targets" for row in ambiguous[:5])
        raise RuntimeError(
            "ambiguous bootstrap history: principals with multiple bootstrap targets "
            f"({samples}); refusing to backfill organization_bootstrap_claims (fail "
            "closed, never silently pick the first target)"
        )

    connection.execute(
        sa.text(
            f"INSERT INTO {_TABLE} (principal_id, organization_id, schema_version) "
            f"SELECT DISTINCT ({scope_expr})::uuid AS principal_id, "
            "organization_id, 1 "
            "FROM idempotency_records "
            f"WHERE scope LIKE '{_BOOTSTRAP_SCOPE_PREFIX}%'"
        )
    )

    # 可验证 backfill：插入后零遗漏（预期集合与 claim 集合必须完全一致）。
    missing = connection.execute(
        sa.text(
            f"SELECT count(*) FROM ("
            f"SELECT DISTINCT ({scope_expr})::uuid AS principal_id, organization_id "
            f"FROM idempotency_records WHERE scope LIKE '{_BOOTSTRAP_SCOPE_PREFIX}%' "
            f"EXCEPT "
            f"SELECT principal_id, organization_id FROM {_TABLE}"
            f") AS missing_backfill"
        )
    ).scalar_one()
    if missing != 0:
        raise RuntimeError(
            "backfill verification failed: "
            f"{missing} historical bootstrap idempotency records were not backfilled "
            f"into {_TABLE}"
        )
