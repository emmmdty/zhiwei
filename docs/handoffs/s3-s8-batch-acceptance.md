# S3–S8 整批验收记录（ADR-012/013 口径）

> 验收日期：2026-09-05　验收依据：整批验收以全量 Gate 为准（progress.md 2026-09-04
> 轮既定口径：「不声称逐 Task 收口」）　operator：本轮清理指令确认（会话记录）

## 1. 验收基准

- **HEAD**：`7a2c3fd`（含 S3–S8 两波入库提交与 ADR-013 补全轮，工作区干净）。
- **口径**：progress.md 2026-09-04 终态记录（pytest 3311 / evals 822 / determinism ✓ /
  ruff·pyright 0）在本轮以全新干净库复验并更新为 3312（新增 ADR-014 契约测试 1 项）。

## 2. 全量 Gate 复验（2026-09-05，均已验证）

| Gate 项 | 结果 |
| --- | --- |
| `make evals` | 822 项全过、26 个冻结资产 |
| `make determinism` | 两次干净重建逐字节一致 |
| `uv run ruff check .` / `uv run pyright` | 0 / 0 errors |
| `uv run pytest -q`（干净库单进程） | 3312 passed / 6 skipped / 20 deselected / 0 failed |
| S0 `zhiwei eval seal-empty --check` + `dev doctor` | 密封 ✓ / fixture_only 三项 ok |
| S2 `replay-check --all-fixtures` + `runtime-contract-v1 --seal` | 7/7 ✓ / 密封 7/7 |
| S1 真实 OPA slow（identity 2 + policy 16） | 全过（ADR-012 §5 显式 `-m slow`） |
| S3 `verify context --all` + `models attest` | exit 0 / 全 ok |
| S4 `provider test --all-reference --sealed` | 3/3 密封 |
| S5 `source sync` + 4 knowledge suites `--seal` | 4/4 ✓ / 15·15·12·11 密封 |
| S6 factqa-v1 120/120 + ask-v1 6/6 密封；`verify evidence` valid=0/tampered=6 | ✓ 退出码契约符合 |
| S7 enterprise-memory-v1 12/12；`eval external-status`（longmemeval） | 密封 ✓ / unavailable→`planned/unavailable` sealed（fail closed 如实） |
| S8 `risk generate --check`（D0–D6 如实：recall 0.786、hard 0.25、D5/D6 not_evaluated_offline）+ numeric-risk 22 + discover-blind 5 | ✓ 密封 |
| e2e：tenancy 13/13（**历史首次全绿**）+ runtime-approval 3/3 | 16 passed（见 s1-tenancy-e2e-repair.md） |

## 3. 状态判定（已验证）

- **S0**：收口（artifact `artifacts/gates/s0/report.md` PASS 在案）。
- **S1**：**有条件收口 → e2e 例外条目解锁关闭**（tenancy 真实栈 13/13；Keycloak leg
  转绿；S1 Gate 其余项全绿）。真实 OPA slow 测试已按 ADR-012 §5 显式纳入 Gate。
- **S2**：有条件收口 → runtime-approval e2e 例外条件已于 `6ddf29a` 关闭（本轮复验
  3/3）；OIDC leg 由 tenancy 覆盖已转绿。
- **S3–S5**：实现入库 + Gate 命令全部可执行 + 无例外条目；按整批口径不声称逐 Task
  收口，**整批验收通过**。
- **S6–S8**：同上 + e2e 例外条目（四要素齐全 + 本轮 operator 确认登记）；**整批验收
  通过（有条件项：各自 e2e）**。

## 4. 遗留与移交

- S4/S6/S7/S8 e2e 例外条目维持「有条件收口」，复执行时点最迟并入 S10 Studio Gate
  清单（operator 已确认）。
- 「计划实现」登记维持：S3 live attestation、生产 egress 组装接线、hidden reasoning
  四个持久化面测试。
- 审计文档残留口径：FINAL_AUDIT_REPORT.md L-4 行未同步 progress.md 更正（代码现状
  以 progress.md 为准）；PHASE1_AUDIT_REPORT.md Round 1.4 状态未回写——不阻塞，
  已在此登记。
