#!/bin/sh
# ZhiWei OPA 授权边车入口：从仓库 policies/zhiwei 构建带固定 revision 的 bundle，
# 再以 server 模式启动。
#
# 为什么这样设计：
# - bundle 必须携带 manifest revision（client 依赖 revision 判新鲜与缓存失效），
#   `opa build --revision` 在启动前生成，构建失败则容器退出（fail closed，不带病启动）；
# - 固定镜像（openpolicyagent/opa:1.19.0-debug，busybox）没有 wget/curl/envsubst，
#   entrypoint 只依赖镜像内的 sh 与 opa 自身；健康检查由集成测试轮询
#   /health?bundles 完成，不在这里声明镜像没有的工具；
# - decision_logs.console 是 decision_id 生成的前提（OPA 只在开启 decision logging
#   时在响应里带 decision_id），input 由输入 schema 保证不含 secret；
# - OPA_BUNDLE_REVISION 与 OPA_POLICY_SRC 字符契约（与 keycloak entrypoint 同款）：
#   revision 会进 bundle manifest 与 decision log，策略路径进 opa build 参数；
#   控制字符/空值必须在构建前拒绝；tr 删除允许字符后统计剩余字节，覆盖换行/
#   CR/TAB（逐行 sed 正则做不到这一点）；
# - OPA_POLICY_SRC 可覆盖（测试用独立挂载 + 收紧策略副本模拟 policy update）。
set -eu

# `-`（非 `:-`）：只有未设置才用默认值；显式空值保留给 reject_dangerous fail closed
OPA_BUNDLE_REVISION="${OPA_BUNDLE_REVISION-s1-t3-local}"
OPA_POLICY_SRC="${OPA_POLICY_SRC-/policies/zhiwei}"
OPA_BUNDLE_OUT="${OPA_BUNDLE_OUT:-/tmp/zhiwei-bundle.tar.gz}"

# 只报变量名，不回显值（revision 不是 secret，但保持同一字符契约纪律）
reject_dangerous() {
    name=$1
    value=$2
    allowed=$3
    if [ -z "$value" ]; then
        echo "entrypoint: $name is empty (fail closed; allowed characters are [$allowed])" >&2
        exit 1
    fi
    remaining=$(printf '%s' "$value" | tr -d "$allowed" | wc -c)
    if [ "$remaining" -ne 0 ]; then
        echo "entrypoint: $name contains characters outside the allowed set (fail closed; allowed characters are [$allowed])" >&2
        exit 1
    fi
}

# revision 只允许安全字节（进 manifest/decision log）；策略路径允许 POSIX 路径字符
reject_dangerous OPA_BUNDLE_REVISION "$OPA_BUNDLE_REVISION" 'A-Za-z0-9_.:-'
reject_dangerous OPA_POLICY_SRC "$OPA_POLICY_SRC" 'A-Za-z0-9_./:-'

opa build --revision "$OPA_BUNDLE_REVISION" "$OPA_POLICY_SRC/authz.rego" -o "$OPA_BUNDLE_OUT"

exec opa run --server --addr 0.0.0.0:8181 \
    --bundle "$OPA_BUNDLE_OUT" \
    --set decision_logs.console=true
