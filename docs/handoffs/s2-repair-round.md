# S2 修复轮交接单（ADR-012 规格修订后的代码回补）

> 本单记录 S0–S2 五路代码审查后、S3 开工前的修复轮。修复分三批次（A/B/C），覆盖 ADR-012
> 规格修订的全部代码级缺口。状态标注遵循声明纪律：已验证 / 配置声明 / 计划实现 / 未验证。

## 1. 轮次概述

S0–S2 五路并行代码审查（subagent）暴露 9 条已核实反例（Critical 1 / High 4 / Medium 3 / Low 1），
类别为：契约测试层级（4 条）、Gate 例外（1 条）、Fake 件边界（1 条）、读路径授权（1 条）、声明
纪律（1 条）、Gate deselect（1 条）。审查结论经独立 subagent 逐条验证后，以 ADR-012 形式回写
specs/s0、specs/s1、specs/s2 与 AGENTS.md（`fb6d973`）。

修复分三批次执行，每批严格遵循 RED→GREEN→REVIEW：Batch A（C-1/H-1/H-2/H-6 + D-1/D-2/D-4）、
Batch B（H-3/H-4/H-5/H-7 + D-3 + A2-1）、Batch C（①–⑪，含 Batch B 验收回归修复 + S0 不变量
+ 架构测试重写）。

## 2. Batch A（C-1/H-1/H-2/H-6 + D-1/D-2/D-4）

### 修复内容

- **C-1**（`2d0e104`）：`record_task_failed` 补全 scheduled/attempt/started 前置事件（backfill
  模式），首事务失败重试耗尽后序列仍可 reduce；TaskFailed 携带 attempt_id（审计对称）。
- **H-1**（`2d0e104`）：审批 requester 穿透链——POST /runs → StartRun `requested_by` → outbox
  → TemporalWorkflowSender → AgentRunWorkflowInput → `create_approval`，SoD 三层防御恢复比较
  真实 human principal（`api/runs.py:441`、`workflows/agent_run.py:216`、`activities/runtime.py:388`）。
- **H-2**（`2d0e104`）：workspace 创建动作改 `workspace_policy.configure`（`api/workspaces.py:185`，
  消除真实 OPA 下 org_owner 结构性恒 deny）；创建者同事务自动授予 `workspace_admin`（GUC 同事务
  重设 `set_tenant_context`，`api/workspaces.py:215-226`）；新增 GET/POST
  `/workspaces/{ws}/memberships`。
- **H-6**（`2d0e104`）：POST /runs body `workspace_id` 先验归属（跨 org 404 防枚举）再成员
  校验（无资格 403）；`create_runs_router` 新增必需参数 `workspace_authorizer`（缺失构造期拒绝）。
- **D-1**（`e776760`）：GET /memberships 读路径授权——新增 `org.read_memberships` 矩阵 cell
  （rego + Action 枚举 + RESOURCE_ACTIONS）；`policy_gate` 新增 `authorize_read`（deny 403
  fail closed，读不写审计）。
- **D-2**（`e776760`）：POST memberships 消费 Idempotency-Key——同 key 同 digest 重放 200、
  异 digest 409（`IDEMPOTENCY_SCOPE_WORKSPACE_MEMBER_ADD`，lookup-before-mutate + claim）。
- **D-4**（`e776760`）：workspace 角色词汇单一事实源化（`WORKSPACE_SCOPED_ROLES` →
  `_WORKSPACE_ROLE_VOCABULARY`）。

### 关键设计决策

- workspace 创建走 `workspace_policy.configure`（ADR-012 反例 4）：原 `configure_workspace`
  要求 workspace-scoped 角色，创建时无 workspace 上下文 → 矩阵结构性恒 deny。
- 创建者同事务自动授予 `workspace_admin`：与 org-owner bootstrap 对称（`0008` 迁移模式），
  GUC 同事务重设写入 workspace_memberships（FORCE RLS）。
- `workspace_authorizer` 为 `create_runs_router` 必需参数：生产绑定 `resolve_context`
  （S1 权威 membership 解析），缺失构造期拒绝（fail closed）。
- GET /memberships GUC 绑定路径 ws（非 actor.workspace_id）：授权读者含 org 作用域角色
  （org_owner/security_admin），不能绑 actor 的 workspace 声明。

### 已知 trade-off

- membership GET 与 POST 授权模型不对称：GET 走 `authorize_read`（无审计），POST 走 PEP +
  idempotency（有审计）。读无副作用、幂等由存储层保证。
- get_workspace_membership 未引入 `on_conflict_do_nothing`——并发同主体重复授予撞 PK
  → 500 + 回滚（既有模式，可与 S3 的 add_membership 对齐）。

### 验收

独立 subagent 通过。S1 测试修订（`d83d981`）、A-2 RED（`5d5d701`）、词汇快照（`545ac02`）
均在 Batch A-2（`e776760`）前完成。

## 3. Batch B（H-3/H-4/H-5/H-7/D-3/A2-1）

### 修复内容

- **H-3**（`71b2cd4`）：`decide_approval` 归属校验前置（`get` + run_id 比对）+ 决策与
  `approval_decided` 信号 outbox 行**同一事务**提交（触发器反例验证全有或全无）；
  `create_approval` 必设 `expires_at`（`approval_expiry_seconds` 默认 3600s）；
  workflow 审批等待加 timer 上界（asyncio.TimeoutError）→ `check_approval` 回查权威行
  → expired 决策落 TaskFailed + AttemptAborted。
- **H-4**（`71b2cd4`）：`create_runs_router` 新增 `event_sink` 参数；`app.py` 组装期
  `redis_stream` 先于 runs router 构造并注入——`canonical.event.committed` 经真实 Redis
  发布（端到端测试验证 `read_since` 流可达）。
- **H-5**（`71b2cd4`）：`load_events_twice_independently`（两个独立 `tenant_session` 事务；
  between 钩子供并发写注入），CLI `_replay_check_all` 改用之。
- **H-7**（`71b2cd4`）：`AgentDefinition` 增委托声明字段（`delegate_dependencies` /
  `tool_agent_refs` / `self_delegation_depth_cap`，draft/sandbox 期可迭代）；发布期组合图环
  检测（直接边 + agent-as-tool + pack 派生边，三色 DFS，`DelegationCycleError` /
  `SelfDelegationUndeclaredError`）；`PackVersionManager` 对等绑定；`StartRun` 命令携带
  `delegation_chain`，`RunCommandService` 在 Run 行写入前执行 `MAX_DELEGATION_DEPTH` 硬上界。
- **D-3**（`71b2cd4`）：`record_task_failed` 补 `AttemptAborted`（attempt 投影 aborted，
  幂等键 `attempt_terminal_key`）。
- **A2-1**（`71b2cd4`）：`_membership_grant_digest` 对 `role_bindings` 排序——digest 与
  frozenset 序列化顺序无关（跨 `PYTHONHASHSEED` 逐字节一致）。

### 关键设计决策

- **decide 归属前置 + 同事务决策+信号**：归属校验在 decide 之前（`get` + run_id 比对），
  避免决策已提交后 404 导致 workflow 永久等待；决策落账与 outbox 行在同一事务。
- **approval expiry 全链路**：`expires_at = T_create + E`；workflow timer = E+1s（重放安全）
  → `check_approval` activity 查询权威行 → store 的 expires_at 拒绝先于 timer。
- **Redis event_sink**：`app.py` 组装顺序保证 redis_stream 先于 runs router 注入；
  dispatcher `event_sink=None` 时直接标 delivered（防 pending 堆积）。
- **`load_events_twice_independently`**：两个独立事务间注入并发写→第二次载入必须观察到
  间隙写入（同事务双查询鉴别力不足，ADR-012 反例 7）。
- **发布期委托环检测**：组合图（agent 委托 + agent-as-tool + pack 派生）三色 DFS；
  `SelfDelegationUndeclaredError` 强制自委托声明上限；declared self-loop 不计入环检测图
  （运行时硬上界兜底）。
- **命令层 depth 硬上界**：`MAX_DELEGATION_DEPTH`（`runtime/delegation.py:19`）在
  Run 行写入前执行；delegation_chain 作为 tuple[str,...] 在 StartRun 命令载荷中。
- **digest 排序**：`_membership_grant_digest` 对 `role_bindings` 排序后进 JCS digest
  （workspaces.py _membership_grant_digest）。

### 已知 trade-off

- per-agent `self_delegation_depth_cap` 不在命令层执行——仅存声明字段；
  `RunCommandService` 只查全局 `MAX_DELEGATION_DEPTH`。待 S4 Delegate handler 实现时接入。

### 验收

独立 subagent 通过。**已知缺陷①**（decide 未知/跨租户 request_id → 500）延至 Batch C。

## 4. Batch C（①–⑪）

### 修复内容

- **①**（`33eee3a`）decide 先查后裁：`store.get` → `ApprovalError` → 404（Batch B 回归）。
- **②**（`33eee3a`）expired 权威行回查：`check_approval` activity 在 timeout 后查询 PG 审批
  状态；已决决策消费决策结果，不误判 expired。
- **③**（`33eee3a`）`EffectUnknownError` 契约 + `_interpret` 门：effect_unknown 直接终态，
  不走重试循环；`EffectUnknownError` 于 `handlers/base.py` 新增。
- **④**（`33eee3a`）`RunPaused`/`RunResumed` 落账：pause/resume 信号置 dirty 标志，主循环
  在 wait 前经 `record_run_terminal` 落 `RunPaused`/`RunResumed` event（`agent_run.py:158`，
  `record_run_terminal` 增加 paused/resumed 分支）。
- **⑤**（`33eee3a`）registry 预检：`start_run` activity 调 `validate_completeness`（排除
  `RequestApproval`，其走专用等待路径不经 handler 执行）；`TaskHandlerRegistryError`
  列入 `_INFRA_RETRY_POLICY.non_retryable_error_types`。
- **⑥**（`33eee3a`）审批 digest 绑定节点内容：`CreateApprovalInput.node_content` dict
  → `digest_bytes(canonical_json(input.node_content))`（`activities/runtime.py:388-393`）。
- **⑦**（`33eee3a`）merge 单边拒绝：`validate_merge_strategies` 改为「每个潜在写者都必须
  声明」（`task_graph.py:176-215`，原 `not a and not b` → `not (a and b)`）。
- **⑧**（`33eee3a`）ConflictDetected canonical event + reducer 去重 + advisory lock 串行：
  执行路径：reduce before → append TaskCompleted → `_append_conflict_events` 追加
  ConflictDetected event（双方 task/value + attempt_id）；reducer `_append_conflict_deduped`
  按 (field, task_ids) 去重；`store.lock_run()` 取 advisory xact lock 保证并行完成事务
  的 reduce 串行（`runtime_events.py:129`，与 `append_event` 同键）。
- **⑨**（`33eee3a`）Synthesize 降级门：execute_task 中 task_type == "Synthesize" 时
  先 reduce 检查未解决 conflict；存在冲突时 handler 输出替换为
  `{"synthesize_downgraded": True, "unresolved_conflict_fields": [...]}`。
- **⑩**（`33eee3a`）S0 不变量：`_reject_jsonb_non_roundtrippable_floats` 在
  `validate_event_command` 中校验（integral float 绝对值超 2^53-1 拒绝，写入侧 fail closed，
  `persistence/events.py:123-147`）；`seal` 前 verify_manifest 复验 dataset 对象完整性
  （`evals/runs.py:536-545`）。
- **⑪**（`33eee3a`）架构测试重写：`tests/contract/solution_packs/test_core_boundary.py`
  遍历真实 core 模块（runtime/workflows/workers/evals）→ `ImportError` 即 FAIL（非恒真 +
  except pass fail-open）；原 `test_schema.py` 的 `TestArchitectureBoundary` 保留占位说明
  替换关系。

### 关键设计决策

- **check_approval 权威行回查**：workflow timeout 后不直接设 expired，先调 activity 查 PG
  审批行状态；store 决策早于 timer 消费者以批准/拒绝决策，非到期误判。
- **effect_unknown 非重试**：`EffectUnknownError` 声明副作用状态未知——重试 = 重复副作用，
  `_interpret` 识别后直接终态，不进指数退避循环。
- **RunPaused/RunResumed 落账**：workflow 暂停态经 `record_run_terminal` 落 PG（幂等键
  `run_terminal_key` 去重），确保 REST/SSE 投影在暂停期间反映 paused。
- **registry 预检排除 RequestApproval**：该 primitive 走专用等待路径（create_approval →
  决策信号 → record_approval_outcome），不经 handler 注册/执行——要求注册无语义，
  只会迫使无意义注册。
- **ConflictDetected 串行**：advisory lock（`pg_advisory_xact_lock`）在 reduce 前获取，
  保证并发完成事务的 reduce 读到先落账者的 TaskCompleted（读已提交语义）。
- **Synthesize 降级**：S2 无 Fact/Inference 词表，降级门为「不产出正常合成输出」
  （marker dict）；S6 Evidence/Ask 需据此引入 claim 降级语义。
- **RecordApprovalOutcome "expired"** 走 TaskFailed 分支（非 approved/completed），
  status 为 "failed"，error 为 "approval expired"。

### 已知 trade-off

- **Synthesize 降级**：S2 命名空间无 Fact/Inference/Inference 类 claim 词表——降级门当前
  为结构性 marker dict，S6 Evidence/Ask 阶段需扩展。
- **冲突 dedupe key**：字段 + 写者集合（不是 attempt 对）；attempt 双方证据仅为当前
  写者的 `attempt_id`。
- **validate_completeness 在 start_run activity（非 command 侧）**：missing handler 时
  Run 行已创建（状态为 "created"，但 workflow 不调度任务，终态为 failed）。
- **scim_group 同事务审计**未在本轮实现（SCIM service 自开 tenant_session，端点另开
  审计事务，GAP 登记于 ADR-012 §决策 3）。

## 5. 批次间 Ripple 修订

修复轮期间多个 RED 测试因批次推进产生 ripple 修订（RED 血统，均在 GREEN 前提交）：

- **S1 测试修订**（`d83d981`）：workspace 创建动作断言 `configure_workspace` → `configure`；
  新建 workspace 不再手动 seed 创建者绑定（bootstrap 路径同事务授予 `workspace_admin`）；
  合成 actor 创建 workspace 前必须种子 principal。
- **TestPolicyRoles 词汇快照**（`545ac02`）：`read_memberships` 登记（ADR-012 决策 4 矩阵
  词汇扩展）。
- **TestDelegationBoundary pack 测试顺序**（`732268a`）：pack_b 在 pack_a 未发布时创建
  → InvalidPackReferenceError（错误原因）→ 修正为 pack_a 先发布。
- **replay_probe/RunCommand RED 测试修复**（`2d46cd7`）：replay_probe fixture 缺 Run 行
  → UoW append 报错（非契约失败）；delegation chain 参数改 tuple 对齐。
- **Batch C 涟漪**（`aa88313`）：pause 测试改轮询、conflict 测试换 ConflictFixtureHandler、
  conftest 补 EffectUnknownFixtureHandler、test_canonical_value_domain ruff/pyright 修复。

**关键教训**：RED 冻结的测试在 GREEN 期产生 ripple 修订时，必须在 GREEN 前以 RED 血统 commit
提交——测试修订 + 实现提交的分界线是 handoff-check 的基线。

## 6. Gate 与验证状态

本轮改动后全量 Gate 状态（2026-09-03，WSL2，PG 17.6@55432，Python 3.11.15，
Temporal WorkflowEnvironment，OPA 127.0.0.1:8181，redis-server 7.2.5）：

| 检查项 | 结果 | 来源 |
| --- | --- | --- |
| `uv run pytest -q` | 1214 passed / 20 deselected / 0 failed | 本机执行 |
| `uv run ruff check .` | All checks passed | 本机执行 |
| `uv run pyright` | 0 errors, 0 warnings | 本机执行 |
| `make evals` | 110 项校验全部通过 | 本机执行 |
| `make determinism` | 两次干净重建逐字节一致 | 本机执行 |
| `replay-check --all-fixtures` | passed，7/7 deterministic+chain verified | 本机执行 |
| `eval run --suite runtime-contract-v1 --mode fixture --seal` | 7/7 terminal, sealed | 本机执行 |
| `npm --prefix apps/web run build` | 全绿 | 本机执行 |
| `make handoff-check HANDOFF_BASE=aa88313` | ✓ 未漂移 | 本机执行 |

声明纪律：以上均为本机实际执行输出（`artifacts/gates/s2-repair-round/report.md` 为 artifact）。

独立 subagent 复核：Batch A 通过（`d83d981`/`5d5d701`/`545ac02`）；Batch B 通过
（`cdff9ba` RED 10 failed 均为缺失契约，GREEN 后全绿）；Batch C 通过（`29f7be3` RED 12 failed
均为缺失契约，GREEN 后 1214 passed/0 failed）。

## 7. 仍开放的注册债务（S3+ 范围）

| 优先级 | 债务 | 说明 |
| --- | --- | --- |
| **高** | SCIM group 审计同事务 | S1 引擎边界设计——SCIM service 自开 session，审计另开；group
  类无跨引擎借口（ADR-012 §决策 3 Fake 件边界登记） |
| **高** | Child-run delegation 集成测试 | S3 Delegate handler 实现后补 integration 级委托链 + 环检测
  端到端；delegation_chain 为 Run typed 字段 + 子 Run 继承递增 |
| **中** | SSE 心跳 + 游标下推 | 降级模式空闲连接被代理掐断（nginx ~60s timeout）；PG 轮询
  每秒全量重载（O(N)/poll, O(N²) 累计） |
| **中** | Web SSE 客户端 + attempts/actor/cost 展示 | spec §5 journey 缺口——run detail
  无 EventSource/轮询、无 attempts/actor/cost placeholder |
| **中** | permission_denied audit trail | 读路径 deny 不留痕（ADR-012 建议但未实现） |
| **低** | opa/redis 本地构建 Makefile targets | 消除手工构建步骤（可与 S11 并行） |
| **低** | POST /workspaces/{ws}/memberships 409 冲突语义 | 同 body 并发授予 500（IntegrityError），
  既有行为，需考虑 add_membership 式原子区分 |

---

## Artifacts

| artifact | 路径 | 说明 |
| --- | --- | --- |
| s2-repair-round gate report | `artifacts/gates/s2-repair-round/report.md` | 全量 Gate 实际执行输出 |
| S0-S2 五路审查 findings | `docs/progress.md` §评审 | 审查发现概要 |

---

*交接单版本：2026-09-03。执行方：opencode。本单记录 S0–S2 五路审查后三批次代码修复轮的
规格修订、修复内容、设计决策、trade-off、ripple 修订、Gate 状态与遗留债务。*
