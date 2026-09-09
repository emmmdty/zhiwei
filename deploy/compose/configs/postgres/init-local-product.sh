#!/bin/bash
# S11 local-product init（specs/s11 §3）：单实例多 database——业务 / identity /
# Temporal persistence / Temporal visibility 各自独立 database 与角色。
# production reference 由 K8s overlay 指向外部托管 PG（不自建 DB operator）。
# dispatcher 角色带 BYPASSRLS：只允许 outbox 租户对发现查询（workers/main.py 契约），
# 消息载荷读取一律走 zhiwei_app 角色的 RLS 上下文。
# 口令默认值只是本地开发占位；真实部署由 operator 以 ZHIWEI_PG_PASSWORD 覆盖。
set -euo pipefail

: "${ZHIWEI_PG_PASSWORD:=zhiwei-dev-pg-only}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
  CREATE ROLE zhiwei_migrator LOGIN PASSWORD '$ZHIWEI_PG_PASSWORD';
  CREATE ROLE zhiwei_app LOGIN PASSWORD '$ZHIWEI_PG_PASSWORD';
  CREATE ROLE zhiwei_identity LOGIN PASSWORD '$ZHIWEI_PG_PASSWORD';
  CREATE ROLE zhiwei_dispatcher LOGIN BYPASSRLS PASSWORD '$ZHIWEI_PG_PASSWORD';
  CREATE ROLE zhiwei_temporal LOGIN PASSWORD '$ZHIWEI_PG_PASSWORD';
  -- membership/TTL resolver 的窄旁路角色（0027）：NOLOGIN + BYPASSRLS，SECURITY
  -- DEFINER 函数以它执行才能穿透 FORCE RLS（非超级用户 definer 被 FORCE RLS 过滤）；
  -- migrator 须是其成员（函数 OWNER 转移的前置），旁路面收窄到函数体。
  CREATE ROLE zhiwei_rls_resolver NOLOGIN BYPASSRLS;
  GRANT zhiwei_rls_resolver TO zhiwei_migrator;
  CREATE DATABASE zhiwei OWNER zhiwei_migrator;
  CREATE DATABASE zhiwei_identity OWNER zhiwei_migrator;
  CREATE DATABASE zhiwei_local_temporal OWNER zhiwei_temporal;
  CREATE DATABASE temporal_visibility OWNER zhiwei_temporal;
  GRANT zhiwei_migrator TO zhiwei_app;
  GRANT zhiwei_migrator TO zhiwei_identity;
  -- dispatcher 角色读取的表由后续 alembic 迁移创建：default privileges 让
  -- migrator 之后创建的所有表自动对 dispatcher 授 SELECT（仅发现查询用）。
  ALTER DEFAULT PRIVILEGES FOR ROLE zhiwei_migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO zhiwei_dispatcher;
  GRANT USAGE ON SCHEMA public TO zhiwei_dispatcher;
EOSQL
