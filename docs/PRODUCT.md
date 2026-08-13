# 产品章程

> 规范源：[企业 Agent Core 冻结设计](superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md)。
> 本文定义用户、产品闭环和验收，不重复底层 schema。

## 1. 产品定位

知微是面向企业内部数据与业务系统的 Agent 应用平台。Agent Core 是可独立演进的主产品；Ask、
Discover 与 ChangeBrief 是通过 Core 公共机制交付的 Agent Apps，而不是三个硬编码功能入口。

目标不是缩小成“作品集能演示的最小 RAG”，也不是先画完所有企业平台菜单。产品完整性的判据是：
一个组织能接入真实知识和能力，Builder 能构建/评测/发布 Agent，用户能完成任务并检查证据，高风险
动作能审批和审计，系统能从失败恢复，新增 App 不改 Core 专用逻辑。

## 2. 用户与作业

| 用户 | 核心作业 | 成功结果 |
| --- | --- | --- |
| Member | 使用已发布 Agent 研究、发现、协作和行动 | 得到带证据/状态/成本的结果，不必理解底层模型 |
| Builder | 用 Knowledge、Memory、Models、Tools、Skills、Task Graph 组装 Agent | 通过评测和权限 Gate 后发布 immutable version |
| Capability Publisher | 导入并治理 MCP/OpenAPI/Skill/SDK/Agent provider | 能检查、准入、连接、测试、升级、暂停和撤销 |
| Approver | 处置高风险工具调用 | 批准的 input digest 与实际执行一致，结果有 ActionReceipt |
| Memory Steward | 管理团队记忆 | candidate 有来源，冲突/撤销/删除能传播 |
| Workspace/Org Admin | 管理成员、资源、策略、成本和审计 | 跨租户失败关闭，凭据不可见，访问可解释 |
| Auditor | 回看 Run、Evidence、Policy、Action、Release claim | 同一事实源可复算且不依赖聊天截图 |

## 3. 产品闭环

```text
OIDC/SCIM 创建组织协作边界
  → Knowledge/Capability Hub 接入数据、代码、模型、工具、Skills 和 Connections
  → Studio 构建 AgentDefinition/SolutionPack，绑定权限、预算、Evidence 和 Eval
  → sandbox/eval/review/publish
  → Workbench/API 运行 durable Task Graph，必要时澄清、委托、审批
  → 结果进入 Evidence、ActionReceipt、Case、Memory candidate、Cost/Audit
  → 人类反馈和 sealed Eval 驱动新版本发布/回滚
```

任何只完成中间某个 registry 或页面、却不能进入这条闭环的交付都不算产品功能。

## 4. Agent Core 能力

### 4.1 Agent Runtime

- Case/Run/Task Graph/Attempt/ContextEpoch 的持久状态与恢复。
- typed primitives、并行只读节点、审批等待、取消、重试、ChildTask 和 Agent-as-tool。
- PostgreSQL 为业务真相，Temporal 为 durable execution；SSE/Redis 只负责实时展示。

### 4.2 Context 与模型

- authoritative/conversational/recoverable/opaque 四类状态。
- Context Compiler 先做权限/预算/压缩，再生成 provider-neutral IR 和实际 wire body。
- openai chat/responses、anthropic messages 三种薄 adapter。
- TransitionManifest 与 ContextManifest；authoritative state 完整或 `context_refusal`。

### 4.3 Knowledge 与 Memory

- 文档/表格、代码/GitHub、数据库和 API 的源原生索引、ACL、时态、新鲜度和 Source Ledger。
- user/team/case memory 的候选、确认、冲突、撤销、删除和 profile-scoped retrieval。
- Knowledge、Context、Memory、Profile/Skill 严格分层。

### 4.4 Capabilities 与治理

- MCP、OpenAPI、Agent Skills、SDK、Agent-as-tool 的目录、准入、版本和更新。
- user-delegated/workload Connection、MCP OAuth 2.1、短凭据、sandbox 和 network policy。
- Organization/Workspace/Group/User/ServiceAccount/AgentIdentity、RBAC/OPA/RLS。

### 4.5 Evidence、评测与发布

- Fact/Quote claim 的 snapshot、canonical value、answer span 和 deterministic verify。
- 写动作的 Approval/ActionReceipt/effect_unknown。
- fixture/replay/offline/live/shadow/human 同构运行，sealed EvalRun 和 Claim Registry。

## 5. 首批 Agent Apps

### 5.1 Ask

Ask 是跨企业知识源的研究 Agent，不是 chat RAG。它能够分解问题、澄清范围、检索文档/代码/
GitHub/数据库/API、调用分析工具、委托子 Agent、处理冲突并生成：

`Answer + Claim/Evidence Map + Artifacts + Execution Summary + Verification + Next Actions`。

Fact/Quote 必须可验证；Inference/Recommendation 只能声明输入证据和责任边界。证据不足必须 partial
或 abstain。

### 5.2 Discover

Discover 是持续风险发现 Agent，不是一个“点按钮跑六条规则”的页面。每个 DiscoveryProgram 绑定
risk charter、数据/实体范围、触发器、detector packs、预算、证据标准、接收人和动作策略，从 source
delta 形成 Signal/RiskHypothesis，经反证、去重和人工处置进入 Case/Action/HumanResolution。

旧 RiskInsight 是第一个 Numeric Risk Detector Pack 和确定性 reference eval，不代表平台只面向经营
数值，也不代表 planted pattern recall 等于真实风险预测率。

### 5.3 ChangeBrief

由 GitHub commit/PR 触发，输出受影响 symbol、依赖、tests、相关 issue/review 和风险的 Verified Brief。
它的产品职责是证明第三个 App 能复用同一 Trigger、Knowledge、Task Graph、Skill、Evidence 和 UI
panels，且 Core 不出现 App 名称分支。

## 6. 关键用户旅程

### Journey A：跨源知识研究

用户询问“这次结算改动为什么可能影响退款口径”。Ask 将 GitHub 代码/PR、内部规范、数据表 schema
和历史已确认团队决定组合成 Task Graph；结果逐条区分事实与推断，Evidence 面板可定位到 commit/
symbol/文档 span/query snapshot。ACL 被撤销后新请求不能命中旧索引，旧 Run 只保留受控历史记录。

### Journey B：风险发现到处置

Discover 收到数据水位更新，数据质量 Gate 后运行 deterministic detector 与受控 exploration；形成带
支持/反证的 RiskHypothesis。分析师 triage 后建 Case，请 Ask 补充代码/流程证据，再审批创建工单。
ActionReceipt 显示谁、以哪个 Agent/Tool/Connection/Policy/input digest 执行，重复提交不会创建第二单。

### Journey C：新增能力与 App

Publisher 从官方 MCP Registry 发现 server，导入到 quarantine，完成 schema/security/OAuth 测试，创建
Workspace Connection 后发布 ToolDefinition。Builder 将它与一个 Agent Skill 绑定到 ChangeBrief，跑
eval 并发布。用户能使用新 App；整个过程不修改 Core 的 app-specific 代码。

## 7. 产品级验收

| ID | 验收结果 |
| --- | --- |
| P1 | 五类角色通过真实 OIDC 完成加入组织、构建、发布、使用、审批和审计 journey |
| P2 | Ask 在同一 Run 使用文档、代码/GitHub 和结构化源，Fact/Quote 全部有有效 Evidence |
| P3 | Discover 从 trigger/watermark 到 HumanResolution/ActionReceipt，Signal、Hypothesis、Resolution 不混写 |
| P4 | 新增 ChangeBrief 只增加 SolutionPack/View，不修改 Core 的 App 名称分支 |
| P5 | MCP/OpenAPI/Skill 能导入、准入、连接、绑定、执行、升级、暂停和撤销，secret 不进入 API/log/artifact |
| P6 | 模型切换绑定实际 wire body；authoritative inventory 完整或请求拒绝；handoff 质量另行评测 |
| P7 | user/team/case memory 能确认、冲突、撤销、删除；Discover ServiceAccount 不能读取个人 memory |
| P8 | 跨 org/workspace、ACL stale、OPA down、artifact corrupt、unknown effect 全部失败关闭或得到明确终态 |
| P9 | fixture/replay/live 标签不可混淆；所有公开数字和声明链接 sealed artifact |
| P10 | local-product clean machine 一条命令启动完整本地产品，不调用真实模型且不需要 GPU |

## 8. 不以什么作为验收

- 页面、表、接口或抽象基类数量。
- fixture 对话看起来像真实模型。
- `authoritative_state_preservation_rate=100%` 被包装成模型效果。
- 120 题资产被用于证明代码知识、记忆、多租户或 Discover 的泛化。
- planted 风险模式被包装成真实企业预测准确率。
- production reference YAML 被包装成生产可用性或高可用证据。

## 9. 竞争力叙事

第一主语是完整 Agent Core：真实多租户产品能接入新知识、Tool/MCP/Skill，能用 durable Task Graph
运行并治理。第二主语是 Context/Knowledge/Memory 的清晰状态模型。第三主语才是 Evidence 与同构
评测如何把 Ask/Discover 的输出变成可反驳 artifact。单独讲 FactQA 或 RiskInsight 会把项目重新压缩成
RAG + SQL demo，应避免。
