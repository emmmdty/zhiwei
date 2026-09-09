\set ON_ERROR_STOP on

CREATE ROLE zhiwei_app
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOBYPASSRLS;

-- S1-T2：identity-global 独立角色（auth_sessions / oidc_login_attempts /
-- secret_envelopes / principals / external_identities），权限由 0003 迁移授予。
CREATE ROLE zhiwei_identity
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOBYPASSRLS;

GRANT CONNECT ON DATABASE zhiwei_test TO zhiwei_app;
GRANT USAGE ON SCHEMA public TO zhiwei_app;
GRANT CONNECT ON DATABASE zhiwei_test TO zhiwei_identity;
GRANT USAGE ON SCHEMA public TO zhiwei_identity;

-- 0027_rls_resolver：membership/TTL resolver 的窄旁路角色（NOLOGIN + BYPASSRLS，
-- dispatcher 先例的函数级收窄版）；migrator 须是其成员（函数 OWNER 转移前置）。
CREATE ROLE zhiwei_rls_resolver NOLOGIN BYPASSRLS;
GRANT zhiwei_rls_resolver TO zhiwei_migrator;
