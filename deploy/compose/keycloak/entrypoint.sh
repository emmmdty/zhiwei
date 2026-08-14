#!/bin/sh
# ZhiWei local-product Keycloak 入口：把 realm 模板中的 ${...} 占位符替换为
# compose 注入的环境变量（客户端 secret / 测试用户口令），再交给 kc.sh 导入。
#
# 模板里绝不出现真实凭据；operator 必须通过环境变量或 docker secret 覆盖默认值。
# 固定镜像没有 gettext 工具，模板替换用镜像内置 sed 完成（验收阻断 3）。
set -eu

import_dir=/opt/keycloak/data/import
mkdir -p "$import_dir"

# sed 替换值转义：\ 与 & 在替换串里有特殊含义，先转义避免注入
escape_sed() {
    printf '%s' "$1" | sed -e 's/[\\&]/\\&/g'
}

client_secret=$(escape_sed "${KEYCLOAK_TEST_CLIENT_SECRET:-}")
user_password=$(escape_sed "${KEYCLOAK_TEST_USER_PASSWORD:-}")

sed -e "s/\${KEYCLOAK_TEST_CLIENT_SECRET}/${client_secret}/g" \
    -e "s/\${KEYCLOAK_TEST_USER_PASSWORD}/${user_password}/g" \
    /opt/keycloak/realm-template.json > "$import_dir/realm.json"

exec /opt/keycloak/bin/kc.sh start-dev --import-realm
