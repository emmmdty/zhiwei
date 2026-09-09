#!/usr/bin/env bash
set -euo pipefail
D=artifacts/gates/s1-t4
OUT=$D/gate-raw.txt
: > "$OUT"
{
  echo "# S1-T4 四轮修复重建（RED history repair）— Gate Raw Output"
  echo "# 候选分支：repair/s1-t4-red-rebuild"
  echo "# 新 RED（HANDOFF_BASE）：2dcb1d5　GREEN：2e0fe7a　REVIEW：bfc3f12　docs 修正：534d30b"
  echo "# 每条命令保存真实原始输出与 exit code（set -o pipefail 防 tee 吞失败码）"
  echo ""
  echo "========================================================================"
  echo "== 1. RED 失败证据（干净 0007，期望失败：缺持久 claim/原子围栏）"
  echo "========================================================================"
  echo ""
  echo "### 1.1 uv run pytest tests/integration/policy/test_bootstrap_claim_db_contract.py -q（新 RED）"
  cat $D/red/db-contract.stdout
  echo ""
  echo "### 1.2 uv run pytest tests/integration/policy/test_opa_bootstrap_slow.py -q -m slow（新 RED）"
  cat $D/red/opa-slow.stdout
  echo ""
  echo "========================================================================"
  echo "== 2. GREEN 证据（候选分支 HEAD = 534d30b，功能 HEAD 534d30b）"
  echo "========================================================================"
} >> "$OUT"
for f in 01-lock 02-db-contract 03-opa-slow 04-unit 05-integration 06-full 07-full-slow \
         08-ruff 09-pyright 10-11-alembic 10b-downgrade-upgrade-clean 12-compose \
         13-evals 14-determinism 15-16-17-handoff; do
  {
    echo ""
    echo "========================================================================"
    echo "== GREEN：$f"
    echo "========================================================================"
    cat "$D/green/$f.txt"
  } >> "$OUT"
done
echo "gate-raw.txt written: $(wc -c < "$OUT") bytes"
