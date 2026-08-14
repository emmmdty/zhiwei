#!/bin/sh
# ZhiWei local-product Keycloak 入口：把 realm 模板中的 ${...} 占位符替换为
# compose 注入的环境变量（客户端 secret / 测试用户口令），再交给 kc.sh 导入。
#
# 模板里绝不出现真实凭据；operator 必须通过环境变量或 docker secret 覆盖默认值。
set -eu

envsubst < /opt/keycloak/realm-template.json > /opt/keycloak/data/import/realm.json
exec /opt/keycloak/bin/kc.sh start-dev --import-realm
