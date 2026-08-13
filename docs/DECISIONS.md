# 架构决策记录

> 本文记录冻结总设计之后、实施之前补齐的机制级决策。每条决策都必须包含：问题、竞品调研、候选
> 方案对比、选择与理由、影响的 spec 和验证方式。
>
> 规范源仍是[企业 Agent Core 冻结总设计](superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md)；
> 本文的决策以增补形式回写对应章节，不构成第二套架构。
>
> 竞品调研日期：2026-08-13。引用表示接口/方法依据，不表示第三方为本项目背书。

## 索引

| ID | 决策 | 影响阶段 | 状态 |
| --- | --- | --- | --- |
| [ADR-001](#adr-001) | 模型请求 wire 完整性绑定的捕获层 | S3 | accepted |
| [ADR-002](#adr-002) | context fit 判定与 token ROI 指标分离 | S3、S9 | accepted |
| [ADR-003](#adr-003) | 无快照数据源的 Evidence 可复算等级 | S5、S6 | accepted |
| [ADR-004](#adr-004) | RiskHypothesis 的序贯证伪机制 | S8 | accepted |
| [ADR-005](#adr-005) | 并行 Task 的 canonical state 合并语义 | S2 | accepted |
| [ADR-006](#adr-006) | Evidence 的 ACL 时态语义：可复算与可见性解耦 | S5、S6 | accepted |
| [ADR-007](#adr-007) | context_refusal 的恢复路径与压缩尝试上界 | S3 | accepted |
| [ADR-008](#adr-008) | 委托环检测与终止界 | S2、S4 | accepted |
| [ADR-009](#adr-009) | Memory candidate 的写入去重与队列收敛 | S7 | accepted |
| [ADR-010](#adr-010) | Provider 中立：OpenCode Go 降为 EndpointProfile 实例 | S3 | accepted |

---

<a id="adr-001"></a>

## ADR-001：模型请求 wire 完整性绑定的捕获层

**影响阶段**：S3 Models & Context　**状态**：accepted

### 问题

`ContextManifest` 承诺绑定「实际序列化 wire body 的 digest」，这是全项目稀缺度最高的机制。但捕获点
若挂在业务层或 SDK 调用层，有三条失真路径：

1. SDK 内部重试（429/5xx）会**重新构造并序列化** request，捕到的是第一次的 body，发出的是第 N 次；
2. `stream=True` 与非流式的序列化路径不同；
3. SDK 版本升级可能在序列化阶段追加/改写字段（默认参数、beta header、tool 格式迁移）。

任何一条发生，`wire_digest` 就是一个看起来严谨、实则无效的证据。

### 竞品调研

| 方案 | 捕获位置 | 拿到真实 wire body | 保留 agent/inventory 语义 | 边界 |
| --- | --- | --- | --- | --- |
| [Helicone](https://blog.premai.io/llm-observability-setting-up-langfuse-langsmith-helicone-phoenix/)（proxy 派） | 改 base URL 走反向代理，HTTP 层 | ✅ | ❌ 请求/响应级，多步 agent 事后拼接 | 需要把流量交给第三方或自建代理；对「哪些内容是 authoritative」无概念 |
| [Langfuse](https://www.langchain.com/resources/llm-observability-tools) / LangSmith / OpenLLMetry（SDK 派） | callback/decorator 包裹 SDK 调用 | ❌ 记录的是**逻辑请求** | ✅ | 语义丰富，但恰好错过本项目要证明的那一层 |
| [OTel GenAI semconv](https://opentelemetry.io/blog/2026/genai-observability/) | span attribute，内容捕获 opt-in | ⚠️ 记录 prompt 文本，非序列化 body | 部分 | 定位是 telemetry 与合规留痕，不是完整性证明；官方明确提示需 sampling/redaction |
| TEE 证明（[NEAR AI](https://docs.near.ai/cloud/private-inference/)、[VeriLLM](https://arxiv.org/pdf/2509.24257)） | provider 侧可信执行环境 | ✅ 但证明的是**服务端执行** | ❌ | 要求 provider 支持 TEE；解决的是「服务端有没有老实跑」，与「客户端实际发了什么」正交 |

**关键结论：没有任何竞品做客户端侧的 wire-body 完整性绑定。** proxy 派站对了位置但没有语义，
SDK 派有语义但站错了位置，TEE 派解决的是另一个问题。

### 候选方案

- **A. SDK 调用层 hook**：实现最简，但三条失真路径全中。否决。
- **B. 自建反向代理**：拿得到真 body，但引入独立进程、TLS 终止和额外部署面，且 manifest 与 proxy
  日志的关联需要再造一套 correlation。
- **C. httpx transport 层捕获**（选中）：`openai-python` 与 `anthropic-python` 同基于 httpx，在自定义
  `AsyncBaseTransport.handle_async_request` 中拿到最终 `request.content`，此时 body 已完成全部序列化，
  位于「proxy 派的位置」，却仍在应用进程内，可直接与 Context IR、inventory 关联。

### 决策

采用 **C：捕获点下沉到 httpx transport 层**，并配套三条约束：

1. **SDK 内部重试禁用**（`max_retries=0`）。重试上移为显式新 Attempt + 新 ContextManifest，使
   「一次发送 = 一个 manifest」成为结构不变量，而不是靠约定。
2. **capture 与 send 在同一 transport 调用内完成**：先计算 digest 再放行请求，digest 计算失败即
   拒绝发送（fail closed），不允许「先发了再补记」。
3. **transport 是唯一出网路径**：模型 adapter 不得自行构造 httpx client；S3 加架构测试断言所有
   provider SDK 实例都注入了受控 transport。

### 必须建设的验证语料（wire tamper corpus）

设计约束不加验证等于没有。在 adapter 与 transport 之间注入三类篡改，断言 pre-send gate 全部捕获：

| 攻击 | 注入方式 | 期望结果 |
| --- | --- | --- |
| 追加隐藏 system message | 中间层往 messages 头部插一条 | inventory 不匹配 → 拒绝发送 |
| 静默截断 tool schema 字段 | 删除某 tool 的 required 项 | Context IR digest 不匹配 → 拒绝发送 |
| 重试时替换 body | 模拟 SDK 重试并改写内容 | 无对应 manifest 的发送 → 拒绝发送 |
| 超长 body 截断 | 传输层截断尾部 | digest 不匹配 → 拒绝发送 |

该 corpus 同时是终面压力题「hash 能证明什么」的直接答案：它证明的是**发送链一致性**，不是语义正确性。

### 后续动作

- 补入总设计 §16 spike 表：**wire capture 保真性（P0）**，降级路径为方案 B 自建代理。
- 该 spike 不依赖任何前置阶段，可在 S0 之前独立执行。
- 影响 `specs/s3-models-context.md`、`docs/MODELS.md` §4。

---

<a id="adr-002"></a>

## ADR-002：context fit 判定与 token ROI 指标分离

**影响阶段**：S3 Models & Context、S9 Eval/Release/Telemetry　**状态**：accepted

### 问题

原设计把「token 预算」同时用于两件性质完全不同的事：

- **context fit**：内容能否装进模型的 context window —— 这是**物理硬约束**，`authoritative-or-refuse`
  这个 invariant 直接建立在它之上；
- **成本预算**：花多少钱 —— 项目所有者已明确这**不是硬约束**，而是衡量 token ROI 的内部指标。

两者混在一个「budget」概念里的后果是：估算不准时，本该 `context_refusal` 的请求被发出去，provider
返回 `context_length_exceeded`，被记成普通 provider error —— **invariant 已被破坏，指标上完全看不见**。

### 竞品调研

**A. context fit 的权威计数**

| 方案 | 精度 | 覆盖 |
| --- | --- | --- |
| [Anthropic `messages.count_tokens`](https://www.propelcode.ai/blog/token-counting-tiktoken-anthropic-gemini-guide-2025) | 精确，与计费一致；按实际发送的 messages/system/tools 构造 | 仅 Anthropic |
| [OpenAI token counting API](https://developers.openai.com/api/docs/guides/token-counting) | 返回模型实际接收的 count，含 role/边界等结构性 token | 仅 OpenAI |
| 本地 tiktoken | 纯文本尚可；**images/files 不支持，tools 与 schema 难以本地计数**，reasoning/caching 会改变分词 | 通用但不可靠 |
| 字符数 /4 | 官方明确标注为不准确 | — |

**B. token ROI 指标**

| 来源 | 方法 | 可借鉴点 |
| --- | --- | --- |
| GitHub Effective Tokens（转引自 [Telerik](https://www.telerik.com/blogs/ai-cost-visibility-before-the-invoice)） | `ET = multiplier × (1.0×new_input + 0.1×cache_read + 4.0×output)` | **加权 token** 而非裸 token；output 权重远高于 input |
| [Braintrust](https://www.braintrust.dev/articles/how-to-track-llm-token-usage-2026) | 三层可见性：每次调用的 prompt/completion、每请求的 context 利用率、agent trace 内每步用量 | 三层结构直接可用 |
| [Glean token efficiency](https://www.glean.com/perspectives/key-metrics-for-evaluating-token-efficiency-in-ai-systems) | token 效率按**任务**而非按调用衡量 | 「cost per request 掩盖整条 trajectory 的真实开销」 |
| [Less Context, Better Agents](https://arxiv.org/pdf/2606.10209) / [GenericAgent](https://arxiv.org/pdf/2604.17091) | 上下文信息密度最大化；full-context 1,480,996 vs 优化后 553,374 tokens / 50 任务 | 压缩收益可量化为对照实验 |
| [Maxim context engineering](https://www.getmaxim.ai/articles/context-engineering-for-ai-agents-production-optimization-strategies/) | 历史 context 压缩比 3:1~5:1，tool 输出 10:1~20:1 | 可作为 Context Compiler 的对照基准 |

### 决策

**拆成两个互不混用的机制。**

#### (1) context fit —— 硬约束，走权威计数，分三级降级

```text
level 1  provider 官方 count_tokens API        → authoritative_count
level 2  官方 tokenizer 本地实现（已知 vocab）  → verified_local_count
level 3  校准估算器 + 保守 margin              → calibrated_estimate
```

- 每个 `ModelProfile` 显式声明所处级别；**未知即 fail closed**，走 level 3 的保守 margin，不取「常见默认」。
- level 3 的 margin 由**实测校准**得到：用 provider 回传的 `usage.prompt_tokens` 对本地估算器做回归，
  per-(endpoint, model) 维护误差分布，margin 取该分布的高分位数。这条路径对第三方聚合 endpoint
  （不提供 counting API 的模型）是唯一可行解。
- **`context_length_exceeded` 类错误强制映射为 `context_refusal`**，不计入 provider failure。否则
  `authoritative_state_preservation_rate` 是靠错误分类洗出来的数字。
- 该映射事件同时触发对应 profile 的 estimator 重标定，并把该 profile 的 context fit 判定临时降为更保守档。

#### (2) token 会计 —— 不是 gate，是 observability 层的 ROI 指标

预算不再阻断执行（`hard_stop` 仅保留为组织可选的运维保护，默认关闭）。改为一组**可用于优化决策**
的指标，按 Run/trajectory 而非按 call 归集：

| 指标 | 定义 | 用途 |
| --- | --- | --- |
| `weighted_tokens` | 借鉴 ET：`1.0×new_input + 0.1×cache_read + 4.0×output`，权重随 profile 可配 | 消除「input 便宜所以无所谓」的错觉 |
| `authoritative_token_share` | authoritative 类内容占实际发送 token 的比例 | **本项目独有**：直接衡量 Context Compiler 是否把预算花在了该花的地方 |
| `evidence_per_kilotoken` | 每千 weighted token 产出的**已验证** Evidence 数 | 把 token 支出与项目的核心产出绑定，而不是与「回答长度」绑定 |
| `recoverable_reload_waste` | 同一 recoverable 内容在一个 Run 内被重复载入的 token 量 | 暴露 ref/rehydrate 策略的缺陷 |
| `context_utilization` | 实际发送 token / 该 profile context window | 对照 Braintrust 的第二层可见性 |
| `compression_ratio` | 压缩前后 token 比，分 conversation / tool output 两类 | 对照 3:1~5:1 与 10:1~20:1 基准 |
| `cost_per_completed_task` | 整条 trajectory 的加权成本 / 终态为 SUCCEEDED 的任务数 | 对照 Glean 的 per-task 口径 |

**这组指标进入 S9 的 sealed eval artifact，并成为 Context Compiler 消融实验的因变量。** 项目已有的
消融矩阵方法论（prereg、paired bootstrap、Holm 校正）可直接复用到「压缩策略 A vs B」这类对照上——
这比单纯报告 cost 更接近真正的工程贡献。

### 后果

- 「预算不足」不再是 Run 的失败原因，`budget` 相关的 failure taxonomy 条目降级为 warning 事件。
- `docs/MODELS.md` §8 的 Usage Ledger 保留（会计仍要准），但其定位从「门禁」改为「ROI 指标源」。
- 补入 §16 spike 表：**token estimator 校准（P0）**。

---

<a id="adr-003"></a>

## ADR-003：无快照数据源的 Evidence 可复算等级

**影响阶段**：S5 Knowledge、S6 Evidence & Ask　**状态**：accepted

### 问题

`docs/DATA_MODEL.md` 对数据库 Query Evidence 写的是「绑定 schema snapshot、transaction/snapshot
identifier（**若支持**）」。这三个字是 Evidence Contract 最弱的一环：企业 DB 常经只读副本接入，
往往没有稳定 snapshot id。没有它，QueryReplay 的「复算」退化成「重跑一次 SQL 得到不同结果」——
而结构化数据恰恰是 Ask 最核心的证据来源之一。

### 竞品调研

| 方案 | 做法 | 对本问题的适用性 |
| --- | --- | --- |
| [dbt snapshots](https://dagster.io/guides/how-dbt-snapshots-work-quick-tutorial-best-practices) | SCD Type-2，周期性把源表变化物化为历史表，可按时间点查询 | 冻结的是**表**不是**查询结果**；且要求对源库有写入/建模权限，Agent 只读接入场景不成立 |
| 数据仓库 time travel（Delta/Snowflake 等） | 引擎级版本化，可 `AS OF` 查询 | 强，但只在特定引擎可得；不能假设所有接入源都具备 |
| [in-toto attestation](https://github.com/in-toto/attestation) | subject digest + predicate，证明「某产物由某过程产生」 | 语义层可直接借鉴：把**结果集**作为 subject |
| [OpenLineage](https://github.com/OpenLineage/OpenLineage) | run/job/dataset/version 的血缘语义 | 可借鉴其 dataset version 概念 |

**结论：没有通用方案能让任意只读数据源都支持时间点重放。** 因此正确做法不是假装能，而是把
「能到什么程度」变成 Evidence 的一等属性。

### 决策

在 `EvidenceRef` 上增加 `reproducibility_level` 枚举，并新增第四种冻结模式。

| level | 含义 | 复算方式 | 适用 |
| --- | --- | --- | --- |
| `replayable` | 可在原快照上重执行并得到逐字节相同结果 | 重跑 SQL @ snapshot id | 支持 snapshot 的源、冻结语料 |
| `copy_frozen` | 原查询不可重放，但**结果集副本**已冻结并 digest | 校验副本 digest + 展示 SQL/params/时刻/schema | 只读副本、无 snapshot 的生产库 |
| `reference_only` | 仅有定位符，内容未冻结 | 只能定位，不能复算 | 外部易变 API，**不得支撑 Fact 类 claim** |

`copy_frozen` 的具体协议：查询结果经 canonical 规范化（沿用已有的 integer/decimal/float/text/bytes/
datetime 编码规则与 ordered/multiset 语义）后写入 Object Store，走既有的
`temporary upload → digest verify → immutable key → PG manifest` 协议；Evidence 绑定
`{sql, typed_params, schema_snapshot_digest, executed_at, result_copy_digest, row_count}`，并显式标注
`replayable: false`。

**Claim 分层规则**（新增硬约束）：

- `Fact` 类 claim 只能由 `replayable` 或 `copy_frozen` 支撑；
- `reference_only` 只能支撑 `Inference` / `Recommendation`；
- 一个 Answer 中若混用不同 level，Claim/Evidence Map 必须逐条显示 level，不允许整体呈现为「已验证」。

### 后果

- Evidence Contract 从「理想情况下成立」变为「所有情况下有定义」，且**弱的地方是显式标注的，不是隐藏的**。
- `zhiwei verify` 增加 level 校验：对 `copy_frozen` 校验副本 digest，对 `reference_only` 直接返回
  「不可复算」而非失败——区分「验证不通过」与「本就不承诺可验证」。
- 影响 `docs/DATA_MODEL.md` §8/§11、`specs/s5-knowledge-fabric.md`、`specs/s6-evidence-ask.md`。

---

<a id="adr-004"></a>

## ADR-004：RiskHypothesis 的序贯证伪机制

**影响阶段**：S8 Discover & Actions　**状态**：accepted

### 问题

总设计 §7.3 要求「每条假设必须同时显示支持、反证/缺失」——这是 Discover 区别于普通异常检测的
**唯一**理由。但反证从哪来、谁生成、如何判定「已充分尝试证伪」，全文没有任何机制。不补的话，
实现时必然退化成「让模型写一段 counter-argument」，正好撞上项目自己在 §11.3 定的自证循环红线。

### 竞品调研

| 来源 | 方法 | 可借鉴点 |
| --- | --- | --- |
| [POPPER](https://arxiv.org/pdf/2502.09858)（ICML 2025） | 以 Popper 证伪原则为纲的 agentic 验证框架：LLM agent 设计并执行**证伪实验**，用**序贯检验**控制 Type-I error，证据来源可多样 | 核心方法直接可用：把「证伪」形式化为一串可累积的统计检验，而非一次性判断 |
| [AIGS](https://arxiv.org/pdf/2411.11910) | 独立的 `FalsificationAgent` 角色 | 证伪职责应由**独立组件**承担，不能与提出假设的组件是同一个 |
| [AnomalyClaw](https://arxiv.org/pdf/2605.10397) | refutation agent，明确针对 confirmation bias：工具调用倾向于**支持**当前假设而非**检验**它 | 直接命中风险发现场景的失效模式 |
| Anomalo / Monte Carlo 等数据质量产品 | 规则 + 统计基线告警，人工 triage | 无证伪概念，告警即结论 |

### 决策

采用 **POPPER 的序贯证伪范式**，落为 typed `NegativeProbe`：

```text
RiskHypothesis
  → 生成 N 个 typed NegativeProbe：「若此假设为假，应观察到 X」
  → X 必须是可由 detector / query / retrieval 独立求值的断言，不得是自由文本
  → 逐个执行，每个 probe 结果作为独立 EvidenceRef 附加
  → 序贯累积证据，控制 Type-I error
  → 未被推翻且证据充分 → 进入 human triage
  → 被推翻 → 记录并终止，保留完整证伪轨迹
```

配套硬约束：

1. **职责分离**（借鉴 AIGS）：probe 的生成与求值由独立 task node 承担，不复用产生该 hypothesis 的
   detector/exploration 上下文，避免 confirmation bias 沿上下文传导。
2. **probe 必须 typed**：断言归约为 `{metric, entity_scope, window, comparator, threshold}` 之类可机器
   求值的结构；模型只负责**提出**候选 probe，求值一律由确定性组件完成——与项目「确定性可判项不用
   LLM judge」的既有纪律一致。
3. **准入门槛**：hypothesis 只有在「至少 N 个 negative probe 已执行且未推翻」时才能进入 human triage
   队列，否则停留在 Signal 状态。N 由 DiscoveryProgram 的 evidence standard 声明。
4. **probe 覆盖度进 eval**：`falsification_coverage`（已执行 probe 数 / 应执行数）与
   `hypothesis_refutation_rate` 成为 Discover suite 的一等指标。**refutation_rate 恒为 0 是危险信号**
   ——说明证伪机制没有真正在工作，等同于项目在校验器上的既有纪律「一个从不失败的校验器等于没有」。

### 后果

- Discover 从「规则引擎 + LLM 包装」升级为有方法论锚点的机制，且锚点是可引用的公开研究。
- 代价：每条 hypothesis 的成本上升（N 个额外 probe）。这在 ADR-002 的新口径下是**可测量的 ROI 权衡**
  而非硬性阻断——`evidence_per_kilotoken` 会如实反映证伪的开销与收益。
- 影响 `specs/s8-discover-actions.md`、`docs/RISK_EVAL.md`。

---

<a id="adr-005"></a>

## ADR-005：并行 Task 的 canonical state 合并语义

**影响阶段**：S2 Agent Runtime　**状态**：accepted

### 问题

总设计 §4.1 规定「只读且无依赖的节点可并行」「合并顺序由稳定 task/call id 决定，不由完成时间决定」
——顺序确定了，但**冲突解决没有定义**。两个并行 Retrieve 对同一 entity 产生矛盾 binding 时，reducer
是后写覆盖、先写胜出，还是并存？留白的必然结果是实现者顺手写成 `dict.update()`，而那与项目
「证据冲突必须并列」的核心主张直接矛盾。

### 竞品调研

| 方案 | 做法 | 可借鉴点 |
| --- | --- | --- |
| [LangGraph reducers](https://ranjankumar.in/langgraph-reducers-concurrent-state-writes) | 并发写同一 state key 时，**未声明 reducer 直接抛 `InvalidUpdateError`**；声明后按 reducer 合并（`operator.add` 追加、`add_messages` 带去重） | **「未显式声明合并策略即拒绝」正是 fail-closed 哲学**，直接采纳 |
| LangGraph 默认行为 | 单写者场景 last-write-wins | 对 authoritative 数据不可接受 |
| CRDT | 无协调的最终收敛 | 过重；且本项目有全序 event log，不需要无协调收敛 |
| 事件溯源常规做法 | 追加事件 + 投影时解决 | 与本项目 reducer 模型一致，但仍需定义解决规则 |

### 决策

**采纳 LangGraph 的「必须显式声明否则拒绝」，但把策略集合扩展为三类 typed merge，并对
authoritative 字段强制最严格的一类。**

| 策略 | 语义 | 适用字段 |
| --- | --- | --- |
| `append` | 追加，保序（按稳定 task id） | Evidence refs、artifacts、observations |
| `last_write_wins` | 后写覆盖（顺序由 task id 定，非完成时间） | 幂等的派生统计、进度类字段 |
| `conflict_preserving` | **冲突并存**，写 `ConflictRecord`，不做仲裁 | entity binding、decision、constraint —— 即全部 authoritative 类 |

配套硬约束：

1. Task Graph **发布前**静态校验：任何可能被并行节点写入的 canonical state 字段，必须在 schema 上
   声明 merge 策略；未声明则 AgentVersion 发布失败（不是运行时才报错）。
2. `conflict_preserving` 字段存在**未解决 conflict** 时，`Synthesize` 节点不得产出 `Fact` 类 claim，
   只能产出 `Inference` 或触发 `Clarify`。这让「证据冲突必须并列」从输出层的呈现要求，变成运行时的
   结构性约束。
3. `ConflictRecord` 记录双方 source task/attempt、Evidence refs 与检测时刻，进入 canonical event，
   可在 Workbench 的 Run 面板展开。

### 后果

- 「并行加速」不再以静默丢失矛盾信息为代价。
- 需要在 S2 增加并发 property test：同一 entity 被 K 个并行分支写入 K 个不同值时，断言产生 K-1 条
  ConflictRecord 且 Synthesize 被正确降级。
- 影响 `specs/s2-agent-runtime.md`、总设计 §4.3。

---

<a id="adr-006"></a>

## ADR-006：Evidence 的 ACL 时态语义 —— 可复算与可见性解耦

**影响阶段**：S5 Knowledge、S6 Evidence & Ask　**状态**：accepted

### 问题

`SourceVersion` 冻结了 ACL snapshot，但**撤权不是内容更新**，两者传播语义不同。用户在 T1 有权限、
Evidence 于 T1 冻结，T2 被撤权后回看历史 Run 的 Evidence——能不能看？原设计只写了「删除/撤权优先
传播」和「源更新使旧 Evidence 标 stale」，没有回答这一条。留白会导致二选一的错误：要么权限泄露，
要么历史 Run 不可复算。

### 竞品调研

| 来源 | 做法 | 可借鉴点 |
| --- | --- | --- |
| [Glean](https://www.glean.com/perspectives/how-do-security-features-affect-enterprise-search) | 权限在**查询时**校验而非仅索引时；用户失去访问权后，文档应**立即**从结果中消失，即使数周前已索引 | 直接采纳为可见性规则 |
| [Glean citations](https://www.glean.com/perspectives/top-ai-assistants-for-accurate-source-citations) | 引用本身也要过同一次 ACL 校验，与其支撑的片段同级 | Evidence 与 Claim 同级校验 |
| 通用 RAG 实践 | 依赖生成后 redaction | 明确被否定：未授权内容根本不应进入 prompt |
| 各家做法的空白 | 均无 durable Run 概念，因此**没有「历史运行记录中的引用」这一问题** | 本项目需自行定义 |

### 决策

**把「可复算性」与「可见性」显式解耦。**

| 维度 | 规则 |
| --- | --- |
| 系统可复算性 | Evidence **永远**可被系统复算（审计、eval、Claim Registry 依赖它）。撤权不删除 Evidence 记录 |
| 用户可见性 | 按**当前** ACL 重新校验，fail closed。冻结时的 ACL snapshot 只用于解释「当时为何可见」，不作为现在的授权依据 |
| 失权呈现 | Run 视图渲染 `evidence_access_revoked` 占位，**不静默移除** —— 静默消失会让用户误以为该结论本就没有证据 |
| 审计通道 | Auditor 角色的可见性由 `docs/PERMISSIONS.md` 矩阵单独授予，并写入 audit event |
| 检索侧 | 沿用 Glean 原则：候选生成前 pre-filter + hydration 后 re-check，两道都在**当前** ACL 上 |

### 后果

- 「历史 Run 完全可复算」这一说法必须精确化为：**系统可复算，用户可见性受当前授权约束**。
  `docs/PORTFOLIO_NARRATIVE.md` 的声明注册表需同步这一措辞。
- S6 增加安全测试：撤权后同一 Run 的 Evidence 对原用户不可见、对 Auditor 可见、对 eval 复算通道可用。
- 影响 `specs/s5-knowledge-fabric.md`、`specs/s6-evidence-ask.md`、`docs/PERMISSIONS.md`。

---

<a id="adr-007"></a>

## ADR-007：context_refusal 的恢复路径与压缩尝试上界

**影响阶段**：S3 Models & Context　**状态**：accepted

### 问题

自动降级链（移除已持久化正文 → 移除可重取内容 → 摘要旧 conversation → task split → 更大 context
模型）全部失败后，Run 终止于 `CONTEXT_REFUSED`，用户无路可走。一个正确的 invariant 会因此在实际
使用中变成「强大但不可用」，进而催生私下绕过它的改动。另外，降级链本身缺少**尝试次数上界**：
若单个 tool 输出极大，每次压缩后上下文立刻被重新填满，会形成压缩循环。

### 竞品调研

| 来源 | 做法 | 可借鉴点 |
| --- | --- | --- |
| Claude Code | 分层策略「从廉价的本地操作到昂贵的 API 调用」按序执行；**若单个文件/工具输出过大导致每次摘要后立刻重新填满，则在数次尝试后停止自动压缩并报错，而不是循环** | 降级链同构；**尝试次数上界**正是本项目缺的 |
| [Claude context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing) | `clear_tool_uses` 策略按时间顺序自动清除最旧的 tool result | 与本项目 recoverable 类语义一致，可作为 adapter 级优化 |
| Claude Code `/rewind` | 时间回溯到更早状态作为恢复手段 | 提供第二条恢复路径的思路 |
| [Structured Context Eviction](https://arxiv.org/pdf/2606.11213) / [Addressable Recall Compaction](https://arxiv.org/html/2607.25066) | 结构化驱逐、可寻址召回；长观测替换为引用，正文仍可从存储恢复 | 与本项目 recoverable + ref 设计一致，可作为压缩策略消融的对照组 |
| 主流框架 | 静默截断 | 明确否定 |

### 决策

三条增补，缺一不可：

1. **压缩尝试上界**：降级链每级最多尝试 `max_compaction_attempts`（默认 3）。达到上界仍不满足即
   进入 refusal，**不允许循环压缩**。上界与每级尝试记录写入 `ContextManifest`，使「为什么拒绝」可解释。
2. **显式授权降级**（人工恢复路径）：向触发者/Approver 展示「将被丢弃的 authoritative 条目清单」，
   经确认后以**新 Attempt** 执行，`ContextManifest` 标 `authoritative_waived: [refs...]`，写 audit +
   canonical event。**该 Attempt 产出的 claim 强制降级为 Inference，不得标 Fact。** invariant 不被破坏，
   只是被显式、留痕、有代价地放宽。
3. **epoch 回退**（第二条恢复路径）：允许回退到上一个 `ContextEpoch` 并改选更大 context 的
   ModelProfile 重试，产生新 `TransitionManifest`；不复用旧 Attempt 的 manifest。

### 后果

- `CONTEXT_REFUSED` 从死胡同变为**有出口的受控状态**，且两条出口都留痕、都有能力降级代价。
- S3 增加测试：注入超大 tool 输出，断言尝试三次后进入 refusal 而非无限循环；断言 waived 路径产出的
  claim 无法标记为 Fact。
- 影响 `specs/s3-models-context.md`、总设计 §4.4、`docs/MODELS.md` §4。

---

<a id="adr-008"></a>

## ADR-008：委托环检测与终止界

**影响阶段**：S2 Agent Runtime、S4 Capability Hub　**状态**：accepted

### 问题

已发布 Agent 可作为 ToolProvider（总设计 §8.4），加上 `Delegate` 节点，`A → B → A` 的环完全可构造。
原设计只说「权限和预算逐层收窄」——预算收窄是**间接**限制：环仍会烧完预算才停，且中途已经产生副作用。

### 竞品调研

| 来源 | 做法 | 边界 |
| --- | --- | --- |
| [When Agents Do Not Stop](https://arxiv.org/pdf/2607.01641) | 系统研究 LLM agent 的无限循环；核心论点：**问题不在于循环存在，而在于是否有有效的界覆盖其反馈路径** | 直接采纳为设计判据 |
| LangGraph | `recursion_limit` 全局步数上限 | 单一全局界，不区分委托层级与工具循环 |
| AutoGen | `max_consecutive_auto_reply`（默认 3） | 会话轮次界 |
| CrewAI | `max_iter`；层级模式下委托链在长任务中变脆，实践中需回退顺序模式 | 说明「仅靠运行时界」不足 |
| [Security Considerations for Multi-agent Systems](https://arxiv.org/pdf/2603.09002) | 界应包含最大轮次、超时、重试上限、预算与递归上限的组合 | 多维界的组合 |

### 决策

**三层界，覆盖全部反馈路径**（对应上述论文的核心判据）：

| 层 | 机制 | 时机 |
| --- | --- | --- |
| 静态 | SolutionPack/AgentVersion 发布前对委托依赖图做**环检测**；自委托必须在 AgentVersion 上显式声明并附带深度上限 | 发布时 |
| 结构 | `max_delegation_depth` 硬上界；`delegation_chain` 作为 Run 的 typed 字段参与 CAS 校验，子 Run 继承并递增 | 运行时创建 ChildTask 时 |
| 反馈路径 | 每条可能构成循环的路径上，必须存在**至少一个单调递减的界**（剩余预算 / 剩余深度 / 剩余重试）。发布前静态检查该性质，无法证明则拒绝发布 | 发布时 + 运行时双重 |

补充：`Delegate` 与 Agent-as-tool 两条路径**共用同一计数**——否则可以通过在两种形式间交替来绕过界。

### 后果

- 环不再依赖「烧完预算」这种事后止损，而是发布期就被拒绝。
- 影响 `specs/s2-agent-runtime.md`、`specs/s4-capability-hub.md`、总设计 §4.6/§8.4。

---

<a id="adr-009"></a>

## ADR-009：Memory candidate 的写入去重与队列收敛

**影响阶段**：S7 Memory　**状态**：accepted

### 问题

每个 Run 都可能产生 memory candidate，长期运行下待确认队列单调增长，Memory Steward 会被淹没。
原设计定义了完整的状态机（candidate → confirmed → superseded/revoked/expired），但没有定义**流量控制**。
一个正确但会被淹没的机制，实际效果等同于没有。

### 竞品调研

| 系统 | 去重 | 冲突解决 | 外部诊断 |
| --- | --- | --- | --- |
| [Mem0](https://github.com/mem0ai/mem0/discussions/4787) | **写入时**去重：结构化 entity 抽取（who/what/where/when）+ 语义相似度混合，避免纯 embedding 匹配的误合并与漏重 | 新事实与旧事实矛盾时**更新/替换**旧事实 | LongMemEval 49.0%（GPT-4o） |
| [Zep/Graphiti](https://menuagentic.com/blogs/mem0-vs-letta-vs-zep-vs-cognee/) | 实体消解：**确定性 MinHash + LSH 快路径**，LLM 仅作 fallback；再做 edge 去重 | 矛盾事实**关闭旧 edge 的有效期窗口并开启新窗口**，两版本按时间范围共存，不删除 | LongMemEval 63.8%（GPT-4o） |
| Letta | 由 agent 自行改写 core memory block | 取决于 prompt | — |

**关键读数**：在时态推理任务上 Zep 显著优于 Mem0（63.8% vs 49.0%），差距来自**结构化时态建模**——
即「保留两个版本 + 有效期窗口」优于「覆盖旧值」。

### 决策

项目现有设计（superseded 版本 + conflict 并存 + tombstone）**已经是 Zep 路线且更严格**（多了人工
confirm 环节），方向无需改变。需要补的是 **Mem0 的写入时去重** 与 **Graphiti 的确定性快路径**：

1. **candidate 去重键**：`(organization, workspace, scope, scope_subject, type, subject, normalized_key)`。
   同键新 candidate **合并证据**（追加 source_refs、更新 observed_at、提升 confidence），不新建记录。
2. **确定性优先的相似度快路径**（借鉴 Graphiti）：先用规范化 key 精确匹配与 MinHash/LSH 近邻，
   仅在快路径无结论时才调用模型判定。这与项目「确定性可判项不用 LLM judge」的既有纪律一致，
   同时把 memory 写入的 token 开销压在可控范围（ROI 指标可观测，见 ADR-002）。
3. **自动过期**：`retention_policy` 给出默认值——candidate 超过 `candidate_ttl`（默认 30 天）未被确认
   自动转 `expired`，保留 tombstone 供审计。
4. **队列排序**：Memory Center 按「触发 Run 数 × 影响面 × sensitivity」排序，而非时间倒序。
5. **验收标准升级**：S7 的 Gate 从「能确认/撤销/删除」提升为「**队列可收敛**」——在注入 N 个重复
   candidate 的负载测试下，待确认条目数不随 Run 数线性增长。

### 后果

- 与已有的 LongMemEval/LoCoMo 外部诊断计划衔接：项目可直接对照上述公开分数说明自身定位，
  但须遵守既有纪律——外部基准只测其定义的能力，不为整个平台背书。
- 影响 `specs/s7-memory.md`、总设计 §6.3、`docs/DATA_MODEL.md` §6。

---

<a id="adr-010"></a>

## ADR-010：Provider 中立 —— OpenCode Go 降为 EndpointProfile 实例

**影响阶段**：S3 Models & Context　**状态**：accepted

### 问题

OpenCode Go 因订阅成本原因被选作开发期的 base URL，但当前文档把它写成了**架构级概念**：
`docs/MODELS.md` 用两个整节（§7 冻结边界、§8 预算与 Usage Ledger）描述它，README 正文出现其配额
数字，`findings.md` 有大量条款与模型清单细节。这会让读者（以及实现者）误以为平台是为某个第三方
聚合服务定制的，与「provider-neutral、三种薄 transport」的核心主张矛盾。

### 决策

**OpenCode Go 是当前唯一已配置的 `EndpointProfile` 实例，不是架构概念。** 具体调整：

0. **凭据键名采用 OpenAI 兼容生态标准**。Agent 运行时的 provider 统一为 OpenAI 兼容风格，默认
   endpoint 使用 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`，可直接复用 OpenAI SDK 与既有
   工具链，也避免每接一个 provider 就发明一个键名。
   **治理不因键名通用而放宽——这正是 `config/providers/endpoints.yaml` 存在的理由**：
   `OPENAI_BASE_URL` 的值必须与某个已登记 endpoint 的 `base_url` 规范化后完全一致，`OPENAI_MODEL`
   必须落在该 endpoint 的 `allowlist ∩ /models ∩ 有效 attestation` 内，任一不满足即 fail closed。
   一句话：**键名是通用的，值必须是已登记的**。该不变量由 `tests/unit/test_environment.py` 在开发
   环境即刻断言，S3 的 endpoint allowlist 再做完整规范化比对。

1. `docs/MODELS.md` §1–§6、§9 为 provider-neutral 规范；§7/§8 改写为「附录：当前已配置 endpoint 实例」，
   明确标注可替换、可移除，且移除后规范部分不变。
2. 配额、条款、模型清单等易变事实保留在 `config/` 与 `findings.md`，文档正文只引用不复述具体数字。
3. 项目对外叙事中，模型接入能力的表述单位是**三种 wire protocol + profile/attestation 分级**，
   不是「支持 18 个模型」。
4. 新增 endpoint 的路径必须是「新增一个 EndpointProfile + 走 attestation 分级」，不改任何 Core 代码
   ——这与「新增 App 不改 Core」是同一条通用性主张，应在 S3 用 architecture test 断言。

### 后果

- 项目的可迁移性成为可展示能力：换 provider = 换配置。
- 影响 `docs/MODELS.md`、`README.md`、`specs/s3-models-context.md`。

---

## 附：新增 spike（补入总设计 §16）

| 风险 | 验证 | 失败后的合法降级 |
| --- | --- | --- |
| wire capture 保真性（ADR-001，**P0**） | httpx transport 层在流式/重试/大 body 下能否稳定取得最终 body；四类篡改语料全捕获 | 改用自建反向代理捕获，manifest 通过 correlation id 关联；不降级为 SDK 层 hook |
| token estimator 校准（ADR-002，**P0**） | 对每个 endpoint/model 用回传 usage 回归本地估算，误差分布是否稳定收敛 | 该 profile 的 context fit 判定固定为最保守档，并在 profile 上标注 `calibrated_estimate` |
| SCIP 多语言索引（**P1**） | 目标语言各自的 SCIP indexer 能否在受控构建环境产出索引 | 降级 tree-sitter + 精确搜索；**必须同时声明 CodeRef 精度损失**（symbol 级降为 span 级） |
