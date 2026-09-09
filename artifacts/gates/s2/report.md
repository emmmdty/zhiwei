# S2 Gate Report（2026-09-03 修复轮 + 独立复审后复跑）

环境：WSL2 / PG 17.6 @127.0.0.1:55432 / Python 3.11.15 / temporalio 1.32.0（进程内 dev server）

## S2 Gate（specs/s2-agent-runtime.md §7）

```text
$ uv run pytest tests/unit/runtime tests/contract/task_graph -q
.......................................                                  [100%]
183 passed in 0.58s

$ uv run pytest tests/integration/runtime tests/integration/temporal -q
.....................                                                    [100%]
21 passed in 71.26s (0:01:11)

$ uv run pytest tests/security/runtime_isolation -q
.                                                                        [100%]
1 passed in 6.43s

$ uv run zhiwei runtime replay-check --all-fixtures
status: passed | fixtures: 7
  runtime/graph/basic-lifecycle deterministic= True terminal= True chain= True outcome= completed
  runtime/graph/parallel-merge-order deterministic= True terminal= True chain= True outcome= completed
  runtime/graph/retry-on-failure deterministic= True terminal= True chain= True outcome= completed
  runtime/graph/dependency-failure-skip deterministic= True terminal= True chain= True outcome= completed
  runtime/graph/duplicate-signal-cancel deterministic= True terminal= True chain= True outcome= completed
  runtime/graph/continue-as-new deterministic= True terminal= True chain= True outcome= completed
  runtime/merge/conflict-preserving deterministic= True terminal= True chain= True outcome= completed

$ uv run zhiwei eval run --suite runtime-contract-v1 --mode fixture --seal
{"suite": "runtime-contract-v1", "mode": "fixture", "executor": "agent-runtime", "registered_units": 7, "terminal_units": 7, "status_counts": {"completed": 7}, "eval_run_id": "92e30034-caf3-4c93-8f17-be0ad4d4eb86", "organization_id": "d8b0f568-291d-42f9-be02-957fdd1c54e6", "workspace_id": "a345583a-5f4f-4b92-8b73-7f6173494db3", "sealed": true, "seal_digest": "sha256:914b824aa13a33261be5ac6805b3900213323e4961bae190b7f297e9e35735cb"}
```

## 全仓回归

```text
$ uv run pytest -q
1097 passed, 20 deselected, 1 warning in 156.46s (0:02:36)

$ uv run ruff check .
All checks passed!

$ uv run pyright
0 errors, 0 warnings, 0 informations

$ make evals
[validate] 110 项校验全部通过

$ make determinism
[checksums] 21 个产物 → evals/CHECKSUMS.sha256
```

## 未执行的 Gate 项

- `npm --prefix apps/web run test:e2e -- runtime-approval.spec.ts`：环境阻塞（本 WSL 无 docker；
  Keycloak/OPA 容器不可用）+ S1 遗留 OIDC redirect_uri 缺陷（docs/handoffs/s1-t6.md §5-1）。
  runtime-approval.spec.ts 尚未编写（T7 遗留，见 docs/handoffs/s2.md §6.3）。
