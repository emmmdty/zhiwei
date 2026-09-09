# S1-T4 四轮修复重建 — Gate Report（bootstrap fencing，RED history repair）

> 日期：2026-08-16
> 候选分支：`repair/s1-t4-red-rebuild`（未移动原 `feat/s1-t4-rls-audit`，等 operator 批准）
> **新 RED（handoff base）：`2dcb1d5`**　GREEN：`2e0fe7a`（= 02059bc 内容）　REVIEW：`bfc3f12`（= f92bead 内容）　docs 修正：`534d30b`
> 功能 HEAD：`534d30b4725c85f210a68531e9eb1cec5013f7db`（docs 修正提交，Gate 结果基于它；handoff commit 不改变本报告结论）
> 状态：工作树干净；RED 在干净 0007 上到达真实行为反例；GREEN 全绿；1 轮独立只读审查无 blocking。

## 判定

**PASS（已验证）**。被拒 RED `6cf854f` 的机制缺陷已修复并重建：新 RED `2dcb1d5`
在干净 0007 上不被缺表清理逻辑截断，slow 纵切实际到达真实行为反例（并发不同
target `[201, 201]`、自移除后 create B 实际 201、业务成功但缺持久 claim、malformed
scope backfill DID NOT RAISE）；GREEN `2e0fe7a` + REVIEW `bfc3f12`（与原 02059bc /
f92bead 逐字节同内容，patch-id 实证）实现持久 bootstrap claim 围栏并全部反转；
docs 修正 `534d30b` 只改注释事实。

## 重建背景（为什么 T4 被拒）

原 RED `6cf854f` 的 `_reset_alice` / bob 清理无条件 `DELETE FROM
organization_bootstrap_claims`——RED 数据库停在 0007 时该表不存在，所有 slow 用例在
fixture 阶段被 UndefinedTableError 截断，永远到不了真实行为反例；另有
`test_identity_domain` 恒真断言（dict 键是 `(principal, org)` 二元组，`org_b` 单值恒
不在其中）；且缺少 malformed bootstrap scope 迁移测试。

## 提交序列（候选分支，原分支未移动）

| Commit | 类型 | 内容 |
| --- | --- | --- |
| `2dcb1d5` | **RED** | 重建四轮 RED（相对 6cf854f 仅 5 处机制修订：`_delete_bootstrap_claims_if_exists` 窄 helper 判表存在性（slow + rls 两文件）、invited-member/multi-org bob 清理复用同一 helper、恒真断言修正为 `(owner.id, org_b) not in memberships`、新增 malformed scope 迁移测试、'无 org/ws 列'→'无 org/ws 租户作用域语义'注释） |
| `2e0fe7a` | **GREEN** | 0008 迁移（表+窄函数+可验证 backfill fail-closed）+ ORM + repository + command + router 403 映射（与 02059bc 内容逐字节一致） |
| `bfc3f12` | refactor | 保留 BootstrapClaimConflict 异常链（`from error`）；恢复误删空行（与 f92bead 一致） |
| `534d30b` | docs | 只修正注释/文档事实：'无 org/ws 租户作用域语义'（models.py、0008 docstring）、claim conflict 注释统一为 '异常退出 tenant_session 后整体回滚'（api/commands/domain/repositories）；ON DELETE CASCADE 保持未来裁决 |

## 1. RED 失败证据（干净 0007，真实反例，非 fixture/Docker/网络）

### 1.1 新 RED DB contract：9 failed（缺表/缺函数/backfill 缺失）

```
UndefinedFunctionError: function public.zhiwei_claim_organization_bootstrap(unknown, unknown) does not exist
UndefinedTableError: relation "organization_bootstrap_claims" does not exist ×4
AssertionError: 窄函数必须存在  (assert None is not None)
Failed: DID NOT RAISE RuntimeError   ← ambiguous backfill（无 0008 → 不触发）
Failed: DID NOT RAISE RuntimeError   ← malformed scope backfill（新增用例，同样反例）
assert 0 == 1                        ← downgrade 后 upgrade 表仍不存在
```

### 1.2 新 RED real-OPA slow：5 failed（8 passed）

```
AssertionError: 并发不同 target 的 bootstrap 必须恰好一个 201 一个 403，实际 [201, 201]
assert 201 == 403                     ← 自移除后 create B 仍 201（资格未被持久化）
UndefinedTableError: organization_bootstrap_claims ×3
  ① invited-member：业务创建 201 已成功 → claim 读点失败（业务成功但缺持久 claim）
  ② audit 回滚：500 后四表零残留断言全过 → claim 断言点失败
  ③ OPA deny：policy denied 已成立 → claim 零行断言点失败
```

8 + 5 项失败全部落在「缺少持久 claim/原子围栏」，`_reset_alice` 不再截断任何用例。
完整 stdout/stderr 见同目录 `red/db-contract.stdout` 与 `red/opa-slow.stdout`。

## 2. GREEN 反转证据

- DB contract 9/9 全绿（含新增 malformed scope 反例反转：RuntimeError unparseable
  owner principal + transactional DDL 整体回滚）；
- slow 纵切 13/13 全绿，四轮 5 项 RED→GREEN 直接反转；
- 锁定测试零漂移：`git diff 2dcb1d5..HEAD -- tests/` 与 `-- evals/` 均为空；
  `make handoff-check HANDOFF_BASE=2dcb1d5` exit 0。

## 3. Gate 逐项结果（功能 HEAD `534d30b` 新鲜输出，原始输出见 gate-raw.txt）

| 命令 | 结果 |
| --- | --- |
| `uv lock --check` | exit 0；Resolved 127 packages OK |
| 新 RED DB contract | exit 1；**9 failed**（预期反例，见 §1.1） |
| 新 RED real-OPA slow | exit 1；**5 failed**（预期反例，见 §1.2） |
| `uv run pytest tests/integration/policy/test_bootstrap_claim_db_contract.py -q`（GREEN） | exit 0；**9 passed** |
| `uv run pytest tests/integration/policy/test_opa_bootstrap_slow.py -q -m slow`（GREEN） | exit 0；**13 passed** |
| `uv run pytest tests/unit/identity tests/unit/policy -q` | exit 0；229 passed |
| `uv run pytest tests/integration/identity tests/integration/rls tests/security/tenancy -q` | exit 0；160 passed |
| `uv run pytest -q` | exit 0；**804 passed, 18 deselected** |
| `uv run pytest -q -m slow` | exit 0；**18 passed, 804 deselected**（单次全绿，Keycloak 竞态未命中） |
| `uv run ruff check .` | exit 0；All checks passed |
| `uv run pyright` | exit 0；0 errors, 0 warnings |
| `uv run alembic upgrade head` | exit 0 |
| `uv run alembic check` | exit 0；No new upgrade operations detected |
| `uv run alembic downgrade base && alembic upgrade head` | exit 0（全链干净重建） |
| `docker compose -f deploy/compose/compose.test.yaml --profile identity config --quiet` | exit 0 |
| `make evals` | exit 0；110 项校验全部通过 |
| `make determinism` | exit 0；两次干净重建逐字节一致 |
| `make handoff-check HANDOFF_BASE=2dcb1d5` | exit 0；锁定测试与 evals/ 零漂移 |
| `git diff --check 2dcb1d5..HEAD` | exit 0 |
| `git status --short` | 空（干净） |

### 已知环境观察（非代码缺陷，登记备查）

- 慢速套件各用例为同一 principal 累积 `organization.create:<principal>` 幂等记录
  （既有行为，跨用例/跨次运行持久）。在残留数据状态下手动 `downgrade 0007 →
  upgrade head` 时，0008 backfill 按设计 fail closed：
  `RuntimeError: ambiguous bootstrap history: ... refusing to backfill`——这是
  fail-closed 的真实运行实证（原始输出 `green/10b-downgrade-upgrade.txt`）；Gate 用
  干净全链重建（downgrade base → upgrade head）作为可逆性证据（exit 0）。
- 既有 Keycloak 容器竞态（三轮已登记）本轮全套 slow 单次运行未命中，不修改 T2 测试。

## 4. 环境指纹

| 组件 | 指纹 |
| --- | --- |
| PostgreSQL | `postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94`（compose 未 pin digest，浮动 tag 17.6-alpine） |
| OPA | `openpolicyagent/opa:1.19.0-debug@sha256:ec3c7a29a21ce96d71231cb4befa2561205fe84e5a2dc3cc46ac7bc8bd21b3a4`（compose pin） |
| Keycloak | `quay.io/keycloak/keycloak:26.1@sha256:be6a86215213145bfb4fb3e2b3ab982a806d00262655abdcf3ffa6a38d241c7c`（compose pin） |

## 5. 并发/生命周期矩阵（migrator 直读断言，green/02、green/03 全绿）

| 场景 | 结果 | claim | org | owner membership | audit | outbox | idempotency |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 并发不同 target（HTTP + DB CAS） | **201+403 / true+false** | 1 | winner 1、loser 0 | 1 | 1、0 | 1、0 | 1、0 |
| 自移除后 create B | **403** claim conflict | 1（仍指向 A） | B 0 | 0 | 0 | 0 | 0 |
| 自移除后重放 A 原 key/body | **200** | 1 | A 1 | 0 | organization.create 计数 1 | — | 1 |
| 受邀成员（从未 bootstrap）移出后建自有 org | **201** | 1（指向新 org） | 1 | 1 | 1 | 1 | 1 |
| audit 写失败 → 不同 target 重试 | **500 → 201** | 0 → 1 | 0 → 1 | 0 → 1 | 0 → 1 | 0 → 1 | 0 → 1 |
| OPA deny（已有 active org 建不同 target） | **403** policy denied | 0 | 0 | 0 | 0 | 0 | 0 |
| 预置异 digest claim（改名后用例） | **409** idempotency_conflict | — | 1 | 1 | failed 1 | 1 | 1 |

## 6. 迁移实验证据

- base→head 全链安装 0008（fixture 持续验证 + 干净全链重建 exit 0）；
- backfill 正向：0007 预置 `organization.create:<pid>` 记录 → head → claim 恰好一条；
- 歧义 fail-closed：同一 pid 多条不同 org → RuntimeError + transactional DDL 整体回滚
  （表/函数不存在）；**malformed scope（新增 RED 用例）**：`organization.create:<suffix>`
  不可解析 → RuntimeError `unparseable owner principal` + 整体回滚；
- backfill 可验证：INSERT 后 EXCEPT 零遗漏校验；
- 残留数据状态下 fail-closed 实证（§3 环境观察）；
- head→0007→head 可逆（db-contract 用例 + 手动 downgrade/upgrade）。

## 7. RED 机制修订登记（相对被拒 6cf854f，全部在 2dcb1d5 内；进入 GREEN 后 tests/ 零改动）

| 文件 | 修订 | 动机 |
| --- | --- | --- |
| `tests/integration/policy/test_opa_bootstrap_slow.py` | 新增窄 helper `_delete_bootstrap_claims_if_exists`（`to_regclass` 判表存在，存在才 DELETE）；`_reset_alice` 与 invited-member/multi-org 两处 bob 清理复用 | RED 数据库停在 0007 时无条件 DELETE 在 fixture 期截断全部用例，永远到不了真实反例；RED fixture 兼容，非生产旁路 |
| `tests/integration/rls/test_mutation_policy_audit.py` | 同款窄 helper | 同机制 |
| `tests/unit/identity/test_identity_domain.py` | 恒真断言 `org_b not in memberships` 修正为 `(owner.id, org_b) not in memberships` | memberships 键是 `(principal, org)` 二元组，原断言恒真 |
| `tests/integration/policy/test_bootstrap_claim_db_contract.py` | 新增 `test_backfill_fails_closed_on_malformed_scope`；seeder 支持自定义 scope；清理 helper 参数化 | 0007 预置不可解析 scope → 0008 必须 fail closed；RED 期以 DID NOT RAISE 失败 |
| `tests/integration/foundation/test_database.py` + db-contract | 注释事实修正 '无 org/ws 列'→'无 org/ws 租户作用域语义' | claim 表含 organization_id（目标值），错误在语义不在列存在 |

## 8. 独立只读审查（1 轮 Subagent，未参与实现，只读）

审查面：RED 反例有效性（fixture 不截断、断言语义零放宽、无 skip/xfail）、无生产
旁路（to_regclass 仅测试 fixture；无第二个 claim 表/函数/service/gate/权限矩阵）、
迁移/ACL（advisory lock 串行化、UNIQUE 第二层、SECURITY DEFINER + search_path、
REVOKE/GRANT 顺序、backfill fail-closed、transactional DDL）、事务语义（claim 仅
created=True 分支、403 `from error` 链、loser 零审计/outbox、重放路径不变）、
反屎山（policies/、policy_gate.py、pyproject/uv.lock 零 diff）、docs 提交纯注释、
cherry-pick 身份（patch-id 实证 02059bc≡2e0fe7a、f92bead≡bfc3f12）。

**结论：无 blocking**。INFO 4 条（hashtextextended 碰撞只增串行化不损正确性；大写
hex UUID 后缀按 malformed fail closed 属保守设计；跨 principal 同 org 历史撞
UNIQUE 时是 IntegrityError 而非自定义消息，仍 fail closed；候选分支不含原交接文档
提交——本次 docs 提交补齐）。

## 9. 纪律核对

- 生产 claim 架构、Rego、幂等域、权限矩阵、API 契约零改动（patch-id 实证与 02059bc/
  f92bead 一致）；0008 迁移与 ORM/repository/command/router 内容逐字节未动；
- 未触碰 evals/（make evals + determinism 全绿后仍零 diff）与 T2 Keycloak 测试；
- 零新增第三方依赖（127 包不变）；
- 冻结测试进入 GREEN 后零改动（handoff-check exit 0）；
- 未直接 rebase/force-push/移动原 `feat/s1-t4-rls-audit` 分支（`git branch` 实证仍在
  e7f8cd3）；是否替换由 operator 批准。

## 10. 本 artifact 自身

- 路径：`artifacts/gates/s1-t4/`（gitignored，未经 owner 批准不 `git add -f`）；
- 原始 Gate 输出：同目录 `gate-raw.txt`（RED 9+5 失败证据 + GREEN 全部门禁原始输出与
  exit code）；逐项原始文件在 `red/`、`green/` 子目录；
- gate-raw.txt SHA-256 见交接单引用处；本文件不嵌入自身 hash。
