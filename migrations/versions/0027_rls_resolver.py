"""membership/TTL resolver 的窄 RLS 旁路角色（A 档，产品化窗口 2026-09-08）。

缺陷（见 docs/handoffs/s11-followup-github-and-live-evidence.md 批次 C 与
tests/security/identity/test_rls_resolver.py 模块说明）：`zhiwei_principal_memberships`
与 `zhiwei_ttl_sweep_targets` 是 SECURITY DEFINER（owner=zhiwei_migrator），但租户表
FORCE RLS 对非超级用户 definer 同样生效（RLS 豁免只看 current_user 的 BYPASSRLS/
超级用户属性；PG 同时禁止在 SECURITY DEFINER 函数内 SET role）。CI 测试栈的
migrator 是引导超级用户故测试不可见；产品姿态（init-local-product.sh 的普通
migrator）下两个函数恒返回空——登录后组织列表为空、TTL sweep 永不清理。

修复（dispatcher 窄 BYPASSRLS 先例的函数级收窄版）：
- 角色 zhiwei_rls_resolver（NOLOGIN + BYPASSRLS）由 init 脚本供给（集群级角色，
  迁移不创建——迁移在产品姿态下以无 CREATEROLE 的 migrator 运行，角色缺席时本
  迁移大声失败并提示供给动作，不静默降级）；
- 两函数 OWNER 转移到该角色：SECURITY DEFINER 以 owner 身份执行，BYPASSRLS 随
  owner 生效——旁路面收窄到「这两个函数体」，表所有权不变；
  注意：后续需要改这两个函数的迁移须先 `SET ROLE zhiwei_rls_resolver`
  （migrator 已是该角色成员，init 脚本授予）；
- resolver 表权限 = 恰好函数体读取的 4 张表的 SELECT；
- EXECUTE 授权面不变（memberships→zhiwei_identity、ttl→zhiwei_app、PUBLIC 已 REVOKE）。

Revision ID: 0027_rls_resolver
Revises: 0026_contract_dispatch_deadline
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_rls_resolver"
down_revision: str | None = "0026_contract_dispatch_deadline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESOLVER_ROLE = "zhiwei_rls_resolver"


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": _RESOLVER_ROLE}
    ).scalar()
    if not exists:
        raise RuntimeError(
            f"角色 {_RESOLVER_ROLE} 未供给：NOLOGIN BYPASSRLS 角色由 init 脚本创建"
            "（init-local-product.sh / init-test-roles.sql），并 GRANT 给 zhiwei_migrator；"
            "既有部署请以集群管理员手工补建后重试迁移"
        )
    # OWNER 转移要求新属主对函数所在 schema 有 CREATE 权限
    op.execute(f"GRANT CREATE ON SCHEMA public TO {_RESOLVER_ROLE}")
    # SECURITY DEFINER 以 owner 执行 → owner=BYPASSRLS 角色 = 函数体跨租户可读，
    # 旁路面收窄到这两个函数；表所有权不变。
    op.execute(
        f"ALTER FUNCTION public.zhiwei_principal_memberships(uuid) "
        f"OWNER TO {_RESOLVER_ROLE}"
    )
    op.execute(
        f"ALTER FUNCTION public.zhiwei_ttl_sweep_targets(timestamptz) "
        f"OWNER TO {_RESOLVER_ROLE}"
    )
    # definer=resolver 后表访问按其授权检查——恰好覆盖函数体读取的表
    for table in ("memberships", "workspace_memberships", "organizations", "memory_records"):
        op.execute(f"GRANT SELECT ON public.{table} TO {_RESOLVER_ROLE}")
    # 分库姿态（业务/identity 分离）下 membership 数据在业务库，调用方是业务数据面
    # 角色 zhiwei_app（sessions.memberships 经业务引擎调用）；单库 CI 姿态仍由
    # zhiwei_identity 调用（0003 原授权保留）——两个姿态的合法调用面并集。
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.zhiwei_principal_memberships(uuid) TO zhiwei_app"
    )
    # dispatcher 发现面授权纳入迁移流：init 脚本的 ALTER DEFAULT PRIVILEGES 只在
    # 卷初始化生效（本窗口实测 default acl 可漂移致发现查询 permission denied）。
    # 该角色仅产品姿态供给（init-local-product.sh），CI 测试栈无——条件授权。
    conn.execute(
        sa.text(
            "DO $do$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zhiwei_dispatcher') "
            "THEN GRANT SELECT ON public.outbox TO zhiwei_dispatcher; END IF; END $do$;"
        )
    )


def downgrade() -> None:
    op.execute("ALTER FUNCTION public.zhiwei_principal_memberships(uuid) OWNER TO zhiwei_migrator")
    op.execute("ALTER FUNCTION public.zhiwei_ttl_sweep_targets(timestamptz) OWNER TO zhiwei_migrator")
    op.execute("REVOKE EXECUTE ON FUNCTION public.zhiwei_principal_memberships(uuid) FROM zhiwei_app")
    for table in ("memberships", "workspace_memberships", "organizations", "memory_records"):
        op.execute(f"REVOKE SELECT ON public.{table} FROM {_RESOLVER_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {_RESOLVER_ROLE}")

