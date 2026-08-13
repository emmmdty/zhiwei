# 知微 ZhiWei：企业 Agent Core 与首批 Agent Apps 冻结设计

> 状态：**已批准，作为后续规格与实施计划的唯一架构事实源**  
> 冻结日期：2026-08-12  
> 当前实现状态：`design_and_benchmark_assets`，`src/` 尚未实现  
> 实施边界：本文冻结产品闭环、架构职责与验证门；具体代码步骤见 `specs/` 与
> `docs/superpowers/plans/`。

## 0. 文档地位与证据纪律

本文取代旧版
`2026-08-12-zhiwei-verifiable-portable-agent-design.md` 中的产品范围、运行架构、权限、存储、
Web 与阶段设计。旧文档只保留为设计演进和冻结评测资产的历史记录；发生冲突时以本文为准。

`docs/DECISIONS.md` 记录本文冻结之后、实施之前补齐的**机制级决策**（ADR-001 至 ADR-010）：它们不
改变本文的产品范围与架构约束，只把「写清了要求、没写清算法」的位置补成可实现、可验证的规格。
本文相应章节已就地增补交叉引用；两者冲突时以 ADR 的机制描述为准，以本文的架构约束为准。

仓库中的主张只允许使用四种状态：

| 标签 | 含义 | 可否对外写成产品能力 |
| --- | --- | --- |
| `【已验证】` | 有仓库内可重复证据，或用户已核验 | 仅能写证据实际覆盖的范围 |
| `【配置声明】` | YAML/manifest 已冻结，但尚无运行证据 | 否 |
| `【计划实现】` | 本设计和实施计划已定义 | 否 |
| `【未验证】` | 尚无证据或仍需 spike | 否 |

截至冻结日唯一已验证事实是：

- `make evals` 通过 110 项 validator；`make determinism` 对 21 个资产两次重建逐字节一致。
- 现有 FactQA 题集为 120 行、112 个 independence unit、57 个 template；四类统计单位故障注入
  均被捕获。
- `src/` 完全未实现，因此 Agent、检索、模型、handoff、风险发现、成本、延迟、吞吐、安全和
  可用性均没有运行结论。
- 旧 S4 live 预算 `$43.0231552` 只是配置声明，不是实际花费、运行资格或新平台预算。

文档、UI、简历和演示不得把结构性保证写成模型效果。例如
`authoritative_state_preservation_rate=100%` 的真实含义是“投影完整或拒绝发送”的结构门，
不是实验测得的 100%。

## 1. 最终产品是什么

### 1.1 一句话定位

> **知微是面向企业内部数据与业务系统的可验证 Agent 应用平台：Agent Core 统一负责知识、
> 记忆、上下文、模型、工具、编排、权限和验证，Ask 与 Discover 是用同一公开机制构建的首批
> Agent Apps。**

“企业内部”描述数据、身份和治理边界，不限制业务领域。研发、经营、供应链、客服、财务、法务
都可接入；平台不能通过缩小到固定数据和固定工具来规避真实工程问题。

### 1.2 产品形态

产品是 Web-first 的多用户组织应用，并提供 API/SDK，不以终端作为主要使用方式：

```text
Organization
├── Workbench：运行 Ask、Discover、ChangeBrief 及后续 Agent Apps
├── Cases：跨 App 的显式协作、证据、动作和结论
├── Knowledge：组织/工作空间/个人知识源、同步、索引和 ACL
├── Agent Studio：构建、评测、发布 AgentDefinition/SolutionPack
├── Capability Hub：模型、MCP、OpenAPI、Tools、Skills、Connections
├── Memory Center：用户、团队和 Case 记忆的确认、冲突、撤销与删除
└── Admin：组织、成员、策略、审批、审计、成本和运行健康
```

市场坐标只用于解释产品形态，不声称功能等同：交互和企业搜索可类比 Glean/Dust，代码知识可类比
Sourcegraph，受治理的 Agent 与动作闭环可类比收窄后的 Palantir AIP。知微的核心差异是可验证运行
契约、跨模型 canonical state、源原生知识和受治理能力接入，而不是另做一个聊天框。

### 1.3 三层产品模型

| 层 | 职责 | 禁止出现 |
| --- | --- | --- |
| Agent Core | 通用运行时、上下文、知识、记忆、模型、能力、安全、证据、评测、发布 | `if app == ask/discover` 等产品分支 |
| Solution Pack | 版本化 AgentDefinition、任务图模板、Skills、能力/知识/记忆/证据策略、输出和视图 manifest | 绕过 Core 的私有执行链 |
| Agent App | 面向用户的完整场景、输入、结果、反馈和 Case 流程 | 复制一套状态、鉴权或工具系统 |

Ask、Discover 和用于证明通用性的 ChangeBrief 都必须只使用公开 Core 契约。新增 ChangeBrief 时若
必须修改 Core 中的 App 名称分支，平台通用性验收失败。

### 1.4 首批用户闭环

- 业务/研发用户在 Workbench 中使用已发布 App，查看来源、工具过程、成本和验证结果。
- Builder 在 Studio 绑定知识、模型、Tools、Skills、memory policy、预算和输出契约，运行评测后发布。
- Capability Publisher 从官方 MCP Registry、组织 Git、MCP URL、OpenAPI 或 SDK 导入能力，经过
  检查、准入、连接和版本发布后供 Builder 使用。
- 审批人处理高风险动作；安全和审计角色查看策略决定、Evidence、ActionReceipt 与访问链。
- 管理员通过 OIDC/SCIM 管理组织、Workspace、Group、User、ServiceAccount 与保留策略。

### 1.5 明确不做

以下是不属于产品承诺的边界，不是通过删需求回避风险：

- 不做无需组织审核即可运行任意第三方代码的公共开放市场；提供组织内 Capability Hub、外部目录
  发现和完整准入流程。
- 不默认自动执行付款、删除、外发等不可逆动作；这些工具可以接入，但必须通过策略、审批、幂等和
  ActionReceipt。
- 不迁移或持久化模型私有 hidden reasoning；只迁移平台拥有的 canonical state。
- 不把知识图谱节点、模型生成摘要或记忆记录直接当成事实证据；Evidence 必须回到冻结源快照。
- 不在本地运行 GPU 训练/推理；模型微调、蒸馏和自托管大模型不是本项目成立的前提。
- 不建设自由聊天式 swarm。多 Agent 通过 typed ChildTask/Agent-as-tool 委托，权限和预算逐层收窄。

## 2. 不可破坏的架构约束

1. **模型 transcript 是可丢弃投影，不是业务真相。**
2. **PostgreSQL 保存业务状态；Temporal 保存执行位置。** 二者互补，不能互相冒充。
3. **Knowledge、Context、Memory、Profile/Skill 分离。** 外部事实、当前任务、学到的协作信息和行为
   规则拥有不同来源、写入、检索和删除语义。
4. **DataSource 与 Tool 分离。** 数据读取产生 observation；只有冻结到 Source Ledger 的内容才可
   成为 Evidence。Tool 写操作产生 ActionReceipt。
5. **AgentVersion 引用不可变版本。** 别名更新不改变在途 Run；安全撤权和能力 suspend 立即生效。
6. **授权取交集。** 用户、Agent、Workspace、工具绑定、数据 ACL、组织策略和委托链任何一层都不能
   自行扩权。
7. **评测走生产运行时。** fixture/replay/live 只替换外部 binding，不允许另写一条“评测专用 Agent”。
8. **任何大框架都必须被首批 App 消费。** 没有 UI 行为、运行事件、测试和 reference integration 的
   空 registry/adapter 不算交付。

## 3. 核心领域与事实源

### 3.1 业务层级

```text
Organization                         # 最高业务隔离边界
└── Workspace                        # 协作、资源、成本和策略边界
    ├── Group / Membership
    ├── KnowledgeCollection / Connection
    ├── AgentDefinition / AgentVersion / SolutionPack
    ├── Case
    │   └── Run
    │       ├── ContextEpoch / Attempt / ChildTask
    │       ├── CanonicalEvent / TaskGraph
    │       ├── Evidence / Artifact / Approval / ActionReceipt
    │       └── UsageLedgerEntry / EvalReference
    └── MemoryRecord
```

个人空间仍归组织所有，只是 ACL 默认为本人；不允许跨组织共享源、连接、记忆或 AgentVersion。

### 3.2 事实源所有权

| 数据 | 权威来源 | 派生/临时来源 |
| --- | --- | --- |
| 组织、成员、资源版本、发布状态、策略绑定 | PostgreSQL | IdP group、前端缓存 |
| Agent canonical event/state、Case、审批、成本账 | PostgreSQL append-only event + projection | 模型 transcript、Temporal history |
| 长任务位置、重试、定时器、Signal、Child Workflow | Temporal | PostgreSQL 状态摘要 |
| 原始知识、snapshot、Evidence/Eval/wire 大 artifact | S3-compatible Object Store + PostgreSQL manifest | worker 临时目录 |
| 混合检索索引 | OpenSearch，可从 Source Ledger 重建 | embedding cache |
| UI 增量 | Redis/SSE 短保留流 | 浏览器状态 |
| 身份认证 | OIDC IdP | 应用 session |
| 应用授权 | ZhiWei resource model + OPA decision + PostgreSQL RLS | IdP role 名、MCP annotation |
| 凭据密文 | SecretBackend | 数据库中的 opaque handle |

Redis、搜索索引和浏览器全部丢失时，系统必须能从 PostgreSQL、Object Store、Temporal 和外部源的
同步水位恢复，不得要求用户从聊天记录重建业务状态。

### 3.3 版本模型

`AgentDefinition`、`SolutionPack`、`ModelProfile`、`EndpointProfile`、`DataSource`、`ToolProvider`、
`ToolDefinition`、`Skill`、`Workflow`、`PolicyBundle`、`Dataset`、`EvalSuite` 均采用：

```text
stable resource id -> immutable versions -> mutable alias/lifecycle
```

Run 绑定具体 version/digest；版本更新只影响新 Run。所有版本必须带来源、父版本、schema version、
内容 digest、作者、创建时间、准入状态和兼容性信息。

## 4. Agent Runtime、编排与 Canonical Context

### 4.1 Run、Task Graph 与执行状态

一个用户请求或后台触发创建 `Run`。Run 可以独立存在，也可以显式绑定一个 `Case`；只有需要跨 Run、
跨 App 协作或进入人工处置时才创建/绑定 Case，不能为每次聊天隐式制造共享空间。Run 绑定
AgentVersion、trigger principal、AgentIdentity、Workspace、策略、预算和 Source watermark。

```text
CREATED → VALIDATING → RUNNING ↔ WAITING_APPROVAL / WAITING_INPUT / WAITING_RETRY
                         ├── DELEGATED(child workflow)
                         ├── CONTEXT_REFUSED
                         ├── SUCCEEDED | PARTIAL
                         └── FAILED | CANCELLED
```

Task Graph 不是模型自由文本，而是版本化 typed node：

`Intake | Plan | Clarify | Retrieve | Analyze | InvokeTool | Delegate | Verify |
RequestApproval | Synthesize | EmitArtifact | WriteMemoryCandidate | Finish`。

节点声明输入/输出 schema、依赖、并行安全性、所需能力、预算、失败策略和完成义务。Planner 可以从允许
的 primitives 动态实例化/修订图，但不能生成任意执行代码。只读且无依赖的节点可并行；写操作、数据
依赖和未知 effect 默认串行。合并顺序由稳定 task/call id 决定，不由完成时间决定。

Runtime 使用 typed `TaskHandlerRegistry`，AgentVersion 发布前检查所有 primitive handler 已注册且版本兼容：
S2 提供 Intake/Clarify/Delegate/RequestApproval/EmitArtifact/Finish 与 fixture handlers；S3 提供
Plan/Analyze/Synthesize 的 model handler；S4 提供 InvokeTool；S5 提供 Retrieve；S6 提供 Verify；S7 提供
WriteMemoryCandidate。handler 只能调用 application ports；需要外部 I/O 的部分必须进入 Temporal Activity，
完成后以 canonical event 提交。缺 handler 时在 validate 阶段失败，不能运行到一半临时 fallback。

### 4.2 Durable shell

`【计划实现】` 使用 Temporal Python SDK 处理长任务、定时器、重试、取消、审批等待和 Child Workflow：

- Workflow 只编排确定性状态，模型、工具、数据库外部读写和秘密访问放 Activity。
- Temporal history 只保存控制信息和 artifact refs，不保存大工具结果、secret 或完整敏感 prompt。
- API 在 PostgreSQL 事务中写 Run/intent/outbox，dispatcher 以确定性 workflow id 启动或 signal；
  Activity 写 canonical event 时使用稳定 idempotency key。
- PostgreSQL canonical head 是业务真相；Temporal crash recovery 不能覆盖或重写它。
- 长 Run 使用 Continue-As-New，但继续引用同一 canonical head、Case 和 Run identity。

选择 Temporal 是因为审批、恢复、定时 Discover 和 child delegation 都是一等需求；不用 LangGraph
checkpoint 作为生产真相，因为其 thread/checkpoint 更适合图运行状态，不替代本项目的组织授权、
Evidence、记忆生命周期和审计数据模型。该判断需通过 crash/replay/cancel spike 验证。

### 4.3 Canonical state

Reducer 从 committed canonical events 重建：

- objective、constraints、completion obligations；
- Task Graph、node status、open questions；
- entity bindings、decisions、conflicts；
- Evidence refs、artifacts、source snapshots/watermarks；
- tool/action/approval/delegation state；
- scoped memory refs；
- model/context epoch、attempt、预算和成本；
- failure、partial 和 abstention reason。

上下文数据按可丢弃性分四类：

| 类型 | 示例 | 压缩/迁移规则 |
| --- | --- | --- |
| authoritative | 目标、约束、任务、决定、冲突、证据、审批、预算 | 完整投影，否则拒绝 |
| conversational | 用户原话、解释、最近交流 | 可摘要，保留来源映射 |
| recoverable | 大工具结果、原文块、生成中间物 | 仅放 ref，需要时重取 |
| opaque | provider hidden reasoning | 当前 Attempt 临时使用，终态销毁 |

### 4.4 Context Compiler

每次模型调用走一条确定的编译链：

```text
canonical state + Agent/Profile/Policy + current task
+ Knowledge results + scoped Memory + allowed Tools + recent conversation
  → context planner / budgeter
  → provider-neutral Context IR
  → transport adapter
  → actual wire body capture
  → pre-send policy + inventory + digest gate
  → network send
```

预算不足时按固定顺序处理：先移除已持久化大结果正文，再移除可重取内容，再摘要旧 conversation，
然后尝试 task split 或更大上下文模型。authoritative inventory 仍装不下则 `context_refusal`，不允许
静默丢约束后发送。

`ContextManifest` 绑定 source inventory、omit/transform map、Context IR、target profile、token estimate
和实际序列化 wire digest。SDK/HTTP 发送层必须提供 pre-send capture，防止“验证的是逻辑请求，实际
发的是另一个 body”。

### 4.5 跨模型交接

显式模型切换创建新 `ContextEpoch` 和 `TransitionManifest`：source head、target profile、全部
authoritative inventory、projection rule、状态 digest 和验证结果。只有 manifest 验证通过后目标模型
才能接管；失败继续旧 epoch 或拒绝。

三种 wire protocol 为 `openai_chat`、`openai_responses`、`anthropic_messages`。本项目不复制 LiteLLM
的全供应商路由；薄 adapter 只负责协议/流/工具错误归一，工程增量放在 profile attestation、canonical
state、budget/policy 和 actual-wire binding 的闭环。每种协议必须有 fixture matrix 和受限 live
attestation，才能从“实现”升级到“验证支持”。

### 4.6 子 Agent

父 Run 创建 typed `ChildTask`，只传最小 `ContextSlice`、能力 allowlist、数据范围、预算、deadline 和
输出 schema。子 Agent 没有父 transcript、未授权工具、原始凭据或个人记忆；返回 `TaskResult +
EvidenceRefs + ArtifactRefs + usage + unresolved`。父 reducer 显式接受、拒绝或要求重做。

## 5. Source-native Knowledge Fabric

### 5.1 四层结构

```text
Source Ledger       不可变原件、版本、ACL、时间、水位和 digest
Source-native Index 文档结构、表格单元、代码符号/引用/提交、DB schema/query
Context Graph       可重建的实体/关系/时态导航层
Knowledge Planner   按问题生成 typed retrieval plan 并汇合证据候选
```

Context Graph 和摘要只帮助导航，不能直接作为事实证据；所有 Fact claim 必须回到 Source Ledger 中
冻结的版本和 locator。

### 5.2 数据源语义

- 文档：保留 document/section/paragraph/table/row/cell/code-block 层次、页码/标题路径/字符 span。
- 代码与 GitHub：以 `Repository@Commit` 为快照，索引 File、Symbol、definition/reference/
  implementation、imports/dependencies/tests、commit/diff/blame、PR/issue/review/check。首选 SCIP，
  tree-sitter 与精确搜索作为降级路径；embedding 只补语义召回。
- 结构化数据库：保存 schema snapshot、column semantics、read-only Query Evidence；SQL 经过 AST、策略、
  timeout/row/byte limit 和 typed params。
- API/MCP resource：原始 observation 先按 DataSource 规则冻结为 snapshot，才可生成 EvidenceRef。

GitHub 使用 GitHub App、细粒度权限、webhook + reconciliation，不把个人 PAT 作为默认企业接入方式。

### 5.3 检索

Knowledge Planner 输出 typed query：sources、entity/time filters、exact identifiers、lexical/vector need、
top-k、rerank 和 evidence requirement。文档默认 BM25 + dense + RRF + rerank；代码的 symbol/path/ref/
commit 等精确信号优先于 embedding；结构化数据优先 schema grounding + constrained query。

生产参考选择 OpenSearch 承载 hybrid/filter，PostgreSQL 保存 Source Ledger 与关系，S3-compatible
Object Store 保存原件。首个本地版本使用固定 revision 的 CPU BGE；不得以本地禁 GPU 为由删除
dense path，也不得用 GPU 才能复现的索引作为默认演示。

### 5.4 ACL、时态与新鲜度

- ACL 在候选生成前过滤，并在 hydration/返回前再次校验；ACL/索引状态不确定时 fail closed。
- 所有结果携带 organization/workspace/source ACL、observed_at、valid_from/to、source version、sync
  watermark 和 freshness status。
- webhook 只缩短延迟，周期 reconciliation 负责弥补漏事件；删除/撤权优先传播。
- Source 更新使旧 Evidence 标为 stale，不回写历史 Run；新 Run 不默认使用旧 snapshot。

### 5.5 Context Graph

首版以 PostgreSQL typed edge tables 实现，不引入 Neo4j。节点/边必须携带 source refs、observed time、
valid time、confidence 和 derivation；删除图可从 Ledger 重建。只有证明关系遍历成为瓶颈后才评估专用
图数据库，避免以技术名词代替实际检索质量。

## 6. Memory System

### 6.1 边界

| 类别 | 回答的问题 | 写入来源 |
| --- | --- | --- |
| Knowledge | 企业外部事实是什么 | 受管 DataSource 同步 |
| Context | 本次任务正在做什么 | Run canonical events |
| Memory | 我们学到了哪些可复用协作信息 | 受策略控制的提议/确认 |
| Profile/Skill | Agent 应怎样工作 | 发布版本 |

工作记忆就是 Run canonical state，不另建一个含义重叠的向量库。长期记忆分 user、team、case 三类；
不允许后台 Agent 自动写 organization-wide memory。

### 6.2 MemoryRecord

核心字段：`id/version/org/workspace/scope(user|team|case)/type(preference|fact|decision|episode|lesson)/
subject/key/canonical_value/source_refs/observed_at/valid_time/confidence/sensitivity/status/
author/approver/conflicts/retention/allowed_profiles/acl`。

状态为 `candidate -> confirmed -> superseded|revoked|expired`，禁止原地覆盖冲突记录。事实/决定必须绑定
来源；模型摘要只可作为 candidate 内容，不能伪装成人工确认事实。

### 6.3 写入与读取

- Run/Case 事实事件自动写当前状态；低风险、可撤销的个人显示偏好可按策略自动确认。
- 敏感事实、团队惯例、业务决定、推断出的习惯和 lesson 只写 candidate，需用户或 Memory Steward
  确认。secret、hidden reasoning、工具注入指令禁止写入。
- 明示配置/仓库文档中的编程规范属于 Knowledge；从 commit/review 得到的统计只是 signal；经本人
  确认的习惯才进入 user/team memory。
- 读取先做 org/workspace/scope/ACL/sensitivity/profile/time 硬过滤，再按 exact、lexical、dense、
  rerank 排序，并显式返回冲突/过期状态。
- Discover 的后台 ServiceAccount 不能读取个人记忆；每个 Agent Profile 声明可读写的 memory types。

Memory Center 必须支持查看来源、确认、纠正、撤销、删除和导出。撤销/删除触发索引与缓存 cascade；
历史 Run 保留 redacted tombstone 和 digest，不继续检索正文。

## 7. Solution Pack 与首批 Agent Apps

### 7.1 SolutionPack 契约

```text
SolutionPackVersion =
  AgentDefinition + TaskGraphTemplate + SkillVersions
  + Tool/KnowledgeRequirements + MemoryPolicy + EvidencePolicy
  + Input/OutputSchemas + ViewManifests + EvalSuiteVersions + ReleaseClaims
```

Pack 是可安装、可评测、可发布的产品单元，不是散落 prompt。Core 只解释通用字段。

### 7.2 Ask：高级企业知识研究 Agent

Ask 面向跨文档、代码/GitHub、结构化数据、经授权业务系统和 scoped memory 的复杂研究，不是普通
RAG 对话。

`AskTaskSpec` 记录问题、期望产物、时间/实体范围、允许数据、风险和完成义务。Planner 动态生成
Retrieve/Analyze/Clarify/Delegate/Verify/Synthesize。中间产物为 Evidence-bearing `Finding`；最终
`AnswerDraft` 中每个片段显式标 `Fact | Quote | Inference | Recommendation`：

- Fact/Quote 必须由 deterministic verifier 绑定 frozen snapshot、typed canonical value 和 answer span。
- Inference/Recommendation 展示输入 Evidence 和推理责任边界，但不伪称 verifier 能证明推理正确。
- 证据冲突必须并列；数据不足返回 partial/abstain 和未满足义务。

默认结果包含 Answer、Claim/Evidence Map、Artifacts、Execution Summary、Verification 和可创建 Case 的
下一步。Ask 的 reference journey 必须同时使用文档、代码/GitHub 和结构化数据，才能证明不是
Text-to-SQL/RAG 套壳。

### 7.3 Discover：持续风险发现 Agent

Discover 从 schedule、webhook、source delta 或人工触发，以 `DiscoveryProgram` 运行。Program 定义 risk
charter、source/entity scope、exclusions、triggers、detector packs、evidence standard、recipients、预算、
审批和允许动作。

三类发现路径共存：

1. deterministic known-pattern detector；
2. source/change-driven detector；
3. 在 typed `AnalysisSpec` 内的受控探索。

```text
trigger → watermark/snapshot → data quality gate → detector/exploration
→ Signal → RiskHypothesis → independent evidence + falsification
→ deterministic fingerprint/dedupe → feed → human triage
→ Case / approved action → HumanResolution → outcome/memory candidate
```

Signal、RiskHypothesis、HumanResolution 分离；不把启发式分数称为概率。生产去重使用可解释
`RiskFingerprint`，semantic similarity 只提出候选合并。每条假设必须同时显示支持、反证/缺失、影响实体、
Source watermark、建议验证动作、状态和负责人。

原 RiskInsight 改为首个 **Numeric Risk Detector Pack 与 reference eval**。realized SNR 仅描述合成数值
信号；ghost/counterfactual 是评测资产属性；一对一匹配只用于 benchmark；六类 planted pattern 的
precision/recall 不能写成现实经营风险预测准确率。

### 7.4 ChangeBrief：通用性证明

第三个轻量 App 从 GitHub webhook/commit/PR 触发，使用代码 Knowledge、change-analysis Skill、Task Graph
和 Evidence Contract 输出 Verified Change Brief：影响 symbol、依赖、tests、相关 PR/issue、风险与证据。
它不追求完整产品深度，只负责证明新增 App 不需要修改 Core 专用分支。

### 7.5 跨 App 协作

Knowledge 和治理资源可共享，Run/session 默认隔离。跨 App 只能由用户或策略显式创建/绑定 Case，并
选择要共享的 Evidence、Artifacts、decisions 和 memory candidates；不得暗中把一个 App transcript 注入
另一个 App。

## 8. Capability Hub：模型、工具、MCP、Skills 与连接

### 8.1 资源模型与目录

核心对象：`Provider`、`ProviderVersion`、`ToolDefinitionVersion`、`SkillVersion`、
`WorkflowVersion`、`Connection`、`CapabilityBinding`、`AdmissionRecord`。Capability Hub 同时提供：

- 官方 MCP Registry、组织 registry/approved Git 等目录发现；
- MCP URL、OpenAPI、Agent Skills repo/package 和 SDK provider 导入；
- 版本、来源 digest、SBOM/license/vulnerability、契约、安全、更新 diff 和健康状态；
- 组织准入、Workspace connection、Agent binding、suspend/revoke。

“发现/导入”“准入”“连接凭据”“绑定 Agent”是四个独立动作。市场目录从不直接授予权限。

### 8.2 生命周期

```text
discovered → quarantined → inspected → contract/security tested
→ approved → published → deprecated | suspended | revoked
```

准入至少验证 schema、来源/digest、license/SBOM、漏洞、网络目标、risk/effect、幂等、输出大小、超时、
权限需求和 contract fixtures。版本必须 pin；更新需要 diff、重新测试和 republish；安全 revoke 立即阻断
新调用并让在途 Run 得到结构化状态。

### 8.3 MCP

支持 stdio 与 Streamable HTTP，并正确映射 tools、resources、prompts、roots、elicitation、sampling 和
tasks，而不是只实现 `tools/list`：

- tools 进入 ToolDefinition；resources 在 S4 先进入 `ResourceDefinition/SourceObservationProvider`，由 S5
  在 Knowledge policy 下创建 DataSource/SourceVersion；prompts 只能成为待审 Skill/模板，不能覆盖
  平台 policy。
- sampling 默认关闭，Discover 后台执行禁止 sampling；elicitation 不能索取/传递 secret。
- MCP tasks 作为实验性外部异步 adapter，Temporal 仍是 ZhiWei durable truth。
- Streamable HTTP 按 MCP OAuth 2.1 做 protected resource metadata、PKCE、Resource Indicator、audience/
  scope 校验、短 token 和轮换；禁止 token passthrough。
- stdio 在固定 OCI digest 的非 root、只读、资源限额、显式 filesystem/network sandbox 中执行；不允许
  API worker 在宿主 shell 直接运行未知命令。
- MCP process/session 的隔离键至少包含 organization、workspace、ProviderVersion、Connection subject、Run；
  首版不得跨上述边界池化或复用。只有准入证明 stateless 且主体/权限完全相同时，后续版本才可受控池化。

### 8.4 Agent Skills、OpenAPI 与 SDK

- 原样兼容 Agent Skills 的 `SKILL.md/scripts/references/assets`；平台元数据 namespaced。Skill 自报的
  allowed-tools 只是请求，不能授予权限。
- declarative Skill 可投影指令/资源；可执行 script 必须冻结依赖、构建 OCI、生成 SBOM，并注册为
  `SkillScriptTool` 走同一工具链。
- OpenAPI 3.1 只导入选定 operation；模型不能拼 server URL/header；写操作必须声明幂等/复核策略。
- SDK 暴露稳定 Provider SPI，适合企业自定义连接器，但生成的资源仍走相同 admission/version/binding。
- 已发布 Agent 可作为 ToolProvider；父 Agent 只获得 typed contract 和最小委托权限。

### 8.5 Connection 与鉴权

Provider 描述能力，Connection 描述某个组织/Workspace/用户如何认证。支持：

- `user_delegated` OAuth：当前用户单独 consent，不能被后台 ServiceAccount 复用；
- `workspace_service` / `service_account`：由管理员授权的 workload identity；
- managed API key、mTLS 或数据库 secret handle。

秘密只进入 SecretBackend。local-product 使用 AES-GCM envelope store + Docker secret 主密钥；
production reference 提供 Vault Transit/KMS adapter。数据库、API、Temporal、Redis、OTel、Evidence 和
ActionReceipt 只保存 connection/credential version ref，不保存明文。

### 8.6 Tool invocation

```text
typed intent → schema validation → current authorization/policy
→ approval if required → re-authorize current principal/policy/binding/Connection
→ scoped Connection → short-lived credential
→ isolated execution → output schema/size/redaction
→ Observation or ActionReceipt → canonical event
```

有效权限为触发主体、AgentVersion、CapabilityBinding、Workspace/Org policy、data ACL、connection scope
和 delegation chain 的交集。写操作先持久化 intent/idempotency key；无法幂等且无法 read-after-write 的
工具不自动重试，超时边界返回 `effect_unknown`，不能偷偷重发。

审批等待不冻结授权。真正执行前必须重新读取 trigger principal/Membership、Agent/Capability 状态、当前
PolicyBundle、Connection/credential revoke/expiry、approval expiry 与 exact input digest；任一收紧或撤销
都拒绝执行并写结构化原因，审批人的 allow 不能覆盖组织 hard deny。

## 9. 多组织身份、安全与治理

### 9.1 身份与角色

Principal 为 `User | ServiceAccount | AgentIdentity`。Web 使用 OIDC Authorization Code + PKCE 和 BFF
server session，浏览器只持 `Secure + HttpOnly + SameSite` cookie；本地 product profile 使用 Keycloak，
生产可替换标准 IdP。SCIM 2.0/JIT 负责成员生命周期；外部稳定身份键为 `(issuer, subject)`。

基础角色：Organization Owner、Security Admin、Capability Publisher、Workspace Admin、Agent Builder、
Memory Steward、Approver、Member、Auditor。`docs/PERMISSIONS.md` 的角色×资源×动作矩阵和职责分离是
授权事实源；产品中的 Builder 即 Agent Builder。RBAC 表达职责，OPA 表达 resource、purpose、
classification、risk、time、connection 与 delegation 等 ABAC 条件。

S1 即交付通用 SecretBackend port 与 local AES-GCM envelope store。OIDC access/refresh token 以密文写
principal/session 级 AuthSession，AAD 绑定 session/issuer/subject/version，支持首次无组织登录、rotate/
revoke/restart；active org/workspace 登录后按 Membership 选择。S4 复用该 port 保存 Connection credentials，
为后者使用 per-org/workspace/binding/version AAD，并增加 Vault/KMS adapter，不另造密钥库。

### 9.2 Policy Enforcement Points

PEP 分布在 API、Run/Task、Knowledge retrieval、Memory retrieval/write、Context/model egress、Tool gateway、
Artifact download 和 SSE。任何一个外部 connector 的 `allowed=true` 都不能替代平台授权。

PostgreSQL RLS 是第二道防线：所有租户表含 `organization_id`，启用 `FORCE ROW LEVEL SECURITY`；
应用 role 不是 owner/superuser/BYPASSRLS；tenant context 仅在事务中 `SET LOCAL`，连接池释放前清理；
repository 仍显式传 org/workspace。

Object Store 使用 org prefix/bucket 和授权下载，OpenSearch 使用 per-org index + workspace ACL filter，
Redis/Temporal key 使用不可枚举 opaque id，SecretBackend per-org namespace。任何跨租户请求 fail closed。

### 9.3 模型出站与提示注入

数据分类为 `PUBLIC | INTERNAL | CONFIDENTIAL | RESTRICTED`。EndpointProfile 声明允许的 classification、
region、retention/training 条件和模型能力；pre-send gate 对 state、Knowledge、Memory、Tool schema 和
endpoint 取交集，字段级 redaction 后再发送。

信任顺序固定为：platform policy > published Agent/Profile > admitted Skill > user instruction > retrieved/
tool/MCP content。低信任内容使用边界标签，不能写 policy、扩大 Tools、请求秘密或直接写 Memory。
memory candidate 记录触发来源，重复/跨用户传播、来源撤销和 injection pattern 进入安全检测。

### 9.4 Audit 与失败关闭

成功和拒绝 mutation 同事务写 Audit outbox：actor/effective identity、org/workspace、resource/version、
action、policy decision/revision、result、request/trace id。hash chain 用于发现应用层篡改，不冒充第三方
签名；对外 release 另做 attestation。

关键 fail-close：OPA/ACL/connection 不可用时禁止数据、模型和工具出站；Redis 不可用只降级实时 UI；
OTel 不可用不阻断 Run但产生 health alert；Object digest 不符阻断 Evidence/Release；外部 effect 不确定
进入人工处置。

## 10. Evidence Contract 与动作验证

### 10.1 Evidence 类型

| 类型 | 复算对象 |
| --- | --- |
| QueryReplay | snapshot/schema 上的 SQL、typed params、canonical result |
| CellRef | file digest、sheet/table、row/column、canonical value |
| DocRef | source version、locator、code-point span、quote digest |
| CodeRef | repository@commit、path、symbol/span、language、content digest |
| GitHubRef | repo、commit/PR/issue/review/check id、versioned payload locator |
| ApiRef | request contract、response snapshot、JSON Pointer、canonical value |
| AgentRef | child AgentVersion、Run、typed output 及其 Evidence |
| PatternRef | detector version、window/entity/input rows、独立复算结果 |

Canonical value 对 integer/decimal/float/text/bytes/datetime/null 有确定编码；结果 digest 覆盖 schema、行列
边界、ordered/multiset 语义。Claim 绑定 answer digest、code-point `[start,end)`、claim type、canonical
value 与 EvidenceRefs。

`zhiwei verify` 对 bundle/schema/version/digest/snapshot/locator/query/result/claim span 做确定性验证并返回
稳定非零退出码。hash 只证明与已知 digest 的一致性；它不证明 SQL 语义正确、发布者身份或推理正确。

### 10.2 Evidence 与 Action 分离

DataSource 读取产生 Evidence；Tool 副作用产生 ActionReceipt。ActionReceipt 绑定 actor/delegation、
Agent/tool/connection/policy/approval version、input digest、idempotency key、外部 correlation id、typed result、
effect status 和 read-after-write verifier。API 200 不能证明长期业务事实，citation 也不能证明动作成功。

## 11. Eval Harness、发布与声明治理

### 11.1 一等评测资源

`DatasetVersion`、`EvalSuiteVersion`、`EvalRun` 与 AgentVersion 一样版本化。执行模式为 fixture、replay、
offline deterministic、live、shadow、human；报告必须明确模式，fixture/replay 不产生 live 模型质量结论。

分层 suite：

1. schema/transport/reducer/tool contract；
2. Knowledge retrieval、ACL、freshness、code/GitHub/cross-source；
3. context/handoff、memory write/read/conflict/forget；
4. Evidence/Action tamper/fault；
5. Ask task quality/abstention；
6. Discover planted/holdout/human utility；
7. security/prompt injection/tenant escape；
8. recovery/concurrency/latency/cost。

同一生产 Runtime 通过不同 binding 运行 eval。每个 sealed artifact 包含 config、code revision、dataset、
source/model/attempt manifest、samples/events、Evidence、score、usage、failures 和 checksum。

### 11.2 现有资产的正确位置

- 120/112/57 是 `factqa-v1` 与 `handoff-pilot` 的历史冻结资产，不证明代码/GitHub、Memory、ACL、
  Capability Hub 或完整 Agent。
- validator/determinism 只证明资产生成纪律。
- 每条 handoff edge 12 条 chain 只作 pilot 和 effect estimation；正式样本由最小有意义效应和 pilot
  variance 做 power analysis 后冻结。
- 当前 Risk planted manifest 不是正式 multi-seed production eval；RiskInsight 只证明 numeric detector
  pack 的可复算性。
- `$43.0231552` 只是 prereg 配置预算。

### 11.3 抗自证循环

正式声明至少同时依赖：冻结内部 suite、外部诊断（如 BIRD/LongMemEval/LoCoMo/Promptfoo）、作者不可见
blind holdout、metamorphic/fault injection 和必要人工评审。外部基准只测其定义的能力，不能反向给整个平台
背书。确定性可判项不用 LLM judge；inference/utility 才使用校准的人评或 judge，并报告协议与一致性。

预注册固定 estimand、independence unit、分母、排除、停止、主次指标和多重比较。比例带 n/CI；partial、
provider error、refusal、retry 和成本进入完整终态。裸模型污染诊断只披露，不按结果剔题。

### 11.4 发布

Agent lifecycle：`draft -> sandbox -> evaluated -> review -> staged -> published -> deprecated/retired`。
发布绑定 immutable dependencies、required eval、policy 和 Claim Registry。Claim Registry 中每条声明为
`planned | implemented | offline-verified | live-verified | retired`，并链接 artifact；无 artifact 的数字不能
进入 README/简历/录屏。

## 12. 可观测性、成本和可靠性

- 全链使用 W3C trace context 与 OpenTelemetry；span 覆盖 API、Run、Task、model、retrieval、tool、policy、
  approval、evidence、memory 和 eval。默认不采 prompt/result 正文，只记版本、digest、token、latency、
  status、data class；内容采样需组织策略。
- Cost Ledger 在调度前 reserve、收到 provider usage 后 reconcile；未知价格/usage 不允许写精确成本。
- Model Router 的次序固定为 compliance -> capability -> context fit -> quality qualification -> budget -> latency，
  不允许价格低就绕过数据出站和能力要求。
- 结构化 failure taxonomy 覆盖 provider、context、policy、knowledge stale、ACL、tool schema、approval、
  effect_unknown、artifact corrupt、budget、cancel 和 dependency unavailable。
- 不在设计阶段拍 SLO。先以固定 workload 测 concurrency、p50/p95、tokens/s、queue wait、recovery、cost 和
  fault behavior，再形成有证据的 SLO proposal。

## 13. Web、API 与 Studio

### 13.1 Workbench

React/TypeScript/Vite SPA 通过同源 FastAPI BFF。Workbench 使用三栏：App/Case 导航、主交互/结果、
可验证运行面板。通用 panel 包含 Run、Task Graph、Evidence、Tools、Approval、Artifacts、Context、Cost、
Memory；Ask/Discover/ChangeBrief 只提供输出 renderer/view manifest。

服务端状态使用 query cache，SSE 处理短增量，本地 draft 隔离；刷新或断线按 cursor 从 API 重建。
关键 mutation 显示 pending/confirmed/failed，不靠 token 流猜终态。

### 13.2 Agent Studio

Studio 不是空白自由画布，而是受约束的构建流程：Overview、Instructions、Knowledge、Memory、Tools、
Task、Triggers、Model、Budget、Evidence、Evals、Access、Release。Task Graph 编辑器只能选择受支持
primitives/typed ports；发布前展示版本 diff、依赖 digest、权限、成本上限和失败 Gate。

### 13.3 Capability Hub 与管理面

Capability Hub 的 Import/Inspect/Admit/Connect/Test/Bind/Suspend 都调用真实 API 并展示状态；不得出现无
后端的卡片。Knowledge、Memory、Cases、Admin 同样以完整 journey 为交付单位，不以页面数量验收。

### 13.4 API

REST 统一 `/api/v1`；资源 mutation 使用 `Idempotency-Key` 和 `If-Match`/expected version，命令使用显式
`/publish`、`/approve`、`/cancel`、`/verify`。列表使用稳定 cursor，不泄露跨租户总数。SSE 只传状态和
小 delta，大 artifact 通过授权下载。错误体固定
`code/message/details/request_id/trace_id/retryable`。

CLI 只提供管理、验证、评测和运维自动化，并调用相同 application service；不能成为绕过 policy 的
隐藏产品路径。

## 14. 工程形态与部署

### 14.1 模块化单体与进程边界

业务代码采用 Python 模块化单体，按 workload/trust 分进程，不提前拆网络微服务：

```text
apps/web/
src/zhiwei/
  api/ identity/ agents/ runtime/ context/ models/
  knowledge/ memory/ capabilities/ evidence/ cases/
  evals/ policy/ telemetry/ persistence/ object_store/ secrets/
  workflows/ workers/ cli/ contracts/
solution-packs/{ask,discover,change-brief}/
deploy/{compose,kubernetes,observability}/
tests/
```

运行进程：Web 静态站点、FastAPI API/BFF、Temporal Agent Worker、Integration Worker、Index/Sync Worker、
Eval Worker、Outbox Dispatcher、Capability Runner。Capability Runner 是独立内部服务，不能与 API/Agent
Worker 合并，API/worker 无 Docker socket、Kubernetes credential 或宿主执行权限。

Runner 后端固定两种：local-product 仅连接 admission/build pipeline 预构建并固定 digest 的 dedicated
provider runner services；新增本地 executable profile 先生成 SBOM/签名镜像和 Compose overlay，再由运维
redeploy，不能运行时把上传代码塞进共享容器。production-reference 由 runner controller 使用最小权限
Kubernetes ServiceAccount 创建“一次调用一个 Job/Pod”，应用 Pod 不持该权限。Job 固定 image digest、
service account、seccomp/AppArmor、read-only rootfs、resources、NetworkPolicy、projected short credential，
结束即清理。S4 用预构建 reference runner 验证 IPC/结果；S11 在 production topology 验证 Job backend。
如果部署未提供合格 executable backend，远端 MCP/OpenAPI 和 declarative Skill 仍可用，但 stdio/script
明确标 `execution_backend_unavailable`，不能回退到宿主 subprocess。

### 14.2 技术栈

| 领域 | 选择 | 原因与边界 |
| --- | --- | --- |
| Python | Python 3.11+、uv、FastAPI/Pydantic、SQLAlchemy/Alembic | 与评测/Agent 生态一致；CPU scorer 放 worker |
| Web | Node.js 22 LTS + npm lockfile + React/TS/Vite/Playwright | 管理/工作台无 SEO/SSR 刚需；npm 是唯一前端包管理器，Gate 不经 uv 启动 TS 工具 |
| DB | PostgreSQL | 事务、并发、RLS、outbox；JSONL 只作导出 artifact |
| Durable | Temporal Python | 审批、定时、恢复、Child Workflow；不保存业务真相 |
| Search | OpenSearch + pinned CPU BGE | hybrid/filter/ACL；索引可重建 |
| Artifacts | S3 adapter；local-product 用 Garage | 内容寻址和企业兼容；不依赖 bucket LIST 作真相 |
| Realtime | Redis + SSE | 短期 fan-out；丢失可从 API 重建 |
| Identity/Policy | OIDC/SCIM、Keycloak local、RBAC + OPA + RLS | 联邦身份和多层 fail-close |
| Telemetry | OpenTelemetry/OTLP | 厂商中立；固定 GenAI semconv revision |
| Code knowledge | SCIP + tree-sitter | 结构/符号优先，搜索和 embedding 降级 |
| Models | custom three thin adapters | 不复制 LiteLLM，把控制权留在 context/evidence/policy |
| Deploy | Docker Compose + Kubernetes reference | 本地完整产品；生产适配不冒充实测 HA |

MinIO 仓库已于 2026-04 archive/source-only，因此不再作为默认本地对象存储；Garage 用于本地完整产品，
生产只依赖 S3-compatible contract。该替换不改变 ObjectStore port。

### 14.3 三种环境

| 环境 | 组成 | 用途/边界 |
| --- | --- | --- |
| test | Python、测试 PostgreSQL、filesystem artifact、fixture providers | 快速测试，不是产品演示 |
| local-product | reverse proxy、Web/API、PG、Temporal dev、workers、OpenSearch、Garage、Redis、Keycloak、OPA、OTel、reference MCP/OpenAPI | 真登录/多用户/ACL/运行时；fixture/replay 默认，live 显式启用 |
| production-reference | ingress/WAF、应用副本、managed/HA PG/Temporal/Search/Object/IdP/KMS/OTel | 部署参考；未做负载/恢复测试前不宣称 HA/SLO |

本地 synthetic environment 必须走真实 Run、PostgreSQL、Temporal、OPA/RLS、Knowledge、Memory、Tool
Gateway、Evidence 和 Eval，只替换外部企业系统与模型响应。Docker 启动绝不自动调用真实模型；
OpenCode Go live 需显式 connection、allowlist、数据分级、预算和 operator action。

### 14.4 数据与升级

- DB 使用 Alembic expand/migrate/contract；Temporal workflow 用显式 version marker；event/schema/tool/skill/
  Agent/Eval 均有版本迁移或兼容读取。
- OpenSearch index 用新版本重建 + alias switch；不可原地赌 mapping 兼容。
- Object 写入为 temporary upload -> digest verify -> immutable key -> PG manifest commit；orphan 由 reconciler
  按安全窗口清理。
- 备份范围包括 PG、ObjectStore、SecretBackend metadata/key procedure、Temporal persistence 配置和版本
  manifest；Redis/search 不作为唯一备份。
- CI 分 unit/property、contract、integration、security/tenant、browser、fixture E2E；live 和 destructive
  fault suite 手动触发并生成 sealed artifact。

## 15. 技术依据与选型记录

以下资料用于冻结当前设计，访问日期均为 2026-08-12。引用表示接口/风险依据，不表示第三方为本项目
背书：

- [Temporal documentation](https://docs.temporal.io/)：durable execution、Workflow/Activity、恢复语义。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 与
  [memory concepts](https://docs.langchain.com/oss/python/concepts/memory)：用于界定 checkpoint 与应用业务
  状态/长期记忆的边界。
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)：
  context selection、压缩与子 Agent 隔离的工程依据。
- [MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)、
  [security best practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)、
  [tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks) 与
  [official registry](https://registry.modelcontextprotocol.io/docs)：协议、OAuth、异步能力和目录边界。
- [Agent Skills specification](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)：
  Skill package 兼容边界。
- [Sourcegraph code search](https://sourcegraph.com/docs/code-search) 与
  [code navigation](https://sourcegraph.com/docs/code-navigation)：源原生代码检索/导航的产品参照。
- [GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)
  与 [webhooks](https://docs.github.com/en/webhooks)：代码源接入的权限和同步机制。
- [OpenSearch hybrid search](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/)、
  [filtering](https://docs.opensearch.org/latest/vector-search/filter-search-knn/)：hybrid 与 ACL filter 选型。
- [Zep/Graphiti paper](https://arxiv.org/abs/2501.13956)、[LongMemEval](https://arxiv.org/abs/2410.10813)、
  [LoCoMo](https://arxiv.org/abs/2402.17753)：时态记忆建模和外部诊断参照。
- [InsightBench](https://arxiv.org/abs/2407.06423)：数据洞察评测参照，不能替代真实风险发现验证。
- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html)、
  [SCIM RFC 7644](https://www.rfc-editor.org/rfc/rfc7644)、
  [PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)：身份和隔离依据。
- [OWASP memory poisoning discussion](https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/)：
  memory candidate、provenance、撤销和注入风险依据。
- [OpenTelemetry GenAI attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)：
  telemetry 命名依据，使用时固定版本。
- [Inspect AI eval logs](https://inspect.aisi.org.uk/eval-logs.html)、
  [Promptfoo CI/CD](https://www.promptfoo.dev/docs/integrations/ci-cd/)：sealed eval 与外部安全诊断参照。
- [MinIO archived repository](https://github.com/minio/minio) 与
  [Garage](https://github.com/deuxfleurs-org/garage)：本地对象存储替换依据。

## 16. 计划验证与降级规则

技术选型不能只靠文档判断。实施期以下 spike 必须先出最小证据：

| 风险 | 验证 | 失败后的合法降级 |
| --- | --- | --- |
| Temporal + Python/WSL crash/replay | worker kill、approval、cancel、Continue-As-New | 保持 DurableRuntime port 后重选成熟引擎；不自写半成品队列 |
| FastAPI SSE backpressure/reconnect | 慢客户端、断线、cursor、Redis loss | 经审查的 SSE gateway；不把 Redis 升为真相 |
| MCP OAuth 互操作 | discovery、PKCE、audience、refresh、revocation | 对该 provider 降为明确标注的 service credential；不 token passthrough |
| executable Skill 隔离 | filesystem/network/resource/secret escape corpus | 禁执行 scripts，只允许 declarative Skill；不在宿主 subprocess 运行 |
| OpenSearch ACL/freshness | pre-filter、post-check、delete/revoke propagation | fail closed 或 source-specific exact retrieval；不返回未授权候选 |
| local secret rotation | known-answer、AAD swap、rotate/revoke/no-leak | local-product 改用 Vault/OpenBao profile；不存明文 |
| endpoint live 资格 | 最新条款/endpoint/price/model probe/预算 preflight | fixture/replay only；不找未登记 fallback |
| **wire capture 保真性（P0）** | httpx transport 层在流式/SDK 重试/大 body 下能否稳定取得最终 body；四类篡改语料全捕获 | 改用自建反向代理捕获并以 correlation id 关联 manifest；**不得降级为 SDK 调用层 hook** |
| **token estimator 校准（P0）** | 每个 endpoint/model 用回传 usage 回归本地估算，误差分布是否稳定收敛 | 该 profile 的 context fit 固定为最保守档并标注 `calibrated_estimate` |
| **SCIP 多语言索引（P1）** | 目标语言各自的 SCIP indexer 能否在受控构建环境产出索引 | 降级 tree-sitter + 精确搜索，并**同时声明 CodeRef 精度损失**（symbol 级降为 span 级） |

前三个 spike 属于原有选型验证；后三个由 [ADR-001](../../DECISIONS.md#adr-001)、
[ADR-002](../../DECISIONS.md#adr-002) 与代码知识路径补入。wire capture 与 token estimator 两个 spike
不依赖任何前置阶段，可在 S0 之前独立执行。

降级只能收窄某个 adapter 的已验证状态，不能删掉产品所需的能力类别，也不能把未通过的安全边界包装为
“reference implementation”。**调整能力门的交付顺序不属于降级**，但必须同步收窄对应的 Claim Registry
条目并在 ROADMAP 显式记录。

## 17. 实施阶段与能力门

阶段只表达依赖，不表达周期。每阶段必须增加真实可观察行为：

| 阶段 | 纵向交付 | 关键 Gate |
| --- | --- | --- |
| S0 Foundation | contracts、PG/Object manifest/outbox、最小 Dataset/Suite/EvalRun/sealing、配置、现有 eval 兼容、local test stack | 现有资产全绿；事务/腐损/迁移测试；sealed empty Run/EvalRun |
| S1 Tenancy & Policy | OIDC/Org/Workspace/Group/User/ServiceAccount、RBAC/OPA/RLS、audit | 多角色 journey；IDOR/RLS/CSRF/revoke/OPA-down fail closed |
| S2 Runtime | Agent/SolutionPack version、Task Graph、Temporal Run、SSE、approval/cancel/retry | fixture planner 跑完整 Run；crash/retry/effect_unknown/10 并发不丢终态 |
| S3 Models & Context | profiles、三 transport、Context Compiler、reducer、handoff/manifests | fixture matrix；actual-wire tamper；authoritative 完整或 refusal |
| S4 Capability Hub | registry/admission、MCP/OpenAPI/Skills/SDK、Connection、Tool gateway | 每类 reference；OAuth/SSRF/injection/secret/capability drift corpus |
| S5 Knowledge | Source Ledger、doc/code/GitHub/DB、OpenSearch、ACL/freshness/Context Graph | 跨源 Ask retrieval；ACL/delete/freshness；code/GitHub 独立 suite |
| S6 Evidence & Ask | Evidence types/verifier、Ask pack、Workbench renderer | cross-source Answer；tamper 全捕获；partial/abstain；Case creation |
| S7 Memory | MemoryRecord、policy、retrieval、Memory Center、Case memory | conflict/confirm/revoke/delete/poisoning；后台 Discover 无个人记忆 |
| S8 Discover & Actions | DiscoveryProgram、detectors、hypothesis/falsification/dedupe、Case/action | trigger 到 HumanResolution；审批/ActionReceipt；Risk eval 口径正确 |
| S9 Eval/Release/Telemetry | layered suites、sealed run、Claim Registry、OTel、cost/router、canary/rollback | 同 runtime；blind/external/fault；无证据声明被 release gate 阻断 |
| S10 Studio & Third App | Studio、Capability/Knowledge/Admin UI、ChangeBrief pack | 新 App 无 Core 分支；Builder 完成 build-eval-publish-use journey |
| S11 Production Reference | local-product Compose、K8s reference、upgrade/backup/restore/load/fault | clean install；恢复/并发/安全报告；只发布有 artifact 的 SLO/数字 |

依赖主链：`S0 -> S1 -> S2 -> S3 -> S4 -> S5 -> S6 -> S7 -> S8 -> S9 -> S10 -> S11`。
允许在不依赖尚未冻结 schema 的前提下并行实现 UI renderer、评测数据和 reference integrations，但任何
阶段不得绕过前置 Gate 对外宣称完成。

## 18. 设计完成判据

本文已明确回答：

- 产品是多用户 Web Agent 平台，不是终端 Demo；Agent Core 是产品，Ask/Discover 是首批 App。
- 新 Tool/MCP/Skill 从哪里来，如何准入、连接、鉴权、运行、更新和撤销。
- Agent 如何用 Task Graph/Temporal 编排，如何委托子 Agent，如何处理审批和副作用不确定性。
- context、knowledge、memory、profile 各自是什么，如何压缩、跨模型、冲突、删除和验证。
- 文档、代码/GitHub、数据库和 API 如何成为 source-native Knowledge，并在 ACL/时态下提供 Evidence。
- 组织、Workspace、用户、服务身份、AgentIdentity 和 connection 权限如何相交。
- Ask、Discover、ChangeBrief 如何只通过公共 Core 机制构建。
- 哪些数字已验证，哪些只是资产/配置，哪些必须等 sealed live/fault/security artifact。

后续 Codex/Claude Code 若发现契约无法实现，必须先提交最小反例、受影响 invariant、候选方案和迁移
影响，再修改本文；不得在代码中静默创造第二套架构。
