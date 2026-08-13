# S2 - Agent Runtime and Durable Task Graph

> Status: frozen implementation specification  
> Depends on: S1  
> Unlocks: S3

## 1. Goal

用版本化 AgentDefinition/SolutionPack、typed Task Graph 和 Temporal durable shell 跑通真实 Run 状态机，
包括审批、取消、重试、并行只读 Task、ChildTask、SSE 和明确副作用边界。使用 FixturePlanner 驱动同一
runtime，暂不接真实 LLM。

## 2. Required modules

```text
src/zhiwei/agents/{domain,versions,solution_packs,task_graph}.py
src/zhiwei/runtime/{commands,reducer,scheduler,attempts,delegation,approvals,actions,failures}.py
src/zhiwei/runtime/handlers/{base,registry,core,fixture}.py
src/zhiwei/workflows/{agent_run,versioning}.py
src/zhiwei/workflows/activities/{base,runtime}.py
src/zhiwei/workers/{agent_worker,outbox_dispatcher}.py
src/zhiwei/evals/executors/agent_runtime.py
src/zhiwei/api/{agents,runs,approvals,events}.py
apps/web/src/features/{workbench,runs,approvals}/
```

## 3. Runtime contract

- Run 状态和 canonical projection 以 PG event 为真相；Temporal 只推进 workflow/timer/retry/signal。
- primitives 固定为 Intake/Plan/Clarify/Retrieve/Analyze/InvokeTool/Delegate/Verify/RequestApproval/
  Synthesize/EmitArtifact/WriteMemoryCandidate/Finish；本阶段只实现 fixture handlers，但 schema 是正式的。
- Task node 声明 typed input/output、dependencies、parallel safety、required capability、budget、failure policy、
  completion obligations。
- FixturePlanner 通过正式 Planner port 输出 TaskGraphPatch，不允许 workflow 中硬编码演示路径。
- TaskHandlerRegistry 以 primitive + handler version 注册；validate 阶段检查完整性。S2 提供 core/fixture
  handlers，后续 S3-S7 通过相同 registry 注册正式 handler，禁止 Solution Pack 直接访问 DB/provider。
- 只读独立 task 可并行，按 stable id 合并；unknown/write 串行。
- **并行合并语义**（[ADR-005](../docs/DECISIONS.md#adr-005)）：任何可能被并行节点写入的 canonical state
  字段必须在 schema 上声明 merge 策略，未声明则 AgentVersion **发布失败**（不是运行时才报错）。策略
  限三类：`append`（Evidence/artifact/observation，按 stable task id 保序）、`last_write_wins`（幂等派生
  统计与进度）、`conflict_preserving`（entity binding、decision、constraint 等全部 authoritative 字段）。
  `conflict_preserving` 字段冲突时并存并写 `ConflictRecord`（含双方 task/attempt、Evidence refs、检测
  时刻），不做仲裁；存在未解决 conflict 时 `Synthesize` 不得产出 Fact 类 claim，只能产出 Inference 或
  触发 Clarify。
- ChildTask 收窄 scope/budget/depth/deadline，返回 typed TaskResult；delegation chain 持久化。
- **委托终止界**（[ADR-008](../docs/DECISIONS.md#adr-008)）：三层界共同覆盖全部反馈路径——① 发布前对
  SolutionPack/AgentVersion 的委托依赖图做**环检测**，自委托必须显式声明并附深度上限；②
  `max_delegation_depth` 硬上界，`delegation_chain` 参与 CAS 校验，子 Run 继承并递增；③ 发布前静态
  证明每条可能构成循环的路径上至少存在一个单调递减的界（剩余预算/深度/重试），无法证明则拒绝发布。
  `Delegate` 与 Agent-as-tool 两条路径**共用同一计数**，不得交替使用绕过界。

## 4. Durable and action semantics

- start/signal 使用 PG outbox + deterministic workflow/signal id；Activity 使用 idempotency key。
- Workflow history 只保存 refs；大 payload 写 ObjectStore。
- ApprovalRequest 绑定 exact input digest；replace input 创建新 request。
- reference side-effect activity 使用 fake external ticket service：支持 provider idempotency/read-after-write。
- 无法确认结果时写 `effect_unknown`；不能自动 retry。cancel 停止新 task，并记录在途 effect state。
- Redis/SSE 丢失只影响增量；REST projection + cursor 可恢复。

## 5. Web journey

Builder 从 sandbox AgentVersion 创建 Run/EvalRun，查看 Task Graph 实时推进；Approver 在独立账号批准/拒绝；
刷新、断网后能恢复。Run detail 展示 actor、AgentVersion、tasks、attempts、approvals、artifacts、failure 和
cost placeholder status（不是虚构金额）。在 S9 release service 前不得把 sandbox version 提升为 published。

## 6. Required tests

- reducer property：事件顺序、duplicate、attempt commit/abort、parallel merge、terminal invariant。
- 并行冲突：同一 entity 被 K 个并行分支写入 K 个不同值时，断言产生 K-1 条 ConflictRecord、Synthesize
  被降级、且未声明 merge 策略的字段在**发布期**即被拒绝。
- 委托界：构造 A→B→A 与经 Agent-as-tool 交替的环，断言发布期环检测拒绝；断言深度上界与单调递减界
  在运行时生效且两条委托路径共用计数。
- Temporal：worker kill/restart、activity timeout/retry、signal duplicate、Continue-As-New、workflow replay。
- outbox：DB/Temporal 任一侧故障、duplicate start/signal。
- approval：digest swap/expiry/revoke/policy change；requester/modifier/effective AgentIdentity 与 approver 必须是
  不同 human principal；并发决策 CAS；effect idempotency/unknown/read-after-write。
- cancellation/backpressure/SSE reconnect/Redis kill。
- 10 个并发跨两 Organization Run：无事件、approval、artifact、stream 串租户且全有 terminal state。
- architecture：Core 不导入具体 Solution Pack 名称。
- handler registry：duplicate/unknown/version mismatch/missing handler 在 Run 前失败，handler I/O 只经 Activity。
- Eval：S0 Eval executor port 绑定同一 Agent Runtime；fixture EvalRun 的 registered units 全部得到终态并 seal。

## 7. Gate

```bash
uv run pytest tests/unit/runtime tests/contract/task_graph -q
uv run pytest tests/integration/runtime tests/integration/temporal -q
uv run pytest tests/security/runtime_isolation -q
npm --prefix apps/web run test:e2e -- runtime-approval.spec.ts
uv run zhiwei runtime replay-check --all-fixtures
uv run zhiwei eval run --suite runtime-contract-v1 --mode fixture --seal
```

## 8. Explicit non-goals

不接真实模型、Knowledge、Memory 或第三方 Tool；fake ticket 只验证动作语义。不得以 FixturePlanner 输出
声称 Agent 智能或模型质量，也不得把 sandbox version 当成发布产品。
