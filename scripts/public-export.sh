#!/usr/bin/env bash
# 公开导出（EXPORT_MANIFEST 驱动）——发布 = 重跑本脚本，可复现、无手工挑选。
#
# 用法：
#   scripts/public-export.sh            # 构建导出树 + 全部断言，dry-run（默认不推送）
#   scripts/public-export.sh --push     # 构建通过断言后，force-push orphan 分支为远端 main
#
# 机制：按 manifest include/exclude 在临时目录装配导出树 → 运行泄漏/边界断言 →
#       以 plumbing 生成 orphan commit（parent 为空，不携带本地过程历史）→ 更新
#       refs/heads/public-main。远端只保留该孤儿历史；本地 main 全量历史永不推送。
set -euo pipefail
shopt -s globstar nullglob
cd "$(dirname "$0")/.."

MANIFEST="EXPORT_MANIFEST"
EXPORT_DIR="$(mktemp -d /tmp/opencode/public-export.XXXXXX)"
trap 'rm -rf "$EXPORT_DIR"' EXIT
PUSH=0
[[ "${1:-}" == "--push" ]] && PUSH=1

command -v rsync >/dev/null || { echo "需要 rsync" >&2; exit 1; }

# ---- 1. 装配导出树（globstar 展开模式，nullglob 下空匹配安全跳过）----
included=0
while IFS= read -r raw; do
  line="${raw%%#*}"
  # 去首尾空白
  line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && continue
  action="${line%% *}"; glob="${line#* }"
  case "$action" in
    include)
      # 未引用展开是刻意的：让 globstar/nullglob 消费 manifest 里的 glob
      matches=($glob)
      if [[ ${#matches[@]} -eq 0 ]]; then
        echo "[export] 注意：include 未命中（Phase 产物未生成即属正常）: $glob"
        continue
      fi
      rsync -aR -- "${matches[@]}" "$EXPORT_DIR/"
      included=$((included + ${#matches[@]}))
      ;;
    exclude)
      matches=($EXPORT_DIR/$glob)
      # 剔除目录本身（glob 以 /** 结尾时含目录壳），否则只删匹配项
      if [[ ${#matches[@]} -gt 0 ]]; then rm -rf "${matches[@]}"; fi
      ;;
    *) echo "[export] 未知动作: $action（行: $raw）" >&2; exit 1 ;;
  esac
done < "$MANIFEST"
[[ $included -gt 0 ]] || { echo "[export] manifest 未命中任何 include" >&2; exit 1; }

# ---- 1.5 声明块渲染：README 的 {{claim:ID}} marker 经 claim registry 填充 ----
# 导出面发布真实值（repo 内 README 保留 marker 供 release check 扫描）。
# fail closed：registry 不可用 / 存在非 verified claim / 口径漂移一律中止导出
# （宁可拒绝发版，也不发布无 artifact 支撑或口径过期的数字）。
fail() { echo "[export] 断言失败: $1" >&2; exit 1; }

if [[ -z "${ZHIWEI_DATABASE_URL:-}" ]]; then
  fail "导出需要 ZHIWEI_DATABASE_URL（maintenance DSN）渲染 README 声明块"
fi
uv run zhiwei release render --paths README.md --output "$EXPORT_DIR/README.md"
echo "[export] README 声明块已按 claim registry 渲染"

# ---- 2. 断言：公开边界（防手工遗漏/防泄漏，失败即中止）----

# 2a. 过程面必须缺席
for p in docs/handoffs docs/review docs/superpowers docs/DEV_ALLOCATION.md \
         docs/PORTFOLIO_NARRATIVE.md docs/ROADMAP.md docs/BENCHMARK.md \
         docs/EXPERIMENTS.md docs/RISK_EVAL.md docs/FINAL_AUDIT_REPORT.md \
         docs/PHASE1_AUDIT_REPORT.md docs/IMPLEMENTATION_FEASIBILITY_AUDIT_1.2C.md \
         data CLAUDE.md findings.md progress.md task_plan.md spikes; do
  [[ -e "$EXPORT_DIR/$p" ]] && fail "$p 属过程面，不得出现在导出树"
done

# 2b. 产品面必须存在（导出不完整比过宽更危险）
for p in README.md LICENSE AGENTS.md pyproject.toml uv.lock Makefile Dockerfile \
         alembic.ini .env.example .gitignore .dockerignore EXPORT_MANIFEST \
         src/zhiwei tests evals deploy/compose/compose.yaml deploy/compose/compose.test.yaml \
         config policies solution-packs migrations demo artifacts/gates \
         .github/workflows/ci.yml docs/API.md docs/DECISIONS.md docs/adr; do
  [[ -e "$EXPORT_DIR/$p" ]] || fail "产品面缺失: $p"
done

# 2c. 泄漏断言：key 形态 / 已知内部主机 / .env 实值
if grep -rInE "sk-[A-Za-z0-9]{20,}" "$EXPORT_DIR" >/dev/null 2>&1; then
  grep -rIlE "sk-[A-Za-z0-9]{20,}" "$EXPORT_DIR" | head -5 >&2
  fail "导出树含疑似 API key（sk-…）"
fi
# 内部主机黑名单用拼接书写——本脚本自身会被导出，整段字面量会自匹配导致断言假阳性
for host in "113.46.""219" "csi-""providor" "csi-""ai"; do
  if grep -rIn "$host" "$EXPORT_DIR" >/dev/null 2>&1; then fail "导出树含内部主机引用: $host"; fi
done
if grep -rInE "^OPENAI_API_KEY=.+" "$EXPORT_DIR/.env.example" >/dev/null 2>&1; then
  fail ".env.example 含非空 key"
fi
[[ -e "$EXPORT_DIR/.env" ]] && fail ".env 不得出现在导出树"

# ---- 3. orphan commit（plumbing，不触碰工作区）----
SRC_HEAD="$(git rev-parse HEAD)"
export GIT_INDEX_FILE="$EXPORT_DIR/.git-export-index"
git read-tree --empty
( cd "$EXPORT_DIR" && find . -type f -not -name '.git-export-index' | sed 's|^\./||' ) |
while IFS= read -r f; do
  git update-index --add --cacheinfo "100644,$(git hash-object -w "$EXPORT_DIR/$f"),$f"
done
TREE="$(git write-tree)"
COMMIT="$(git commit-tree "$TREE" -m "公开导出：产品面快照（源 ${SRC_HEAD:0:12}，EXPORT_MANIFEST 驱动）

由 scripts/public-export.sh 生成：orphan 历史，过程面本地保留。
断言清单见 EXPORT_MANIFEST 与本脚本 §2。")"
git update-ref refs/heads/public-main "$COMMIT"
unset GIT_INDEX_FILE

COUNT="$(find "$EXPORT_DIR" -type f | wc -l)"
echo "[export] 完成：public-main @ ${COMMIT:0:12}（$COUNT 个文件，源 ${SRC_HEAD:0:12}）"
if [[ "$PUSH" -eq 1 ]]; then
  git push origin public-main:main --force
  echo "[export] 已 force-push public-main → origin/main（远端只保留公开孤儿历史）"
else
  echo "[export] dry-run：未推送。确认后执行 scripts/public-export.sh --push"
fi
