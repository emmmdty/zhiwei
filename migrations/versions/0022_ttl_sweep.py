"""F-R6-06：TTL sweep 的跨租户目标发现——窄 SECURITY DEFINER 函数（P2b）。

Revision ID: 0022_ttl_sweep
Revises: 0021_source_ledger

背景：ADR-009 TTL 自动过期（expire_candidates）在生产环境无调度（F-R6-06）。
所有 tenant 表 FORCE RLS——zhiwei_app 无法自行枚举租户，sweep 的目标发现必须
走窄 definer 函数（0003_auth_sessions 同款纪律：跨组织发现只能调用窄
SECURITY DEFINER 函数）。

设计要点：

- 固定 SQL、无动态 SQL、SET search_path、STABLE；只暴露 (organization_id,
  workspace_id) 两列；谓词 = 存在过期 candidate（status='candidate' AND
  created_at < cutoff——与 PgMemoryRepository.expire_candidates 的 SQL 预过滤
  同谓词，域层 expire 前再取锁复验）；
- SECURITY DEFINER（owner=zhiwei_migrator）；REVOKE EXECUTE FROM PUBLIC；
  GRANT EXECUTE 仅授 zhiwei_app；不提供任意 SQL/全表导出接口；
- cutoff 由调用方传入（activity 侧 now - retention.candidate_ttl，ADR-009
  生产默认 30d），函数不内嵌策略——保留策略变更不触达数据面函数；
- downgrade 逆序撤销。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_ttl_sweep"
down_revision: str | None = "0021_source_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = "zhiwei_ttl_sweep_targets"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{_FUNCTION}(p_cutoff timestamptz)
        RETURNS TABLE (organization_id uuid, workspace_id uuid)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT DISTINCT r.organization_id, r.workspace_id
            FROM public.memory_records AS r
            WHERE r.status = 'candidate' AND r.created_at < p_cutoff
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.{_FUNCTION}(timestamptz) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{_FUNCTION}(timestamptz) TO zhiwei_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS public.{_FUNCTION}(timestamptz)")
