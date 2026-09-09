#!/bin/sh
# ZhiWei local-product Keycloak 入口：把 realm 模板中的 ${...} 占位符替换为
# compose 注入的环境变量（客户端 secret / 测试用户口令），再交给 kc.sh 导入。
#
# 模板里绝不出现真实凭据；operator 必须通过环境变量或 docker secret 覆盖默认值。
# 固定镜像没有 gettext 工具，模板替换用镜像内置 sed 完成（验收阻断 3）。
#
# realm 注入字符契约（验收阻断 4 / 验收修订 5，fail closed）：
# - 注入值只允许 [A-Za-z0-9-_.:@/&+%]：双引号 / 反斜杠 / 控制字符（换行/CR/TAB）/
#   空值会破坏 JSON 结构或 sed 替换，必须在写文件前拒绝（容器退出，不写 realm.json）；
# - 换行/CR 检测不依赖逐行 sed/grep（sed 按行处理会把换行当行分隔符漏检）：
#   tr 删除允许字符后统计剩余字节数，覆盖整个 shell value；
# - `/` 与 `&` 是 JSON 合法字符，通过非 `/` 分隔符与 sed 替换串转义正确处理；
# - 渲染先写受控临时文件，sed 成功后再原子移动到 realm.json；失败清理临时文件，
#   绝不让最终输出文件先被创建/截断；
# - 校验失败的消息只报变量名，绝不回显注入值（日志不泄露 secret）。
#
# 路径可用 ZHIWEI_KC_* 环境变量覆盖（本地渲染测试用；容器内使用默认值）。
set -eu

import_dir=${ZHIWEI_KC_IMPORT_DIR:-/opt/keycloak/data/import}
realm_template=${ZHIWEI_KC_REALM_TEMPLATE:-/opt/keycloak/realm-template.json}
realm_output=${ZHIWEI_KC_REALM_OUTPUT:-"$import_dir/realm.json"}
kc_launcher=${ZHIWEI_KC_LAUNCHER:-/opt/keycloak/bin/kc.sh}
render_only=${ZHIWEI_KC_RENDER_ONLY:-0}

# 拒绝注入值中的 JSON/sed 危险字符；只报变量名，不回显值。
# tr 删除允许字符后剩余字节数非零即含非法字符：换行/CR/TAB 不在允许集内，
# 作为字节保留下来被统计到——逐行 sed 正则无法做到这一点（换行是行分隔符）。
reject_dangerous() {
    name=$1
    value=$2
    if [ -z "$value" ]; then
        echo "entrypoint: $name is empty (fail closed; allowed characters are [A-Za-z0-9-_.:@/&+%])" >&2
        exit 1
    fi
    remaining=$(printf '%s' "$value" | tr -d '[A-Za-z0-9_.:@/&+%\-]' | wc -c)
    if [ "$remaining" -ne 0 ]; then
        echo "entrypoint: $name contains characters outside the allowed set (fail closed; allowed characters are [A-Za-z0-9-_.:@/&+%])" >&2
        exit 1
    fi
}

# sed 替换串转义：\ 与 & 在替换串里有特殊含义（分隔符用 |，/ 不需要转义）
escape_sed() {
    printf '%s' "$1" | sed -e 's/[\\&]/\\&/g'
}

mkdir -p "$import_dir"
for name in KEYCLOAK_TEST_CLIENT_SECRET KEYCLOAK_TEST_USER_PASSWORD; do
    eval "value=\${$name:-}"
    reject_dangerous "$name" "$value"
done

client_secret=$(escape_sed "${KEYCLOAK_TEST_CLIENT_SECRET}")
user_password=$(escape_sed "${KEYCLOAK_TEST_USER_PASSWORD}")

# 先渲染到受控临时文件（与最终输出同目录，保证原子重命名）；任何失败由 trap
# 清理临时文件，最终 realm.json 不被创建/截断。
realm_tmp="$realm_output.tmp.$$"
trap 'rm -f "$realm_tmp"' EXIT HUP INT TERM

sed -e "s|\${KEYCLOAK_TEST_CLIENT_SECRET}|${client_secret}|g" \
    -e "s|\${KEYCLOAK_TEST_USER_PASSWORD}|${user_password}|g" \
    "$realm_template" > "$realm_tmp"
mv "$realm_tmp" "$realm_output"

if [ "$render_only" = "1" ]; then
    exit 0
fi
exec "$kc_launcher" start-dev --import-realm
