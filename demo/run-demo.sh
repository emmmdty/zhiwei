#!/usr/bin/env bash
# 终端演示脚本（S11 followup-2 任务四）：三条可独立运行的旅程，各 ≤2 分钟。
#
# 用法：bash demo/run-demo.sh <1|2|3>
#   1  契约测试      identity/RLS 契约套件（FORCE RLS / bypass resolver 契约）
#   2  故障注入      temporal_restart + opa_down_fail_closed（compose 后端，seal 落盘）
#   3  写动作治理    未审批写逐字拒绝 → SoD 审批 → 完整生命周期 receipt
#
# 实测计时（2026-09-09，本机 8 核 / compose 栈 healthy 口径；任何一段 >2 分钟
# 必须裁剪范围，不许放宽断言——docs/handoffs/s11-followup-2-publish-demo.md §4）：
#   旅程 1：81 passed in 24.5s
#   旅程 2：temporal_restart 11.1s（recover 9207ms）+ opa_down_fail_closed
#           8.8s（fail_closed 7336ms）≈ 20s
#   旅程 3：4 passed in 1.2s
#
# 边界：release_mode=fixture_only，三条旅程都不调用 live 模型；每段结束打印
# 可核验证据锚点（run id / digest / 退出码）。
# 前提：docs/operations/install.md 安装序列完成（产品栈 healthy；测试栈
# postgres 55432 + OPA 8181 运行中——旅程 1/3 的 pytest 自带 conftest 依赖）。
set -euo pipefail
cd "$(dirname "$0")/.."

export ZHIWEI_PROFILE=local_product
export ZHIWEI_RELEASE_MODE=fixture_only
export ZHIWEI_DATABASE_URL="${ZHIWEI_DATABASE_URL:-postgresql+asyncpg://zhiwei_app:zhiwei-dev-pg-only@127.0.0.1:55433/zhiwei}"
export ZHIWEI_OBJECT_STORE_ROOT="${ZHIWEI_OBJECT_STORE_ROOT:-/tmp/zhiwei-demo-objects}"
mkdir -p "$ZHIWEI_OBJECT_STORE_ROOT"

usage() {
  cat <<'EOF'
用法: bash demo/run-demo.sh <1|2|3>

  1  契约测试    tests/security/identity（81 条：FORCE RLS / bypass resolver /
                 session 密封契约）
  2  故障注入    zhiwei ops fault-run --scenario temporal_restart + opa_down_fail_closed
                 （compose 后端真实栈；seal 产物落盘即证据）
  3  写动作治理  未审批写 409 逐字拒绝 → SoD 审批（自批 409 / 他人批准 / 重放 409）
                 → 完整生命周期 ActionReceipt
EOF
}

journey_1() {
  echo "=== [demo 1/3] 契约测试：identity / RLS（FORCE RLS 与 bypass resolver 契约）==="
  uv run pytest tests/security/identity -q -m ""
  echo "证据锚点：81 条契约全绿（退出码 $?）。"
  echo "  - tests/security/identity/test_rls_resolver.py：resolver 角色属 bypass 角色、"
  echo "    NOLOGIN BYPASSRLS、授权面恰为函数读（FORCE RLS 不可旁路）"
  echo "  - tests/security/identity/test_identity_roles.py：角色词汇与归一契约"
}

journey_2() {
  echo "=== [demo 2/3] 故障注入：temporal_restart + opa_down_fail_closed（compose）==="
  local seal_dir="/tmp/zhiwei-demo-seals/fault-$(date +%s)"
  mkdir -p "$seal_dir"
  uv run zhiwei ops fault-run --profile local-product --scenario temporal_restart \
    --sealed --seal-dir "$seal_dir"
  uv run zhiwei ops fault-run --profile local-product --scenario opa_down_fail_closed \
    --sealed --seal-dir "$seal_dir"
  echo "证据锚点：区别性终态 recover（temporal 重启恢复 healthy）与 fail_closed"
  echo "（OPA 不可用即拒绝，不降级放行）；seal 产物落盘："
  sha256sum "$seal_dir"/*
  echo "（seal 载荷含 raw events / environment / recovery_time_ms，可独立复核）"
}

journey_3() {
  echo "=== [demo 3/3] 写动作治理：审批门禁最小闭环（创建→待审批→批准→receipt）==="
  uv run pytest \
    'tests/integration/discover/test_discover_api.py::TestGatedActionSubmission' \
    'tests/unit/discover/test_actions.py::TestActionManager::test_full_lifecycle' \
    -q -m ""
  echo "证据锚点：4 条全绿（退出码 $?）。"
  echo "  - 未审批写动作：request 落账 pending_approval，执行被逐字拒绝"
  echo "   （detail = \"action requires human approval before execution\"），重放不复制"
  echo "  - SoD：requester 本人批准 409，他人批准 approved，重复批准 409"
  echo "  - 完整生命周期：proposed → pending → approved → ActionReceipt（不可变执行回执）"
}

case "${1:-}" in
  1) journey_1 ;;
  2) journey_2 ;;
  3) journey_3 ;;
  *) usage; exit 2 ;;
esac

echo
echo "=== 旅程 ${1} 完成（fixture only，未发出任何 live 模型请求）==="
echo "manifest: demo/manifest.json（旅程 → 测试/命令锚点 → 验收口径）"
