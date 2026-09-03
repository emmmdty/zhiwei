# S2 修复轮 Gate Artifact（2026-09-03）

> 基线 commit: `aa88313`（RED 血统）；GREEN commit: `33eee3a`。
> 验证环境：WSL2, PG 17.6@55432, Python 3.11.15, OPA 127.0.0.1:8181, redis-server 7.2.5
> (in /tmp/opencode/redis-build/redis-7.2.5/src/redis-server), Temporal WorkflowEnvironment.

## Gate 输出

### pytest（完整套件）

```
uv run pytest -q
1214 passed, 20 deselected, 1 warning
```

20 deselected：16 个 OPA slow/identity profile + 2 个 SCIM slow + 2 个 deploy。All batch A/B/C 修复测试
在默认选择内运行。D-1/GATE-SLOW 等待 docker 编排时通过 Gate 例外登记。

### ruff / pyright

```
uv run ruff check .            → All checks passed
uv run pyright                 → 0 errors, 0 warnings, 0 informations
```

### evals / determinism

```
make evals      → [validate] 110 项校验全部通过
make determinism → [determinism] ✓ 两次干净重建产物逐字节一致
```

### replay-check / eval seal

```
ZHIWEI_DATABASE_URL=... ZHIWEI_OBJECT_STORE_ROOT=/tmp/opencode/eval-objects \
  uv run zhiwei runtime replay-check --all-fixtures
→ {"status": "passed", "fixture_count": 7, "results": [
  {"label": "runtime/graph/basic-lifecycle", "deterministic": true, "terminal": true, "chain_verified": true},
  {"label": "runtime/graph/parallel-merge-order", "deterministic": true, "terminal": true, "chain_verified": true},
  {"label": "runtime/graph/retry-on-failure", "deterministic": true, "terminal": true, "chain_verified": true},
  {"label": "runtime/graph/dependency-failure-skip", "deterministic": true, "terminal": true, "chain_verified": true},
  {"label": "runtime/graph/duplicate-signal-cancel", "deterministic": true, "terminal": true, "chain_verified": true},
  {"label": "runtime/graph/continue-as-new", "deterministic": true, "terminal": true, "chain_verified": true},
  {"label": "runtime/merge/conflict-preserving", "deterministic": true, "terminal": true, "chain_verified": true}
]}

ZHIWEI_DATABASE_URL=... ZHIWEI_OBJECT_STORE_ROOT=/tmp/opencode/eval-objects \
  uv run zhiwei eval run --suite runtime-contract-v1 --mode fixture --seal
→ {"suite": "runtime-contract-v1", "mode": "fixture", "registered_units": 7, "terminal_units": 7,
   "status_counts": {"completed": 7}, "sealed": true,
   "seal_digest": "sha256:ee457f9a..."}
```

### handoff-check

```
make handoff-check HANDOFF_BASE=aa88313
[handoff] ✓ 锁定测试与 evals/ 相对 aa88313 未漂移
```

## 测试修订 ripple 记录（GREEN 阶段合法修订）

按纪律，以下测试文件在 GREEN 期产生了合法 ripple 修订（RED 血统 commit `aa88313`）：

| 文件 | 原因 |
|------|------|
| `tests/integration/temporal/test_agent_run.py` | pause 测试改轮询；conflict 测试换 ConflictFixtureHandler；batch C 新增 7 个测试 |
| `tests/integration/temporal/conftest.py` | 新增 EffectUnknownFixtureHandler |
| `tests/integration/foundation/test_canonical_value_domain.py` | 新增（S0 JSONB 值域守卫测试） |
| `tests/contract/solution_packs/test_core_boundary.py` | 新增（架构测试重写） |

以上全部在 GREEN 提交前以 RED 血统 commit（`29f7be3`/`aa88313`）落盘。GREEN commit
`33eee3a` 零 tests/ 改动，handoff-check 验证通过。
