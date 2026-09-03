# S2 - Agent Runtime and Durable Task Graph

> Status: frozen implementation specification  
> Revised: 2026-09-03（S0–S2 复审增补，见 ADR-005/008 增补与 ADR-012：模块清单纠偏、append
> 保序与声明边界、委托界可判定化、审批 SoD/原子性/过期/digest、effect_unknown 重试门、
> Redis event_sink 接线、测试层级契约）  
> Depends on: S1  
> Unlocks: S3

## 1. Goal

用版本化 AgentDefinition/SolutionPack、typed Task Graph 和 Temporal durable shell 跑通真实 Run 状态机，
包括审批、取消、重试、并行只读 Task、ChildTask、SSE 和明确副作用边界。使用 FixturePlanner 驱动同一
runtime，暂不接真实 LLM。

## 2. Required modules

```text
src/zhiwei/agents/{domain,versions,solution_packs,task_graph,schemas}.py
src/zhiwei/runtime/{commands,reducer,scheduler,attempts,delegation,approvals,actions,failures,events,planner}.py
src/zhiwei/runtime/handlers/{base,registry,core,fixture}.py
src/zhiwei/workflows/agent_run.py
src/zhiwei/workflows/activities/{base,runtime}.py
src/zhiwei/workers/{agent_worker,outbox_dispatcher,temporal_sender}.py
src/zhiwei/persistence/{runtime_events,run_commands,approvals}.py
src/zhiwei/telemetry/redis_streams.py
src/zhiwei/evals/executors/agent_runtime.py
src/zhiwei/api/{agents,runs,approvals,events}.py
apps/web/src/features/{workbench,runs,approvals}/
```

（2026-09-03 纠偏：`workflows/versioning.py` 已删除（C-B1）；`run_commands`/`runtime_events`/
`approvals` 的 PG 绑定按 C-A1 裁决位于 persistence 层；`planner`/`temporal_sender`/
`redis_streams` 为 T7/遗留轮新增。修订记录见 ADR-012。）

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
- **并行合并语义**（[ADR-005](../docs/DECISIONS.md#adr-005) 及 2026-09-03 增补）：任何可能被并行
  节点写入的 canonical state 字段必须在 schema 上声明 merge 策略，**每个潜在写者都必须声明**
  （单边声明即发布失败，不是「至少一方声明」）；未声明则 AgentVersion **发布失败**（不是运行时才
  报错）；经 Planner 产物等未过发布校验的图在运行时遇到未声明策略的写入，拒绝该事件或降级为
  conflict_preserving，**禁止静默覆盖**。策略限三类：`append`（Evidence/artifact/observation，
  合并结果为 `(task_id, attempt_no)` 的纯函数，与到达/完成顺序无关，同一逻辑图两次执行逐字节一致）、
  `last_write_wins`（幂等派生统计与进度，顺序由 task id 定）、`conflict_preserving`（entity binding、
  decision、constraint 等全部 authoritative 字段）。
  `conflict_preserving` 字段冲突时并存并写 `ConflictRecord`（含双方 task/attempt、Evidence refs、检测
  时刻），**ConflictDetected 作为 canonical event 落账**（不允许只存在于内存投影）；存在未解决
  conflict 时 `Synthesize` 不得产出 Fact 类 claim，只能产出 Inference 或触发 Clarify——该降级是
  运行时结构性门，必须有实现与 integration 级测试，不允许只存在于文档。
- ChildTask 收窄 scope/budget/depth/deadline，返回 typed TaskResult；delegation chain 持久化。
- **委托终止界**（[ADR-008](../docs/DECISIONS.md#adr-008) 及 2026-09-03 可判定化增补）：三层界共同
  覆盖全部反馈路径——① 发布前对委托依赖图（AgentVersion 的 delegate 依赖 + SolutionPack 依赖 +
  agent-as-tool provider 边）做**环检测**，任何环发布失败（含 A→B→A 与经 tool provider 交替构成
  的环），自委托必须显式声明并附深度上限；② `max_delegation_depth` 硬上界，`delegation_chain`
  作为 Run 的 typed 字段参与 CAS 校验，子 Run 继承并递增；③ 终止性由「DAG 无环 + 每条委托边
  严格消耗剩余深度」的构造直接成立，无独立静态证明义务。`Delegate` 与 Agent-as-tool 两条路径
  **追加同一 chain、递增同一计数**，不得交替使用绕过界。发布期环检测与运行时共享计数都必须有
  integration 级测试（域模块单测不构成契约生效的证据，ADR-012）。

## 4. Durable and action semantics

- start/signal 使用 PG outbox + deterministic workflow/signal id；Activity 使用 idempotency key。
  **审批决策信号同样经 outbox 原子入列**：决策落账与 `approval_decided` 信号必须在同一事务提交
  （两事务分离 = 崩溃窗口内决策已生效而 workflow 永久等待，ADR-012 反例）。
- Workflow history 只保存 refs；大 payload 写 ObjectStore。
- **审批 requester 必须为真实 human principal**：从 POST /runs 的 actor 穿透 StartRun 命令 →
  workflow input → approval request 行；SoD（requester/last_input_modifier 与 approver 不同
  human principal）必须在 REST 决策路径上有 integration 级反例测试——requester 恒为常量（如
  `agent-runtime`）时三层防御（域层/PG/DB CHECK）同时失效（ADR-012 反例 1）。
- ApprovalRequest 绑定 **task input** 的 exact input digest（不得派生自 run/task 身份常量——
  那样 swap 检测结构上不可触发）；replace input 创建新 request；审批必须设置 expiry，workflow
  等待有上界或由过期信号解除。
- reference side-effect activity 使用 fake external ticket service：支持 provider idempotency/read-after-write。
- 无法确认结果时写 `effect_unknown`；不能自动 retry——该门必须在 workflow 重试决策
  （`_interpret` 循环）内生效，域层 manager 未接线时不构成契约交付（ADR-012 反例 2）。
- pause/resume 信号除 workflow 内存态外，必须落 `RunPaused`/`RunResumed` canonical event
  （否则 PG 真相在暂停期间恒显 running，违背「以 PG event 为真相」，ADR-012 决策 1 回补清单）。
- cancel 停止新 task，并记录在途 effect state。
- Redis/SSE 丢失只影响增量；REST projection + cursor 可恢复。**Redis event_stream 必须在生产
  组装中接线为 outbox event_sink**（未接线时 Redis 恒空、「加速通道」为死代码，SSE 实际退化为
  纯 PG 轮询且延迟声明不实——ADR-012）；SSE 降级模式（Redis 不可用）必须发送心跳注释帧，
  否则空闲连接被代理掐断。

## 5. Web journey

Builder 从 sandbox AgentVersion 创建 Run/EvalRun，查看 Task Graph 实时推进（实时 = 经 SSE 或等价
订阅消费，不允许仅一次性 REST 拉取冒充实时）；Approver 在独立账号批准/拒绝；
刷新、断网后能恢复。Run detail 展示 actor、AgentVersion、tasks、attempts、approvals、artifacts、failure 和
cost placeholder status（不是虚构金额）。在 S9 release service 前不得把 sandbox version 提升为 published。

## 6. Required tests

> 按 ADR-012：跨组件不变量的验收测试必须至少一条位于 integration/contract 层并走真实生产路径；
> 域模块单测是必要而非充分条件。以下标注【I】的条目必须有 integration 级测试。

- reducer property：事件顺序、duplicate、attempt commit/abort、parallel merge（含 append 保序为
  `(task_id, attempt_no)` 纯函数、与到达序无关的确定性断言）、terminal invariant；**写入侧不得
  产生 reducer 自身拒绝的事件序列**（第一事务失败 → 重试耗尽 → TaskFailed 落账后该 run 的每次
  reduce 仍可消费——缺失前置事件时由写入侧补全）【I】。
- 并行冲突：同一 entity 被 K 个并行分支写入 K 个不同值时，断言产生 K-1 条 ConflictRecord（作为
  canonical event 落账，重放后仍在）【I】、Synthesize 被降级【I】、且未声明 merge 策略的字段在
  **发布期**即被拒绝（含「单边声明」反例：K 个写者任一未声明即拒绝）。
- 委托界：构造 A→B→A 与经 Agent-as-tool 交替的环，断言发布期环检测拒绝【I】；断言深度上界在
  运行时生效且两条委托路径共用计数（integration 级，非仅 chain 数据结构单测）【I】。
- Temporal：worker kill/restart、activity timeout/retry、signal duplicate、Continue-As-New（含
  approval_decided 信号跨 CAN 不丢失）、workflow replay。
- outbox：DB/Temporal 任一侧故障、duplicate start/signal、审批决策与信号同事务原子性【I】。
- approval：digest swap（digest 绑定 task input，改输入必须触发新 request）【I】/expiry/revoke/
  policy change；requester/modifier/effective AgentIdentity 与 approver 必须是不同 human principal
  （**经 REST 决策路径的反例测试**：requester 本人 approve 被 403/409 拒绝）【I】；并发决策 CAS。
  effect 语义分界（消歧，ADR-012）：workflow `_interpret` 的 **effect_unknown 禁自动重试门属本阶段
  修复轮**——构造 effect_unknown 失败，断言 workflow 不重试、run 以 failed/effect_unknown 终态
  落账【I】；provider idempotency/read-after-write 语义随 S3 正式 handler 补齐（登记于 S3 计划）。
- pause/resume：信号后 PG 事件真相更新（RunPaused/RunResumed 落账，投影可观测）【I】。
- cancellation/backpressure/SSE reconnect/Redis kill；SSE 降级模式心跳存在性【I】；Redis
  event_sink 生产组装接线后端到端「发布→XREAD 唤醒」路径【I】。
- 10 个并发跨两 Organization Run：无事件、approval、artifact、stream 串租户且全有 terminal state。
- architecture：Core 不导入具体 Solution Pack 名称——测试必须遍历真实 core 模块、断言具体 Pack
  名称，`ImportError` 时 fail（恒真断言与 `except ImportError: pass` 不满足本条，ADR-012）。
- handler registry：duplicate/unknown/version mismatch/missing handler 在 Run 前失败（validate
  阶段预检接线，不允许空转重试后才失败）、handler I/O 只经 Activity。
- Eval：S0 Eval executor port 绑定同一 Agent Runtime；fixture EvalRun 的 registered units 全部得到终态并 seal。
- replay-check：探针两次载入必须在不同事务/会话（同事务双查询鉴别力不足，ADR-012 反例 7）【I】。

## 7. Gate

```bash
uv run pytest tests/unit/runtime tests/contract/task_graph -q
uv run pytest tests/integration/runtime tests/integration/temporal -q
uv run pytest tests/security/runtime_isolation -q
npm --prefix apps/web run test:e2e -- runtime-approval.spec.ts
uv run zhiwei runtime replay-check --all-fixtures
uv run zhiwei eval run --suite runtime-contract-v1 --mode fixture --seal
```

按 ADR-012：`runtime-approval.spec.ts` 必须真实存在并可执行——环境阻塞（Keycloak 编排）时按 Gate
例外条目登记（阻塞项/根因/解锁条件/复执行时点），阶段状态为「有条件收口」；不存在该文件时 Gate
不得宣称通过。

## 8. Explicit non-goals

不接真实模型、Knowledge、Memory 或第三方 Tool；fake ticket 只验证动作语义。不得以 FixturePlanner 输出
声称 Agent 智能或模型质量，也不得把 sandbox version 当成发布产品。
