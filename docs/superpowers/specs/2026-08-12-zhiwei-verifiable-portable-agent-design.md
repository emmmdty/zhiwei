# ZhiWei 可验证、可迁移企业数据 Agent 总设计（历史基线）

> **已被取代：** 产品范围、运行架构、权限、存储、Web 和阶段计划以
> [企业 Agent Core 冻结设计](2026-08-12-zhiwei-enterprise-agent-platform-design.md)为准。本文仅保留旧版
> Evidence/FactQA/Canonical Context/Risk 资产的设计演进与历史口径；不得再交给开发 Agent 单独实施。

> 状态：用户已批准总设计与本地作品集 live 使用例外  
> 日期：2026-08-12  
> 目标岗位：国内校招，AI 应用 / Agent 工程  
> 发布形态：公开 GitHub + 本地 Docker + 3 分钟演示录屏

## 1. 一句话定位

知微是一套可验证、可迁移的企业数据 Agent：事实答案绑定冻结数据快照并可由第三方
独立重放；同一会话可以在异构模型之间显式接管，且每次上下文保留、结构化、压缩和
省略都有机器可读清单；系统设计通过抗污染任务、植入式风险模式和成对实验验证。

## 2. 设计原则

1. **原能力不退化**：FactQA、RiskInsight、多形态数据、抗污染基准、F2 冲突、F5
   多轮、F6 拒答与 BIRD 外部评测全部保留。
2. **证明边界清楚**：`zhiwei verify` 证明输入快照与执行结果可重放，不声称它能证明
   自然语言到 SQL 的语义正确性。
3. **状态高于聊天文本**：约束、实体、证据和冲突是结构化业务状态，不依赖某个模型
   “记住了什么”。
4. **兼容是能力协商，不是字段改名**：不同模型的协议、上下文、reasoning、工具调用、
   结构化输出和多模态能力均显式登记和实测。
5. **实验失败关闭**：没有认证的模型不进入正式跑分；超预算、上下文不明、trace 无效、
   数据版本不一致时停止，不静默 fallback。
6. **确定性优先**：能用执行、哈希、规则或 schema 判分的维度不用 LLM-as-judge。
7. **公开声明分级**：目录资格、能力验证等级和一次具体 run 的资格是三个正交字段，
   不能用一个 `supported` 混写。

## 3. 范围与非目标

### 3.1 保留的产品能力

- FactQA：自然语言到只读 SQL、多形态检索、结果绑定回答、冲突展示、拒答与追问。
- TraceRef：SQL、CSV/Excel 单元格、文档片段三类可验证证据。
- Canonical Conversation：多轮约束、实体绑定、证据连续性和显式模型切换。
- RiskInsight：确定性模式检测、带证据风险假设、规则置信和验证动作。
- Eval Harness：抗污染题集、handoff 题集、风险题集、消融、失效分析、BIRD 适配。
- 三个本地界面：对话/溯源、风险假设、实验报告。

### 3.2 明确删除

- 登录、邀请制账号、四角色 RBAC、用户/组管理。
- 模型管理页面、数据源管理页面、项目列表页面。
- 自动模型 fallback。重试不得改变模型；切换必须由用户或预注册实验配置显式发起。
- 外部搜索、反封锁、代理池、通用 OpenAPI 工具、OCR/VLM 提取阶梯。
- 多租户、云托管、生产级密钥托管、调度监控和自动风控决策。

### 3.3 资源非依赖

- 本地 CPU 完成开发、确定性测试、FixtureTape/ReplayTape 回放与 Docker 演示。
- OpenCode Go 是唯一 live 端点，严格限制在套餐内；无 Key 时只用 FixtureTape，绝不改用
  其他付费端点。
- `gpu-4090` 仅允许作为本地 embedding/reranker 对照的可选实验；没有它不影响任何 Gate。
- `gpu-5090` 当前不纳入计划。

### 3.4 规范优先级与旧文档迁移

本设计经用户书面复核后成为开发期唯一规范源。在复核和同步完成前，**不得开始
`src/` 实现**。迁移顺序和冲突处理如下：

| 旧资产 | 本设计的规范结论 | 同步动作 |
| --- | --- | --- |
| `EXPERIMENTS.md` | B0 + 13 个单变量变体，共 14 个唯一配置 | 删除 19/7 口径，写入精确注册表与配对统计 |
| `BENCHMARK.md` | 答案值由执行派生，不是零人工标注；抗污染协议见下文 | 保留 L1-L4，自曝漏洞不得删 |
| `RISK_EVAL.md` | 六类模式、realized SNR、clean/planted 配对、seed 聚类统计 | 移除手填难度作为判据 |
| `PRODUCT.md` | Evidence Contract 是叙事核心，handoff 是增强层 | 删除登录/RBAC/fallback 与未实现承诺 |
| `ROADMAP.md` 与 Specs | 采用第 14 节命令级 Gate | 不得保留“功能完成即过门” |
| `README.md` / `evals/README.md` | 只陈述已通过 Gate 并有 artifact 的能力 | 实现前先标 design-only，发布时由检查器生成状态表 |

若旧文档与本设计冲突，以本设计为准；若代码、测试和本设计冲突，Gate 必须失败，不能
由实现者自行选择较宽松口径。

抗污染协议的规范版本如下：

1. 公开小说先生成冻结 base snapshot，再由声明式 transform 生成 perturbed snapshot。
   **发布语料就是这份 perturbed snapshot**；仓库不物化第二份 original snapshot，因此
   不存在“在两份 snapshot 之间选分区”这件事。base 数据与 transform 声明一起入库，
   validator 用它们复现发布语料，从而使扰动可审计。
2. 每个可评测题记录 `template_id`、source SQL、snapshot digest 和答案规范值；
   手工题只表示 NL-to-SQL 映射由人设计，答案值仍在生成时执行得到。
3. 裸模型污染诊断是一个**独立注册的 suite**（`naked_baseline`，见 `evals/configs/prereg.yaml`）：
   只给题面不给数据，`replicates=3`，按 `world_knowledge_stable|data_dependent|mixed` 分桶。
   分桶结果留在该 sealed run 内、按 `question_id` join，**不回写进已锁定的题集资产**。
4. F2 必须同时含真实跨源冲突和数值一致的负例；F6 必须没有足够证据且以拒答契约判分。
5. template-generated 与 manual 集合分层报告；不得把同模板改写后的题随机拆到互相独立的
   train/test 口径中。L1-L4 作为已知限制随每个正式报告发布。

确认性总体固定为**全部 112 个 `independence_unit_id`**，在任何 live 调用前冻结。单轮题的
unit 是一个 `question_id`（108 个）；F5 的 unit 是完整 `chain_id`（4 条），其 confirmatory
binary outcome 只有“所有计分轮均正确”才为 1。McNemar 的 pair 由“同一 independence unit
在 B0 与 variant 下的结果”构成。

裸模型诊断**只作披露，不改变分母**。按模型表现事后剔题是 post-hoc selection：它会让
确认性总体依赖于被比较系统的表现，从而破坏预注册。诊断区间以 `template_id`（57 个）为
cluster 做 bootstrap；若做检验，用预注册的 cluster-level sign-flip randomization test，
不使用普通 McNemar。

题行上的 `targets_perturbed_field` 是**题目属性**——这道题问的字段是否被扰动过，只在
F4（24 题）为真。它不是数据集分区，任何实现都不得用它筛选确认性样本；`prereg.yaml` 的
`dataset.row_attribute_not_a_partition` 显式登记了这条禁令。

执行单位与分析单位必须分开记账：一行题 = 一次 solver 调用 = 一个执行 cell（120 行）；
一个 independence unit = 一个确认性观测（112 个）。14 个配置对应 1,680 个执行 cell 与
1,568 个分析 cell，两者不相等是正常的，把它们混成同一个数才是错误。

## 4. 总体架构

```text
Web / CLI
   |
   v
Application Service
   |-- FactQA ---------------- SQL / Report / Document adapters
   |-- RiskInsight ----------- deterministic pattern extractors
   |-- Conversation ---------- canonical state + explicit handoff
   |
   +--> Model Gateway
   |      |-- OpenAI Chat transport
   |      |-- OpenAI Responses transport
   |      `-- Anthropic Messages transport
   |          + EndpointProfile + ModelProfile + capability attestation
   |
   +--> Evidence Service
   |      |-- QueryReplay
   |      |-- CellRef
   |      `-- DocRef
   |          + immutable snapshot + claim binding + verify CLI
   |
   `--> Local Run Store
          events.jsonl + samples.jsonl + run.json + SQLite index

Eval Harness
   Dataset -> Solver -> Scorer -> Run Artifact -> Paired Analysis -> Report
```

模块之间只交换 Pydantic 契约。provider wire payload、SQLAlchemy 对象、HTTP response 和
前端 DTO 不跨模块泄漏。

## 5. 模型兼容层

### 5.1 为什么不用 LiteLLM 作为核心

LiteLLM 适合通用网关，但它主要统一调用形式。知微要回答的核心问题是：某个模型真正
支持什么、一个模型产生的状态如何安全地交给另一个模型、上下文投影损失如何审计。
这些仍需在应用层实现。项目借鉴 LiteLLM 的行业接口，但不把核心贡献外包给它。

### 5.2 三层边界

#### Transport

只负责 wire protocol，不做业务判断：

| Transport | 路径形态 | 典型 Go 模型 |
| --- | --- | --- |
| `openai_chat` | `/chat/completions` | DeepSeek、GLM、Kimi、MiMo、Hy3、Grok |
| `openai_responses` | `/responses` | GPT 5.6 Luna |
| `anthropic_messages` | `/messages` | MiniMax、Qwen |

Transport 使用 OpenAI 与 Anthropic 官方 Python SDK；统一的 timeout、stream event、错误
分类和 request id 由薄 adapter 处理。业务代码禁止直接导入 provider SDK。

#### EndpointProfile

描述鉴权与端点，不描述模型能力：

```yaml
id: opencode-go
base_url: https://opencode.ai/zen/go/v1
credential_env: OPENCODE_GO_API_KEY
credential_mode: bearer
billing_mode: subscription_allowance
allowed_paths: [/chat/completions, /responses, /messages, /models]
redirect_policy: deny_cross_origin
extra_spend_allowed: false
batch_eval_allowed: true
project_live_policy: allow_user_accepted_local_portfolio
docs_url: https://opencode.ai/docs/go/
docs_digest: sha256:b6b408b8a4a25a95b345a9cfb498e1afd3a990a9d47ee13152e74623b422b93a
terms_url: https://opencode.ai/legal/terms-of-service
terms_digest: sha256:e0da927d713f5ccc49511c8d1665053c94f033ddf8887eb936cf633cc82031e1
terms_reviewed_at: 2026-08-12
terms_review_due_at: 2026-09-12
privacy_url: https://opencode.ai/legal/privacy-policy
privacy_digest: sha256:fae5079c62898a22df303ad7b49f44489ff66ddbcba917d9a9d01afd4f7d1a3a
risk_acceptance_artifact: config/compliance/opencode-go-risk-acceptance.yaml
```

Go 文档公开 API endpoint，但通用 Terms 同时禁止自动或程序化提取 data/Output。自动
benchmark、缓存并保存输出至少存在直接的条款风险，因此 endpoint 可调用不能推出本项目
获准使用。用户在 2026-08-12 明确决定：本地、小规模、非商业求职作品集继续使用 Go API，
接受该已知条款风险。本设计据此不再把 provider 书面许可设为 Gate，但对外不得声称“官方
已授权”或“条款明确允许”。

`risk_acceptance_artifact` 保存用户决策日期、用途范围、Terms/docs/privacy digests 和固定
限制：只用公开/合成数据、禁止通用抓取、禁止转售输出、禁止训练竞品模型、禁止绕过限额。
Terms digest 变化时要求重新确认；`Use balance=off` 和预算门禁仍不可绕过。

MiniMax、Kimi、GLM、DeepSeek、Qwen 官方按量端点，以及阿里/火山计划端点均可配置为
profile，但本项目当前只有 `opencode-go` 允许 live。
其他 profile 默认
`batch_eval_allowed=false`、`billing_mode=external_or_unknown`，只运行 fixture；添加 Key 也
不会解除拒绝，必须先显式更新条款审查和发布范围。阿里 Token Plan 与火山 Coding Plan
面向获准的 coding tool 使用，不能因协议兼容就用于自定义后端批量评测。

`.env` 只接受专用变量 `OPENCODE_GO_API_KEY`；若检测到旧 `OPENAI_API_KEY` 只给迁移提示，
不得自动复用，避免把其他付费 Key 误发往 Go 或把 Go Key 发往厂商端点。配置用 dotenv
解析并覆盖 CRLF，不允许 shell `source` 作为运行前提。

首版必须交付下列 EndpointProfile 与 request/response fixtures；它们复用同一
`openai_chat` transport，不各写一套 client。除 Go 外全部 `project_live_policy=deny`：

| profile | base URL | credential env | `batch_eval_allowed` | 首版资格 |
| --- | --- | --- | --- | --- |
| `minimax-standard` | `https://api.minimaxi.com/v1` | `MINIMAX_API_KEY` | `null`，启用前复审条款 | fixture only |
| `kimi-standard` | `https://api.moonshot.cn/v1` | `KIMI_API_KEY` | `null`，启用前复审条款 | fixture only |
| `glm-standard` | `https://open.bigmodel.cn/api/paas/v4` | `GLM_API_KEY` | `null`，启用前复审条款 | fixture only |
| `deepseek-standard` | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `null`，启用前复审条款 | fixture only |
| `qwen-standard-cn` | `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | `null`，启用前复审条款 | fixture only |
| `alibaba-token-plan` | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `ALIBABA_TOKEN_PLAN_API_KEY` | `false` | fixture only；自定义后端/批量调用被条款禁止 |
| `volcano-coding-plan` | `https://ark.cn-beijing.volces.com/api/coding/v3` | `VOLCANO_CODING_PLAN_API_KEY` | `false` | fixture only；仅获准 coding tool 场景 |

`null` 不表示“默认允许”，而是缺少本项目版本化条款审查，因此 live preflight 必须拒绝。
这张表证明兼容层边界，不构成已实网验证或套餐可用性承诺。

#### ModelProfile

描述某个 endpoint 上某个 model id 的已知能力：

```yaml
stable_id: deepseek-v4-flash
display_id: deepseek-v4-flash
endpoint_id: opencode-go
transport: openai_chat
request_path: /chat/completions
context_window: 1000000
max_input_tokens: 606784
max_output_tokens: 393216
min_output_reserve: 4096
modalities: [text]
structured_output: json_object
tool_choice: auto
reasoning_field: reasoning_content
reasoning_roundtrip: in_flight_only
stream_usage: null
cache_usage: null
pricing: {source: opencode-go-docs-2026-08-12}
data_retention: {source: opencode-go-docs-2026-08-12}
catalog_status: allowlisted
verification_level: transport_verified
profile_source: {url: https://opencode.ai/docs/go/, fetched_at: 2026-08-12}
profile_digest: sha256:...
```

字段至少包含：上下文窗口、最大输入、最大输出、输入模态、结构化输出模式、工具调用、
reasoning 字段、流式 usage、缓存 usage、稳定 alias、端点返回的 display id、协议、价格
分段、数据保留、目录资格和验证等级。未知值不得用行业常识补齐，必须为 `null`。

能力解析顺序固定为：

```text
effective_capability = static_profile 字段 ∪ 最新有效 attestation 的 probed 字段
```

静态 profile 是**不可变的来源快照**，`profile_digest` 只覆盖它。probe 结果一律只写进
capability attestation，**绝不回写 profile**——否则每次 probe 都改变 `profile_digest`，
而 attestation 又绑定 `profile_digest`，刚生成的 attestation 会立刻自我失效。Gate 先解析
`effective_capability`，仍为 `null` 才失败关闭；开发命令可明确 skip。

`config/models/opencode-go-profiles.yaml` 的 `unresolved_before_s1_live` 必须列全“正式
live 前仍未解析、且被某个 suite 实际需要”的字段，并由 `required_capabilities_by_suite`
交叉检查。B0 与 A6 三档全部是 Qwen，FactQA planner 必须读 `structured_output`；若只登记
`max_input_tokens` 而漏掉它，14 配置 campaign 会在毫无预警的情况下整体失败关闭。

### 5.3 OpenCode Go 清单策略

官方文档在 2026-08-12 的“The current list of models includes”段明确列出以下 18 个套餐
模型。规范来源是 OpenCode Go 文档原文，
内容摘要为
`sha256:b6b408b8a4a25a95b345a9cfb498e1afd3a990a9d47ee13152e74623b422b93a`：

| model id | transport | path |
| --- | --- | --- |
| `grok-4.5` | `openai_chat` | `/chat/completions` |
| `gpt-5.6-luna` | `openai_responses` | `/responses` |
| `glm-5.2` | `openai_chat` | `/chat/completions` |
| `glm-5.1` | `openai_chat` | `/chat/completions` |
| `kimi-k3` | `openai_chat` | `/chat/completions` |
| `kimi-k2.7-code` | `openai_chat` | `/chat/completions` |
| `kimi-k2.6` | `openai_chat` | `/chat/completions` |
| `mimo-v2.5` | `openai_chat` | `/chat/completions` |
| `mimo-v2.5-pro` | `openai_chat` | `/chat/completions` |
| `minimax-m3` | `anthropic_messages` | `/messages` |
| `minimax-m2.7` | `anthropic_messages` | `/messages` |
| `qwen3.8-max` | `anthropic_messages` | `/messages` |
| `qwen3.7-max` | `anthropic_messages` | `/messages` |
| `qwen3.7-plus` | `anthropic_messages` | `/messages` |
| `qwen3.6-plus` | `anthropic_messages` | `/messages` |
| `deepseek-v4-pro` | `openai_chat` | `/chat/completions` |
| `deepseek-v4-flash` | `openai_chat` | `/chat/completions` |
| `hy3` | `openai_chat` | `/chat/completions` |

仓库保存同一份带来源 URL、抓取日期和摘要的 allowlist。运行时 `/models` 只用于发现，
采用集合交集：

```text
enabled_candidates = official_go_allowlist ∩ live_models_endpoint
```

只出现在 `/models`、未出现在上述 current-list 的模型为
`catalog_status=discovered_only` 且默认禁用。官方页面的 Endpoints/价格表同时列出了
`minimax-m2.5`，但 current-list 与请求量估算表均未列它；这是一项官方文档内部冲突，
严格 Go 模式按更窄的 current-list 失败关闭，只为它保留 fixture/profile，不发 live 请求。
官方清单变化需要显式更新 profile、来源摘要与 probe artifact，不能在运行时自动
扩大花费面。

### 5.4 三个正交资格字段

`catalog_status` 只取 `allowlisted`、`discovered_only`、`blocked`；
`verification_level` 是模型单调等级：`fixture_tested < transport_verified < agent_verified`。
handoff 是有方向的模型对属性，单独记录 edge attestation，不能写成单模型能力。
`run_qualification` 不写入静态 profile，只存在于密封 run artifact，值为
`complete` 或 `invalid`。不能把某次完整跑分反写成模型的永久属性。

EndpointProfile 的 required fields 为：`id`、`base_url`、`credential_env`、
`credential_mode`、`billing_mode`、`allowed_paths`、`redirect_policy`、
`extra_spend_allowed`、`batch_eval_allowed`、`project_live_policy`、`docs_url`、`docs_digest`、
`terms_url`、`terms_digest`、`terms_reviewed_at`、`terms_review_due_at`、`privacy_url`、
`privacy_digest`、`risk_acceptance_artifact`。
ModelProfile 的 required fields 为：`stable_id`、`display_id`、`endpoint_id`、`transport`、
`request_path`、`context_window`、`max_input_tokens`、`max_output_tokens`、
`min_output_reserve`、`modalities`、`structured_output`、`tool_choice`、`reasoning_field`、
`reasoning_roundtrip`、`stream_usage`、`cache_usage`、`pricing`、`data_retention`、
`catalog_status`、`verification_level`、`profile_source`、`profile_digest`。字段可以显式为
`null`，但不能缺键；Gate 声明所需能力对应 null 时失败关闭。

| 命令 | 最低验证等级 | 失效/过期规则 |
| --- | --- | --- |
| fixture/ReplayTape | `fixture_tested` | 不要求 Key，不产生 live 主张 |
| 交互 live 文本 | `transport_verified` | attestation 可缺失时现场 probe |
| FactQA/Risk 描述 live | `agent_verified` | 正式报告 attestation 不得早于 7 天 |
| Handoff treatment | 两端模型均 `agent_verified` | 通过后生成 edge-scoped attestation；任一 profile hash 变化即失效 |
| 正式 release run | `agent_verified` + 7 日内 attestation | 全部预注册样本终态后才可 `complete` |

表中所有 live 行都以 §11 的风险确认、计费和预算 Gate 已通过为先决条件；认证等级从不
构成 provider 授权声明。

`zhiwei models probe` 是显式 live 命令，不进入 CI。每次结果写为不可变 capability
attestation，字段固定为：`attestation_id`、`endpoint_id`、`model_stable_id`、
`observed_display_id`、`transport`、`profile_digest`（被证的静态 profile 快照）、
`sdk_versions`、`probed_at`、`expires_at`（`probed_at + 7 天`）、`test_matrix`
（逐项 pass/fail/unsupported）、`probed_capabilities`（供 `effective_capability` 合并的
字段子集）、`request_ids`、`usage` 和 `errors`。不保存密钥、认证 header 或原始 reasoning。

attestation 的有效性判据只有两条：`probed_at` 在 7 日内，且其 `profile_digest` 等于当前
静态 profile 的 digest。由于 probe 不回写 profile，正常流程下后者恒等；只有作者显式更新
来源快照时才失效，此时必须重新 probe。README 的能力矩阵由
`profile + 最新 attestation + sealed run` 派生，不人工填写状态词。

### 5.5 已实测的差异必须进入 fixture

- DeepSeek Go：`json_object` 可用；`json_schema` 不可用；auto tool call 可用；thinking
  下强制 `tool_choice` 不可用；reasoning 位于 `reasoning_content`。
- MiMo Go：严格 `json_schema`、强制 tool call、图像输入可用；reasoning 位于
  `reasoning`；低 `max_tokens` 可能全部被 reasoning 消耗而得到空 final。

客户端对空 final、`finish_reason=length`、reasoning-only、损坏 tool arguments、部分
stream、429、5xx、TLS 中断分别分类。只有明确可重试错误才重试，所有尝试都计入预算。

## 6. Canonical Conversation 与跨模型交接

### 6.1 两份状态，不做一锅消息数组

每个会话维护：

1. **Canonical Event Log**：持久、provider-neutral、可审计。
2. **Provider Turn Projection**：针对目标模型临时生成的 wire messages/items。

Canonical Event 类型分为三组：

- 输入/输出：`UserUtterance`、`AssistantFinal`、`ClarificationRequested`。
- 业务状态：`ConstraintDelta`、`EntityBindingDelta`、`EvidenceAttached`、
  `EvidenceInvalidated`、`ConflictDelta`。
- 执行控制：`AttemptStarted`、`ToolInvocation`、`ToolResult`、`AttemptTerminal`、
  `EpochTransitionRequested`、`EpochTransitionFailed`、`ContextEpochStarted`。

每条事件的公共 envelope 固定为：

```json
{
  "event_id": "uuid7",
  "session_id": "uuid7",
  "epoch_id": "uuid7",
  "sequence_no": 42,
  "turn_id": "uuid7",
  "attempt_id": "uuid7-or-null",
  "type": "ConstraintDelta",
  "status": "committed",
  "idempotency_key": "session/turn/attempt/type/logical-key",
  "payload_schema_version": "1.0.0",
  "payload": {},
  "previous_event_digest": "sha256:...",
  "event_digest": "sha256:...",
  "created_at": "2026-08-12T12:00:00Z"
}
```

`sequence_no` 在 session 内从 1 严格递增；digest 链覆盖 canonical JSON envelope（排除
`event_digest` 自身）。canonical JSONL 只收录 `status=committed` 的不可变记录；staged 数据
只存在于 attempt 临时目录。attempt 生命周期由事件序列推导，不靠原地修改 status。涉及
证据时只引用 immutable EvidenceRef，不复制可变结果。

### 6.2 权威等级

| 状态 | 权威性 | 压缩规则 |
| --- | --- | --- |
| 安全策略、schema、活动约束 | authoritative | 必须原样保留或结构化等价保留 |
| EntityBinding、冲突、EvidenceRef | authoritative | 不允许由 LLM 摘要覆盖 |
| 用户原话、最终回答 | conversational | 近期原样，较早轮可压缩 |
| 工具原始大结果 | recoverable | 保留 digest、定位和小型摘要，原文在 artifact |
| provider reasoning/continuation | opaque | 不跨模型，不作为业务状态 |

authoritative state 由独立 reducer 从 committed 事件重建，规则不可交给 LLM：

- constraint 以稳定 `constraint_id` 为键，delta 只允许 `set/update/unset/expire`；后写覆盖
  必须显式引用被替代事件，`expire` 在指定 `sequence_no` 生效。
- entity binding 以 `(scope, mention_id)` 为键，只允许 `bind/supersede/clear`；候选歧义
  未解决时不得生成唯一绑定。
- conflict 以 `conflict_id` 为键，状态只有 `open/resolved`；`resolved` 必须引用新证据和
  resolution rule，任何 answer/compaction 都不能隐式消除冲突。
- EvidenceRef 只可追加或由 `EvidenceInvalidated` 失效；invalidation payload 必含 evidence id、
  原 digest、理由和替代 ref（可为 null），不允许同 id 换内容。

同一 `idempotency_key` 重放时，payload digest 相同则返回原结果而不追加事件；不同则硬
失败。重复 tool result 以 `(session_id, turn_id, attempt_id, tool_call_id)` 去重。

### 6.3 Context Epoch

`ContextEpoch` 是一段使用同一系统基线、模型 profile、数据快照与投影规则的区间。随机
模型的重复采样一律称 `replicate`，不得称 epoch。以下变更开启新 epoch：

- 切换 provider 或 model；
- 完成上下文压缩；
- model profile 或系统策略版本变化；
- 数据快照变化。

系统基线在 epoch 内保持逐字节稳定。所有 epoch 变更共用一个两阶段协议，
`transition_kind` 只取 `model_switch|compaction|policy_change|profile_change|snapshot_change`：

1. 在旧 epoch 追加 `EpochTransitionRequested`，payload 必含 kind、target ids/digests、source
   head、source state 和 idempotency key。对于 compaction，target conversation artifact
   先为空；其他变更未变化的 digest 必须逐字节沿用。
2. 构造不含当前用户新输入的 `ProjectionPlan`，列出完整 source inventory、预算和映射
   规则，由独立 validator 从 source head 复算。compaction 另生成 content-addressed
   compaction artifact；它是 plan 的输入之一。验证结果形成不含 attempt/wire 的
   `TransitionManifest`。
3. 验证成功只追加一条 `ContextEpochStarted`，该事件属于**新 epoch 且必须是该 epoch 的
   `sequence_in_epoch=1`**；失败则只在旧 epoch 追加 `EpochTransitionFailed`，活动 epoch
   不变。没有独立 `ContextCompacted` 成功事件，避免它的 epoch 归属歧义。

`ContextEpochStarted` payload 固定包含：`transition_kind`、`previous_epoch_id`、
`source_log_head_digest`、`source_state_digest`、`system_policy_digest`、
`tool_schema_digest`、`data_snapshot_digest`、`model_profile_digest`、
`projection_rule_digest`、`projection_plan_digest`、`transition_manifest_digest`，以及 compaction
时非空的 `compaction_artifact_digest`。切换失败、压缩失败、policy/profile/snapshot 变更
失败都只能形成 `Requested -> Failed`；成功都只能形成 `Requested(old epoch) ->
ContextEpochStarted(new epoch)`。

provider-native response id、reasoning 签名和隐藏状态只在
endpoint/model/profile/epoch 完全相同时可用。

attempt 状态机固定为 `staged -> in_flight -> committed | aborted | timed_out | cancelled`。
tool invocation/result 可以先写为 attempt 事件，但 reducer 只消费 `committed` attempt 内的
业务 delta。失败、超时和取消都以 `AttemptTerminal` 封口；重试创建新 `attempt_id`，不得
覆盖旧 attempt 或伪装成同一次延迟。

### 6.4 Reasoning 处理

- 隐藏 reasoning 不作为可迁移业务记忆，也不展示为产品证据。
- DeepSeek/GLM 等在交错工具调用中要求回传 reasoning 时，只在当前 in-flight tool loop
  内临时保留并回传。
- 一轮完成后，持久化可见 final、结构化约束、工具事实和 EvidenceRef；不把原生工具
  wire history 原样带入另一个模型。
- 进程在 tool loop 中崩溃时从 canonical turn 起点重跑，不尝试伪造缺失 reasoning。
- reasoning buffer 只存在于 attempt 内存；attempt 进入 `committed`、`aborted`、
  `timed_out`、`cancelled` 任一终态时立即销毁。artifact 仅记录字段是否出现、字节数和摘要，不记录
  内容。崩溃恢复先把无终态 attempt 标为 `aborted`，再以新 attempt 重跑。

Canonical JSONL 使用 session 单写锁；每条记录先写临时文件并 `fsync`，再在锁内 append
并 `fsync`。恢复时只允许截断最后一个未完成行，随后验证 `sequence_no` 与 digest 链。
SQLite 只是可重建索引，不是事实源。run 的 sample 文件先写 `.partial`，全部预注册样本
终态后原子 rename 并生成密封 manifest；中断 run 只能 resume，不能发布。

### 6.5 投影与预算算法

每次调用前按以下固定顺序构造目标上下文：

1. 固定系统政策与工具 schema。
2. 活动数据快照、活动约束、EntityBinding、未解决冲突。
3. 当前任务相关 EvidenceRef 及可验证小结果。
4. 最近完整轮次。
5. 较早轮次的确定性结构化摘要。
6. 当前用户输入。

预算：

```text
input_budget = min(profile.max_input_tokens, profile.context_window - output_reserve)
output_reserve = max(requested_output, profile.min_output_reserve)
compact_at = min(input_budget * 0.80, input_budget - absolute_safety_margin)
```

模型没有官方 tokenizer 时使用保守的 profile estimator，并把估算与实际
`prompt_tokens` 的偏差写进 run log；后续只允许调整 estimator 的安全系数，不回改历史
结果。上下文不足时先移出 recoverable 大结果，再确定性压缩旧对话，最后才省略无关旧
轮次。authoritative 状态装不下时拒绝调用并要求新会话，绝不静默丢失约束。

### 6.6 TransitionManifest 与 ContextManifest

epoch 迁移和实际模型请求是两个生命周期，禁止共用一个自相矛盾的清单。

`TransitionManifest` 在新 epoch 开始前生成，不含 `attempt_id` 或 wire request：

```json
{
  "transition_id": "...",
  "transition_kind": "model_switch",
  "source_epoch_id": "...",
  "target_epoch_id": "...",
  "source_log_head_digest": "sha256:...",
  "source_state_digest": "sha256:...",
  "target_invariant_digests": {
    "system_policy": "sha256:...",
    "tool_schema": "sha256:...",
    "data_snapshot": "sha256:...",
    "model_profile": "sha256:...",
    "projection_rule": "sha256:..."
  },
  "projection_plan_digest": "sha256:...",
  "compaction_artifact_digest": null,
  "source_inventory_digest": "sha256:..."
}
```

transition validator 从 source head 独立重放 reducer，复算完整 inventory、target invariants、
projection plan 与 compaction artifact。通过后 `ContextEpochStarted` 只引用该
`transition_manifest_digest`；这里尚未发生模型请求。

`ContextManifest` 在目标 epoch 内**每次实际 attempt**生成，加入当前用户输入并绑定实际
SDK wire body：

```json
{
  "session_id": "...",
  "attempt_id": "...",
  "epoch_id": "...",
  "source_log_head_digest": "sha256:...",
  "source_state_digest": "sha256:...",
  "target_endpoint": "opencode-go",
  "target_model": "qwen3.7-plus",
  "profile_hash": "sha256:...",
  "source_inventory": [
    {"source_type": "event", "source_id": "...", "json_pointer": "/payload", "authority": "conversational", "digest": "sha256:..."},
    {"source_type": "policy", "source_id": "system-policy-v1", "json_pointer": "", "authority": "authoritative", "digest": "sha256:..."}
  ],
  "authoritative_inventory": [
    {"kind": "constraint", "key": "date-window", "digest": "sha256:..."}
  ],
  "estimated_input_tokens": 12345,
  "actual_wire_request_digest": "sha256:...",
  "actual_wire_request_artifact": "requests/sha256-....json",
  "transition_manifest_digest": "sha256:...",
  "input_budget": 200000,
  "items": [
    {
      "source_refs": [{"event_id": "...", "json_pointer": "/payload/value"}],
      "source_digests": ["sha256:..."],
      "action": "structured",
      "target_refs": [{"json_pointer": "/messages/2/content/0/text"}],
      "target_digests": ["sha256:..."],
      "reason": "completed tool trace lowered to verified evidence"
    }
  ]
}
```

允许的 `action` 只有 `preserved`、`structured`、`compacted`、`omitted`。两个 Manifest 自身
进入 trace，但不包含密钥、完整敏感工具结果或隐藏 reasoning。映射是 many-to-many：一个
旧事件可拆到多个 wire 位置，多个旧轮次也可汇成一个有独立 artifact 的 deterministic
compaction。

wire artifact 只包含 method、规范 path 和实际发送的 JSON body；认证 header 永不进入 artifact
或 digest。由于 formal live 仅允许 public/synthetic 数据，body 可以密封供复算。官方 SDK
使用注入的 HTTP client；pre-send hook 读取 SDK 已序列化的 request body，做 canonical JSON
后与 ProjectionPlan/ContextManifest 比较，通过才允许发往网络，因而 digest 绑定的是实际
出站 body 而不是 adapter 自己重建的近似值。hook 不记录 header。
request validator 不能信任 ContextManifest 的自报结果。它必须独立完成：从 source head 重放 reducer；
生成完整 source inventory 与 authoritative inventory；检查每个 source item 均恰有一个
可追踪 action（many-to-many mapping 可共享同一 group id），并逐项检查 authoritative item
都有 target mapping 且值等价；读取经过
secret scrub 后实际发送的 wire request artifact，校验位置和值；复算 request digest、预算
和目标 epoch。遗漏任一 authoritative key、引用不存在、wire artifact 与 manifest 不同或
transition manifest 不匹配都使请求在出网前失败。pre-send hook 取得实际序列化 body 后先
密封 ContextManifest，再追加引用其 digest 的 `AttemptStarted`；request validator 通过才
释放请求，失败则追加 `AttemptTerminal(status=aborted)`，不得产生网络调用。

### 6.7 “无感交接”的准确含义

产品可以声称：

> 当目标模型容量足以容纳 authoritative state 时，约束、实体、冲突和证据引用无损
> 交接；其他对话内容的变换由独立校验过的 ContextManifest 完整记录。

不得声称任意大小上下文、任意模型间“所有 token 无损”。不同 tokenizer、上下文窗口和
provider-native continuation 使该承诺在技术上不成立。

## 7. Evidence Contract

### 7.1 Evidence Bundle

`zhiwei verify` 的输入从单一 trace JSON 提升为版本化 Evidence Bundle：

```text
bundle/
  manifest.json
  trace.json
  claims.json
  snapshots/<content-addressed artifact>
  schemas/<schema snapshot>
```

`manifest.json` 至少记录 bundle schema、生成器版本、git revision（若存在）、数据集 id、
snapshot digest、schema digest、问题 id、query digest、result digest、文件清单与每个文件
SHA-256，以及创建时的 SQLite/SQL parser 版本、SQL dialect、collation、timezone、值规范化
器版本和只读安全策略 digest。

Bundle 有三种且不能混写：

| mode | 内容 | 允许声明 |
| --- | --- | --- |
| `embedded_public` | 小型公开 snapshot、schema、query、result、claim 全部内嵌 | 任何第三方可离线独立重放 |
| `external_snapshot` | manifest 引用公开、内容寻址且可获取的固定 snapshot | 获取成功的第三方可独立重放 |
| `private_local` | 只含组织内部可访问 snapshot locator | 仅同一信任域内重放，不作公开第三方主张 |

SHA-256 只证明 bundle 内部一致性和相对于已知 digest 的防篡改，不证明发布者身份或数据
来源真实性。正式 GitHub release 只有在 CI 用 GitHub artifact attestation 或 Sigstore 为
manifest digest 签名后，才可声明 artifact 源自该仓库 revision；未签名开发 bundle 不得作
真实性声明。

### 7.2 QueryReplay

QueryReplay 包含：

- snapshot digest 与只读数据库文件定位；
- schema digest；
- 原始 SQL、参数和规范化 SQL digest；
- 结果列名、类型与顺序语义；
- `ordered` 时的有序 row digest，`unordered` 时的 row digest multiset；
- result subset；
- claim bindings。

SQL 参数和值都使用类型化 canonical value，禁止 `str(value)`：

- `null`、boolean 和 integer 分别编码为 `null`、`true|false`、十进制整数串；
- decimal 编码为去除非必要尾零的规范十进制串，同时保留 scale/source type；
- float 编码 IEEE-754 binary64 十六进制 bits；NaN/Infinity 用显式 tag，正式数值 claim
  默认拒绝非有限值；
- text 先做 Unicode NFC，再编码 UTF-8；bytes 用 base64url；
- date/time/datetime 用 ISO-8601，datetime 必须带 offset 并规范到 UTC；
- 每个 cell 同时记录 DB declared type/affinity、column name 和 canonical value。

结果 digest 覆盖列顺序、列元数据、row 边界和值；typed 参数、SQL dialect、collation 和
timezone 同样进入 query digest。实现必须为 NULL 排序、文本 collation、decimal/float 和
时区边界写跨平台 fixture。

没有 `ORDER BY` 的查询不能依赖数据库返回顺序。若答案语义需要 Top-N/先后关系，SQL
必须显式排序；否则验证器以无序 multiset 比较。

### 7.3 Claim binding

回答中的每个事实 claim 既绑定回答文本，也绑定结果 cell、聚合结果或文档 quote。
`claims.json` 记录最终回答 UTF-8/NFC digest、claim 的 Unicode code-point `[start,end)`、
该 span 的 digest、规范 claim value 和 EvidenceRef。验证器检查：

1. 回答 digest、span 边界和 span digest 一致，claim 不是回答之外的旁挂记录；
2. claim 引用存在且属于本次重放结果；
3. 数值/枚举与 canonical value 一致；
4. quote 能在固定快照的指定 code-point 偏移定位，并校验 quote digest。

这可以证明“回答所写数字确实来自所附执行结果”，但不能证明“这条 SQL 回答了用户真正
想问的问题”。NL-to-SQL 语义由执行结果 scorer、F2/F5/F6 契约与人工错误分析评价。

### 7.4 Verify 退出码

| 退出码 | 含义 |
| ---: | --- |
| 0 | bundle 完整，快照匹配，重放结果与 claim binding 全部通过 |
| 2 | bundle/schema 版本或输入格式错误 |
| 3 | 文件、snapshot 或 manifest digest 不匹配 |
| 4 | SQL 安全策略拒绝 |
| 5 | 重放结果不一致 |
| 6 | claim binding 不成立 |
| 7 | 发布签名/attestation 缺失或不匹配（仅 `--require-attestation`） |

错误输出为结构化 JSON，可供 scorer 和前端共同消费。

## 8. FactQA 数据流

1. 用户问题进入 Canonical Conversation。
2. constraint resolver 生成显式约束和实体候选。
3. planner 按 ModelProfile 选择严格 schema 或 `json_object + Pydantic retry`。
4. schema provider 构造最小必要数据库上下文。
5. SQL parser/validator 基于 AST 拦截写操作、多语句、危险函数和无界查询。
6. 只读 connector 在固定 snapshot 上执行。
7. answerer 只从结果与已验证 quote 生成最终 claim。
8. Evidence Service 生成 bundle，`verify` 在交付前先本地自检。
9. 最终回答、约束、证据与模型快照写入 canonical log。

无结果时进入 clarification，不允许模型自由补答案。跨源值不一致时先做单位/时间口径归一，
仍不一致则双向报告并绑定两份证据。

## 9. RiskInsight 保留与加固

RiskInsight 仍是知微原功能，不并入模型兼容层。其核心保持确定性：模式检测、匹配、证据
有效性和置信规则均不用 LLM 判分。LLM 只负责把已检出模式转成可读描述和验证动作。

开发前需要修正：

1. 正式 suite 固定 10 个 seed：`20260811..20260820`。每个 seed 从同一 base 生成
   `clean` 与 `planted` 配对 snapshot；scorer manifest 不进入 Solver input contract，solver
   子进程工作目录只挂载 snapshot，发布后则连同 suite 一起公开以便复算。这是防止无意
   泄漏的逻辑隔离，不是针对恶意读取宿主仓库的安全沙箱，报告必须注明。
2. 六类模式的规范 kind 为 `trend`、`concentration`、`seasonal`、
   `baseline_deviation`、`ratio_divergence`、`compound_supplier_dependency`；P6 不再伪装
   成两个普通 signal。`metric` 对 P6 是有序 component set。
3. 生成后从 snapshot 计算 realized SNR，再由数值派生 difficulty；manifest 不接受手填
   difficulty 作为自证。定义 `robust_sigma(x)=max(1.4826*MAD(x), 1e-9)`，六类公式为：

| kind | realized SNR（仅在方向条件成立时为正，否则为 0） |
| --- | --- |
| P1 `trend` | OLS 拟合窗口首尾变化绝对值 / 拟合残差 `robust_sigma` |
| P2 `concentration` | share 的 OLS 首尾增量 / 拟合残差 `robust_sigma` |
| P3 `seasonal` | 目标窗口相对历史同月中位数的绝对中位偏差 / 历史季节残差 `robust_sigma` |
| P4 `baseline_deviation` | post-window 中位数与 pre-window 中位数之差 / pre-window `robust_sigma` |
| P5 `ratio_divergence` | `min(delta_revenue/sigma_revenue, -delta_cashflow/sigma_cashflow)` |
| P6 `compound_supplier_dependency` | `min((max_share-share_threshold)/sigma_share, -delta_on_time/sigma_on_time)` |

   每条 pattern 还记录公式版本、方向、pre/target window、阈值和中间量。难度固定为 hard
   `[0.8,1.5)`、medium `[1.5,3.0)`、easy `[3.0,+inf)`；distractor 必须 `<0.8`。边界值由
   Decimal 计算后再比较，避免平台浮点差异。
4. generator、detector、scorer 不互相导入实现；共享仅限版本化 schema、kind 枚举和单位
   定义。scorer 必须从数据独立复算 SNR，不能相信 generator manifest 中的 `snr`。
5. 每种 planted pattern 和 distractor 都有 plantability、ghost 和 counterfactual 检查：
   clean 不得越过 0.8，planted 必须落入声明档位，去除植入后必须恢复 clean 结论。
6. 缺失值、重复、单位变化必须覆盖检测核心字段，并按 pattern/seed 分层报告脏度鲁棒性。
7. 匹配边存在仅当 kind、entity `(dim,value)`、metric/component set 均相同且 window IoU
   `>=0.5`。选择最大 cardinality，再最大 total IoU 的一对一二分匹配；仍平局时按
   `(planted_id,hypothesis_id)` 字典序确定，防止一个输出重复命中。
8. 每个 kind 有独立 evidence verifier：从 EvidenceRef 重放对应公式，检查 entity、metric、
   window、最小行数、单位和方向；只匹配答案但证据不足时 recall 可命中，
   `evidence_validity=0`，二者不能合并成一个分数。
9. 主指标以 seed 为 cluster 做 paired bootstrap（clean/planted 与系统比较都保持 seed 内
   相关性），同时报告每 seed 和 pooled 值。置信校准仅在 `n>=100` 时用 5 个等频 bin
   计算 ECE；不足时只画 reliability table，不发布单一 ECE 数字。
10. LLM judge 的 `description_quality` 为可选非核心指标；必须先通过人工锚点和位置/长度/
    格式扰动可靠性测试，否则不发布。

报告继续声明：合成检出率不等于真实经营风险预测能力，生成与检测共享概念定义造成的
结构性优势无法被完全消除。

## 10. Eval Harness

### 10.1 核心抽象

借鉴 Inspect AI，但保持项目内薄实现：

- `Dataset`：版本化样本与 target。
- `Solver`：空系统、裸模型、ZhiWei、ReplayTape。
- `Scorer`：执行值、集合、冲突、拒答、trace、handoff、risk。
- `EvalRun`：冻结 task/model/config/dataset/code snapshot。
- `RunArtifact`：不可变原始记录。
- `Analyzer`：统计、失效分类与报告。

每次 run 目录：

```text
evals/runs/<run-id>/
  run.json
  samples.jsonl
  events.jsonl
  capability-attestations/
  context-manifests/
  summary.json
```

`run.json` 必含 dataset digest、solver config digest、model profile digest、prompt digest、
代码 revision 或 source tree digest、live/replay 标记、开始/结束时间、预算、usage、失败数、
环境版本与限制声明。正式 run 在启动时冻结 `sample_id x replicate_id` 清单；每个单元必须
进入 `passed/failed/provider_error/refusal/context_refusal` 之一的终态。预算耗尽或进程中断
只是 run 状态，不是样本终态；未全部终态的 run 只能 `partial` 并 resume，不能生成正式
summary。只有 sealed manifest 覆盖全部文件 digest 后才有 `run_qualification=complete`。

跨额度窗口的正式实验由不可变 `campaign.json` 注册：列出完整配置×执行单位、确定性
分片规则、预期 child run、逐 child 的预算上限和 sealed manifest digest。

分片按**执行单位**（题行 `sample_id`）做，因为一行题就是一次 solver 调用；配对统计再按
`independence_unit_id` 聚合。FactQA 因而是 `14 × 120 = 1,680` 个执行 cell 与
`14 × 112 = 1,568` 个分析 cell：除 A6-2 外每个配置一个 120 行 child，A6-2 按 `sample_id`
字典序分成两个 60 行 child（同一条 F5 chain 的三行必须落在同一 child，否则该 chain 的
确认性结果会跨 run 拼接），共 15 个 child，最大 child 保守成本 `$4.3776`。

Handoff 按**边**分片：3 条预注册有向边各一个 child run，最大边 `$4.227072`，合计
`$9.2700672`。它不能与 FactQA 合并成一个 run，也不能三条边共用一个 `$10` 上限——
三边合计已超过单 run 上限，preflight 会直接拒绝启动。

Analyzer 只有在全部 child sealed、每个注册执行 cell 恰好出现一次且 campaign digest 一致时
才生成正式报告；partial child 只能跨窗口 resume，不能被当作完整 campaign 的缩小分母。

### 10.2 Live 与 Replay

- CI 只跑 deterministic + fixture + ReplayTape，不使用密钥。
- live response cache key 是规范化完整请求 digest；命中必须记录，不伪装成 live latency。
- ReplayTape 只用于代码回归和 scorer 重算；模型质量报告必须标 live。
- 网络错误、限额错误和模型拒绝是样本结果，不得从分母静默删除。
- 可恢复 run 根据 sample id 跳过已完成项，重试保留原 attempt 和 usage。
- UI 和报告把响应来源明确标为 `Live`、`Cached live`、`ReplayTape` 或 `FixtureTape`，
  四者延迟与成本不得
  混合统计。

### 10.3 题集

1. **FactQA 120 题 / 112 units**：保留 F1-F6，纠正文案为“执行派生答案值”。
2. **Naked Baseline**：与 FactQA 同题面、不给数据、`replicates=3`，用于污染披露。
   它是 diagnostic，不改变 FactQA 的确认性分母。
3. **Handoff Suite**：从 F5 扩展 SQL 生成 chain，覆盖实体指代、时间/分类约束、跨源
   冲突、证据追踪和无关话题切换。
4. **Risk Suite**：多个冻结 seed、planted patterns、distractors 和 counterfactuals。
5. **BIRD Mini-Dev**：使用官方评测脚本与数据获取说明，不提交第三方数据本体。

Dataset schema 强制每个 sample 记录 `template_id`、`independence_unit_id` 与
`unit_kind=single|chain`。**这三个字段随资产一起冻结在题集 JSONL 里，由生成器写出，
不由下游 loader 推断**——`template_id` 无法从题面正则还原，而 chain 归属一旦猜错就是
直接的伪重复。注册器校验普通单轮 unit 只有一个确认性观测；F5 同 chain 的多个 turn 只
汇总成一个 `all_scored_turns_correct`。逐 turn accuracy 仍可分 F1-F6 描述，但不得进入
普通 McNemar。

### 10.4 Handoff 评测

不做 18 个 allowlisted 模型的全排列。认证分两层：

- 18 个 allowlisted profile 全部跑 fixture transport conformance；只有被实验引用的模型才
  运行 live agent conformance，未实测模型保持 `fixture_tested`。
- 正式 handoff 选择覆盖三种协议和两种 reasoning 形态的代表模型，使用预注册有向切换边。

每条 chain 在固定 switch turn 之前只执行一次，得到内容寻址的 frozen A-prefix artifact；
所有 treatment 从同一 prefix 重放，不允许各跑一遍前半段。**prefix 本身是 live 调用**，
必须计入预算：每边的保守量为 `12 chain × (4 次 prefix attempt + 3 treatment × 6 attempt)`，
按各 arm 实际模型定价，逐边登记在 `prereg.yaml` 的
`handoff.live_budget.worst_case_quota_value_usd_by_edge`。评测拆成两个 estimand：

1. **switch effect**：`canonical A->B` 对 `canonical A->A`，二者使用相同 prefix、投影器、
   token budget 和后续题，只改变目标 model。该差值回答“换模型带来多少净变化”。
2. **handoff method effect**：`canonical A->B` 对 `transcript-only A->B`，二者使用相同
   prefix、目标 B、profile、采样参数和后续题，只改变交接表示。该差值回答“结构化
   canonical handoff 是否优于聊天 transcript”。

正式 prereg 边固定为 `deepseek-v4-flash -> gpt-5.6-luna`（chat -> responses）、
`deepseek-v4-flash -> qwen3.7-plus`（chat -> messages）和
`qwen3.7-plus -> gpt-5.6-luna`（messages -> responses）。某条边两端未达最低认证时整条边
失败关闭，不以其他边替代；修改边集合必须形成新 prereg version，旧结果保留。核心指标：

- `handoff_answer_accuracy`
- `constraint_retention_rate`
- `entity_binding_retention_rate`
- `evidence_continuity_rate`
- `handoff_replay_success_rate`
- `authoritative_state_preservation_rate`
- `compaction_rate`
- `cost_per_chain`
- `p95_latency_per_turn`

`ContextManifest` 可直接确定 authoritative state 是否被保留，答案与证据用现有 scorer 判分。
其中 `authoritative_state_preservation_rate` 是结构 Gate：正常调用应为 100%，装不下则计
`context_refusal`，不能把它包装成模型理解能力。模型是否真正利用交接状态只能由后续
constraint/entity/evidence/answer 指标和 method-effect 对照判断。

两个 estimand 的唯一 confirmatory binary primary 都是 chain 末轮的
`handoff_answer_accuracy`。Holm family 精确为：

- switch family：上述 3 条 edge 各一个 `canonical A->B vs canonical A->A` McNemar p 值；
- method family：上述 3 条 edge 各一个 `canonical A->B vs transcript-only A->B` McNemar
  p 值。

每个 family 恰有 3 个 comparison，双侧 `alpha=0.05`。每边 12 条 chain 是设计上的检验力
上限，报告必须显式声明：这个样本量只能检出很大的效应，**“未达显著”不等于两种交接方式
等价**，不得用零结果反向宣称 transcript-only 足够好。`prereg.yaml` 以
`handoff.power_disclosure_required` 登记该义务。

constraint/entity/evidence continuity、replay、结构保留、compaction、cost、latency 全部是
secondary/exploratory：二元/比例给 chain-cluster bootstrap 95% CI，成本给 chain-cluster
mean/median delta CI，p95 latency 给 chain-cluster percentile bootstrap。它们不报
confirmatory p 值，不使用“显著提升”。

### 10.5 消融注册

现有设计统一为：六个维度、13 个单变量变体、一个 B0，共 14 个唯一配置。B0 固定为
`schema=table+column+comments`、`few_shot=fixed_3`、`result_binding=on`、
`rewrite_max=2`、`retrieval=dense`、`model_tier=medium`。精确注册表为：

| id | 相对 B0 的唯一变化 |
| --- | --- |
| `B0` | 无 |
| `A1-0` | `schema=none` |
| `A1-1` | `schema=table+column` |
| `A1-3` | `schema=table+column+comments+3_samples+range` |
| `A2-0` | `few_shot=zero` |
| `A2-2` | `few_shot=retrieved_3` |
| `A3-1` | `result_binding=off` |
| `A4-0` | `rewrite_max=0` |
| `A4-1` | `rewrite_max=1` |
| `A5-0` | `retrieval=bm25` |
| `A5-2` | `retrieval=hybrid_rrf` |
| `A5-3` | `retrieval=hybrid_rrf+rerank` |
| `A6-0` | `model_tier=small` |
| `A6-2` | `model_tier=large` |

A5 因而明确包含四种检索方案：BM25、dense、hybrid RRF、hybrid RRF + reranker。默认
dense embedding 固定 `BAAI/bge-small-zh-v1.5@7999e1d3359715c523056ef9478215996d62a620`；
reranker 固定
`BAAI/bge-reranker-base@2cfc18c9415c912f9d8155881c133215df768a70`。两者均为 MIT，
CPU 是正式基线，`gpu-4090` 只可生成另列 accelerator
对照。PDF 只用仓库已生成的文本层，不引入 VLM/OCR。A6 的稳定 alias 固定为
`small=qwen3.6-plus`、`medium=qwen3.7-plus`、`large=qwen3.7-max`，并冻结实际
model/profile digest；它们是同一 Qwen lineage，但并非严格仅参数规模不同，因此 A6 只能
解释为“部署档位选择”，不得声称纯参数量因果效应。任一模型未认证时 A6 失败关闭，不用
跨家模型替代。

报告不再把“19”当价值主张。注册器必须机器检查：

1. id 唯一；
2. 每个 variant 与 B0 恰好一个实验因子不同；
3. 因子能追踪到实际装配点；
4. 配置和 primary metric 在 live run 前冻结；
5. 结果表包含质量、成本、延迟和样本级失败。

新增 context handoff 作为独立实验族，不混进原六维后宣称仍是单变量消融。

### 10.6 统计口径

- 单个比例：Wilson 95% CI + `n`。
- 确认性 FactQA 系统差异：总体是全部 112 个 `independence_unit_id`，每个 unit 一个 binary
  outcome；F5 chain 先聚合为全轮均正确。对 unit 做 10,000 次、固定统计 seed 的 paired
  bootstrap CI + McNemar exact test，双侧 `alpha=0.05`。注册器发现同 unit 多行直接进入
  McNemar 时硬失败，发现总体被按题目属性或模型表现筛过时同样硬失败。
- 裸模型 contamination diagnostic 以 `template_id`（57 个）整簇 bootstrap；若做检验则用
  预注册 cluster-level sign-flip randomization，不使用 McNemar。诊断结果只随报告披露，
  不改变确认性分母。
- 六个 core ablation primary 预注册为 `A1-0/A2-0/A3-1/A4-0/A5-0/A6-0 vs B0`，构成
  一个 Holm family；其余变体明确为 exploratory，不作确认性显著主张。
- handoff 的两个 Holm family 均只含 3 条 edge 上的 `handoff_answer_accuracy`；secondary
  指标不得塞进 family 后再挑选，也不得跨 family 挑最显著边。
- 成本差：配对 bootstrap mean/median delta。
- handoff chain 是聚类与分析单位；bootstrap 必须整条重采样。p95 latency 按 chain 聚类，
  包含该 chain 的全部 retry；ReplayTape 不报告延迟结论。
- 小题型 `n < 30`：只报描述统计，不使用“显著提升”。
- 正式 primary 使用 `temperature=0`、每 chain/treatment 一个终态结果；非确定性作为限制。
  exploratory robustness 使用 3 个 `replicate` 并按 chain 聚类，绝不称 epoch。同一 paired
  run 固定题序与并发。
- provider retry 不能删除：最终二元正确性用于 McNemar，成本和延迟累计全部 attempt；
  retry 后仍失败按错误终态计错。

### 10.7 失效分类

保留 E1-E7 作为回答失效分类，但允许 `primary + secondary`，不强迫互斥因果。规范定义和
消融映射为：

| id | 可复核判据 | 关联实验 |
| --- | --- | --- |
| E1 schema 误解 | 选错表/列，SQL 可执行但语义对象错误 | A1 |
| E2 口径歧义 | 时间、单位、去重或实体口径与 target 不同 | 无直接单变量；作为 constraint 设计缺口 |
| E3 聚合逻辑错 | GROUP BY、分母、排序或窗口逻辑错误 | A2 |
| E4 幻觉数值 | 回答 claim 不存在于 result/quote | A3 |
| E5 溯源失败 | EvidenceRef 缺失或 bundle 重放/claim binding 失败 | Evidence Contract Gate，不做可关闭消融 |
| E6 过度拒答 | snapshot 有可执行答案但系统拒答 | A4 |
| E7 检索未召回 | gold document chunk 未进入候选上下文 | A5 |

A6 是跨类部署档位对照，不强行映射到单一失效原因。新增独立 Handoff taxonomy：

| id | 含义 |
| --- | --- |
| H1 | transport/capability mismatch |
| H2 | authoritative state projection loss |
| H3 | entity/constraint resolution loss |
| H4 | compaction information loss |
| H5 | context overflow or budget refusal |

确定性信号能分类的先自动分类，剩余错题由作者人工标记。单作者标签只作探索性分析，
不得包装成高可靠 ground truth。

## 11. Go 套餐预算与失败关闭

### 11.1 远端最终保障

OpenCode 控制台的 `Use balance` 必须保持关闭。只有该设置能保证套餐耗尽后服务端阻断，
因为本地无法观察用户在其他 OpenCode 会话产生的共享用量。
2026-08-12 官方 Go 口径为 `$12/5h`、`$30/week`、`$60/month`；它们是共享套餐限额，
仓库不把这些数写死为永不过期常量，而是连同官方文档 digest 放入 EndpointProfile，过了
`terms_review_due_at` 就拒绝正式 live。

CLI 无法读取该控制台状态，不能伪称自动验证。任何 live 命令前要求操作者生成
`billing-guard.json`，逐字确认 `plan=OpenCode Go`、`use_balance=off`、允许的 endpoint 和
当前时间；声明最长 24 小时失效。缺失/过期只允许 ReplayTape。该声明是操作责任记录，
不是服务端状态证明；最终保护仍是控制台开关。

### 11.2 本地五层门禁

1. **Terms acknowledgment**：risk acceptance artifact 的 Terms/docs/privacy digest 必须与
   当前 profile 相同，且用途为 local portfolio；digest 变化时拒绝 live，等待用户重新确认。
   该 artifact 记录知情决策，不包装成 provider 许可。
2. **Endpoint allowlist**：当前版本任何 live（不只 eval）只允许规范化后精确 origin/path
   `https://opencode.ai/zen/go/v1`，拒绝重定向到 Zen 通用或厂商按量 URL。
3. **Terms/billing preflight**：Go docs、Terms、Privacy 三份 digest 审查未过期、有效
   `billing-guard.json`、`extra_spend_allowed=false` 缺一不可。其他厂商 Key 即使存在也
   默认拒绝；本项目公开的“兼容”仅指 fixture 契约。
4. **Run preflight**：基于冻结价格表、样本数、max tokens、cache 假设和重试上限计算
   worst-case；每次正式命令强制 `--quota-cap-usd 10.00` 或更低，超过不启动。该上限低于
   Go 的 `$12/5h` 窗口，但不能推断共享周/月剩余额度。
5. **Usage ledger**：每个 attempt 按实际 usage 累计；达到 hard cap 的 80% 停止调度新
   样本，保留余量给已在途请求。ledger 同时维护 5 小时/周/月三个**滚动窗口**的本地累计：
   启动任一 child 前，若 `窗口已用 + 本 child 最坏值 > 窗口额度 × 0.8`，则拒绝启动并给出
   下一个可用窗口，而不是启动后中途撞墙。单 run 上限只挡“一次跑太多”，挡不住“连着跑
   好几个”——S4 注册总额 `$43.02`、共 20 个 child，三个 `$4.38` 的 child 排进同一个 5 小时
   窗口就是 `$13.13 > $12`。该台账只记录本项目自己的请求，是保守下界而非 provider 真值。
   台账文件缺失或损坏时正式 run 失败关闭。

默认 live 并发为 1，避免额度、限流和时延互相污染。任何 retry 都使用同一 model/config，
并计入成本。价格未知或 usage 缺失时，正式跑分失败关闭；交互 demo 可显示“成本未知”，
但不能进入成本报告。预算中断的 run 保留为 partial，后续额度窗口只能 resume 原注册表。

成本字段固定为：`quota_value_usd`（Go 限额口径）、`estimated_vendor_cost_usd`（冻结公开
价格推算）和 `incremental_charge_usd`。后者只有在有效 operator assertion 下记录为
`0 (operator asserted)`，不能把套餐内额度价值写成实际账单。没有
`OPENCODE_GO_API_KEY` 缺失时系统是 fixture-only；可以完整验 bundle、上下文清单和报告
管线，不能声称现场模型切换。通过全部五层门禁录制的 ReplayTape 才可回放真实历史响应。

## 12. 产品外壳与 Docker

### 12.1 服务

- `api`：FastAPI，承载问答、显式模型选择、verify、风险和报告读取。
- `web`：React + TypeScript + Vite，三视图工作台。
- 不引入 Postgres、Redis、对象存储或第三方观测平台；SQLite 与本地 artifact 目录足够。
- API key 只进入后端容器，绝不注入前端 build args 或浏览器 storage。
- Compose host port 只绑定 `127.0.0.1`；API CORS allowlist 只有
  `http://127.0.0.1:5173`，不使用 wildcard/credentials。容器内部服务名只用于
  compose network，不暴露局域网监听。
- 每个数据集声明 `data_class=public|synthetic|private`。live Go 运行器只接受
  `public|synthetic`；`private_local` bundle 只可用本地确定性工具和 ReplayTape。本公开
  portfolio 不向第三方模型发送真实企业数据，不因 provider 隐私条款变化而放宽。

### 12.2 三个视图

1. **ChatWorkspace**：紧凑模型选择器、消息流、活动约束、证据列表与切换状态。
2. **TracePanel**：SQL/snapshot/result/claim binding、ContextManifest 和 verify 结果。
3. **Report/Risk workspace**：报告 tabs 与风险假设表，证据展开复用 TracePanel。

不做营销首屏。打开即是工作台。模型 badge 只显示认证状态；未认证模型不能进入正式
实验。切换前展示目标模型与是否需要 compaction，用户确认后产生
`EpochTransitionRequested(transition_kind=model_switch)`；
验证成功才产生新 epoch 的 `ContextEpochStarted`。

### 12.3 无密钥演示

`docker compose up` 默认使用人工定义且不冒充模型输出的 FixtureTape 和冻结 Evidence Bundle，陌生人无需 Key
即可体验完整问答、模型交接记录、trace verify、风险假设和实验报告。live 模式需要用户
显式提供 `.env`、通过五层门禁并执行单独命令，不是 Docker 启动副作用。只有
合格 sealed live run 才能生成 ReplayTape；FixtureTape 只演示交互与验证链，不
进入模型质量结论。

## 13. 测试分层

| 层 | 内容 | CI |
| --- | --- | --- |
| L0 | schema、哈希、SQL AST、安全、scorer、统计、risk、context projection | 必跑 |
| L1 | FakeTransport + provider fixtures + FixtureTape + Docker smoke；录制后追加 ReplayTape 回归 | 必跑 |
| L2 | 通过五层门禁后的 Go capability probes、小型真实模型 chain | 手动 live |
| L3 | 全量消融、handoff、BIRD、发布报告 | 手动 release |

关键属性测试：

- 任意空证据不能产生事实 claim。
- 任意篡改 snapshot/trace/claim 都让 verify 非 0。
- 任意模型切换不得省略 authoritative state；放不下必须拒绝。
- 任意 variant 与 B0 只能有一个注册因子不同。
- 任意 run 缺模型、数据、配置、成本、日期或局限字段时报告拒绝生成。

## 14. 命令级能力门

现有 S0-S7 保持，不新建平行项目。下列是最终接口契约；实施计划可以拆任务，但不能改宽
断言。`evals/assets.lock.json` 在 S0 由当前确定性资产生成，记录逐文件 digest、生成器 source
tree digest 与 schema version，之后所有 suite 先校验该锁。

| 阶段 | 必须执行的命令 | 通过断言 |
| --- | --- | --- |
| S0 | `uv sync --extra evals --extra dev`；`make evals`；`make determinism`；`uv run zhiwei assets lock --check`；`uv run ruff check .`；`uv run pyright`；`uv run pytest -m 'not live and not slow'`；`uv run zhiwei eval run --suite factqa --solver empty --mode offline --seal` | 120 题、110 项 validator、两次资产逐字节一致；fixture 覆盖三 transport；empty solver 的 120 个 sample 全终态、密封报告可重算 |
| S1 | 离线：`uv run zhiwei eval run --suite factqa-smoke --solver zhiwei --transport fake --mode fixture --seal`；live 前置：`uv run zhiwei compliance check --endpoint opencode-go --require-risk-acceptance`；`uv run zhiwei billing guard --endpoint opencode-go --confirm-use-balance-off`；`uv run zhiwei models probe --models deepseek-v4-flash,mimo-v2.5 --quota-cap-usd 0.05`；`uv run zhiwei eval run --suite factqa-smoke --solver zhiwei --model deepseek-v4-flash --mode live --quota-cap-usd 0.20 --seal` | 风险确认与五层门禁通过；离线 12 题先验 SQL/Evidence；两模型 attestation 达 `agent_verified`，12 个 live bundle 全部重放通过 |
| S2 | `uv run zhiwei eval run --suite retrieval --solver bm25,dense,hybrid-rrf,hybrid-rerank --mode offline --seal`；`uv run pytest -m slow tests/retrieval` | 四种方案使用同一 frozen chunk/index input；CSV/XLSX/DocRef 均有 replay；固定 embedding/reranker revision 与 artifact digest；不需要 GPU |
| S3 | 离线：`uv run zhiwei eval run --suite handoff-smoke --edges deepseek-v4-flash:mimo-v2.5 --mode fixture --seal`；`uv run zhiwei context verify evals/runs/<run-id>/context-manifests --all`。再执行同 edge 的 `--mode live --quota-cap-usd 0.70` | 固定 12 条 chain，同一 A-prefix 派生三 treatment；FakeTransport 先验完整 inventory、wire hook、失败保持旧 epoch；live 结果不得用 fixture 代替。cap `$0.70` 由 `prereg.handoff_smoke` 的最坏值 `$0.68124672` 派生，含 prefix attempt |
| S4 | 离线：`uv run zhiwei eval run --suite factqa --solver zhiwei --configs evals/configs/ablation/index.yaml --mode fixture --seal`；`uv run zhiwei analyze --run <run-id> --prereg evals/configs/prereg.yaml`。live 前重新执行 compliance/billing/endpoint/budget/滚动窗口全部 preflight，确认 5 个唯一模型节点均有 7 日内 attestation；先用 `zhiwei eval campaign plan` 把 FactQA 1,680 个执行 cell 冻结为 15 个 child run，再用 `zhiwei eval campaign run` 逐 child 执行 `--temperature 0 --replicates 1 --quota-cap-usd 10.00 --resume --seal`；3 条 handoff edge 各一个 child run、`naked-baseline` 与 BIRD Mini-Dev 500 题另成注册 run | 离线 Gate 证明 14 配置装配点、统计和报告可重算；live Gate 要求 FactQA 15 个 child 全部 sealed、每个注册执行 cell 恰好一次、3 条 handoff edge 全部 sealed 且两类 estimand 齐全，以及单独的 naked-baseline 与 BIRD official execution report；额度不足时只 partial/resume，不缩题集。离线 fixture 跑分用 `--solver zhiwei --mode fixture`，不存在名为 `fixture` 的 solver |
| S5 | `uv run zhiwei risk generate --seeds 20260811:20260820 --check`；`uv run zhiwei eval run --suite risk --solver risk-rules --mode offline --seal`；`uv run zhiwei risk verify <run-id> --all` | 每 seed 的 clean/planted 配对与 hidden-at-runtime manifest 齐全；六种 realized SNR、ghost/counterfactual、干扰项阈值、一对一匹配和证据公式独立复算；LLM 文案质量不是 Gate |
| S6 | `docker compose up --build -d`；`curl --fail http://127.0.0.1:8000/healthz`；`docker compose run --rm web npm run test:e2e` | 无 Key 默认 fixture-only；Playwright 在 desktop/mobile 覆盖问答、handoff manifest、篡改 verify 红灯、风险和报告；所有模型输出醒目标 `FixtureTape`，端口只在 localhost |
| S7 | `uv run zhiwei release check --require-license --require-demo-script --require-source-digests --enforce-narrative-template --forbid-unqualified-live-claims`；`docker compose config --quiet` | 无合格 run 时只接受 `release_mode=fixture_only`；五层门禁和 sealed run 齐全才接受 `live_qualified` 并另加 `--require-sealed-runs`。所有已发布数字指向合格 artifact，GitHub release workflow 附 attestation |

正式 live 子门不允许 skip：风险确认、Key、额度、模型、网络、profile 字段或条款审查
缺失时保持未通过，但不阻塞离线实现与 fixture 发行。开发者可另用 `--allow-skip-live` 做本地
冒烟，其输出必须标 `development_skip`，不能被 `release check` 接受。正式 run 的 quota cap 触发时只能 partial
并在下个额度窗口 `--resume`；不能把已完成子集当报告分母。所有 live suite 仅接受
`public|synthetic` 资产。

### 14.1 CLI 契约（唯一权威表）

本表是 CLI 面的唯一规范源。`docs/API.md` 与各 Spec 只可引用，不得新增或改写命令；
出现分歧以本表为准。

| 命令 | 关键参数 | 说明 |
| --- | --- | --- |
| `zhiwei assets lock` | `--check` \| `--write` | 默认 `--check`，只校验不隐式重写 |
| `zhiwei compliance check` | `--endpoint`、`--require-risk-acceptance` | 条款/隐私/文档 digest 审查 |
| `zhiwei billing guard` | `--endpoint`、`--confirm-use-balance-off` | 生成 24 小时操作者声明 |
| `zhiwei models list` | `--endpoint` | 列 allowlist ∩ `/models` 与认证等级 |
| `zhiwei models probe` | `--models`、`--quota-cap-usd` | live；产出不可变 capability attestation |
| `zhiwei verify` | `<bundle>`、`--require-attestation` | Evidence Bundle 重放，退出码 0/2/3/4/5/6/7 |
| `zhiwei context verify` | `<manifest-dir>`、`--all` | TransitionManifest / ContextManifest 独立复算 |
| `zhiwei eval run` | `--suite`、`--solver`、`--configs`、`--mode`、`--model`、`--edges`、`--temperature`、`--replicates`、`--quota-cap-usd`、`--resume`、`--seal` | `--solver` 接受逗号分隔的多值；没有 `--solvers` |
| `zhiwei eval campaign plan` | `--suite`、`--solver`、`--configs`、`--prereg`、`--max-child-worst-case-usd`、`--out` | 冻结 `campaign.json` |
| `zhiwei eval campaign run` | `--manifest`、`--mode`、`--resume`、`--seal` 及 `eval run` 的运行参数 | 逐 child 执行 |
| `zhiwei analyze` | `--run`、`--prereg` | 只读 sealed artifact 生成报告 |
| `zhiwei risk generate` | `--seeds`、`--check` | 10 seed clean/planted 生成与自检 |
| `zhiwei risk verify` | `<run-id>`、`--all` | 六类证据公式独立复算 |
| `zhiwei release check` | `--require-license`、`--require-demo-script`、`--require-source-digests`、`--enforce-narrative-template`、`--forbid-unqualified-live-claims`、`--require-sealed-runs` | 发布模式与叙事策略 |

`resume` 与 `seal` 是 `eval run` / `eval campaign run` 的**标志**，不是独立子命令。交互式
问答只由本地 Web Workspace 提供，不提供 `zhiwei ask`：它会成为第二条未被 Gate 覆盖的
产品路径，且与 Docker 演示重复。

## 15. 开源借鉴与项目增量

| 工作 | 借鉴 | 不重复实现 | 知微增量 |
| --- | --- | --- | --- |
| Inspect AI | task/solver/scorer/log | 通用 frontier eval 平台 | 面向证据重放、handoff、risk 的薄 harness |
| Promptfoo | cache/resume/CI failure | YAML 测试平台与红队套件 | sample-level evidence 与 paired analysis |
| OpenCode/Kimi CLI | context epoch/compaction/provider projection | coding agent runtime | 企业数据 authoritative state + ContextManifest |
| LiteLLM | provider capability 对照 | 百厂商网关/路由服务 | 三协议薄 adapter + 实测认证等级 |
| in-toto/OpenLineage | digest/run/dataset/version | 供应链平台/lineage server | 面向回答 claim 的 Evidence Bundle |
| BIRD/Spider | 执行结果判分 | 新 Text-to-SQL 榜单 | 自建动态题集与外部 sanity check 并列 |
| VarBench | 动态变量扰动 | 通用动态 benchmark | 跨源冲突、裸模型桶、Evidence contract 联动 |
| InsightBench | 植入式 insight | 新通用 insight benchmark | 确定性匹配、证据重放、干扰项与 realized SNR |

## 16. 对外叙事顺序

`release check` 根据 sealed run qualification 自动选择唯一模板，README 不允许作者
手工跨模板摘取话术。

**`fixture_only`（没有合格 sealed live run 时）**：

1. 可重放 Evidence Contract：展示公开 bundle、篡改后 verify 失败和真实性边界。
2. Canonical Context 验证器：用醒目标注的 FixtureTape 跨三种 wire schema 展示约束、
   证据和 omitted/compacted 映射；只声称 projection contract 通过，不写“DeepSeek 已交接”。
3. RiskInsight 离线确定性评测：展示 clean/planted、realized SNR、证据重算和 seed 区间。
4. Eval Harness：展示 14 配置装配校验、paired statistics 的 fixture golden test 与 partial/
   seal 纪律；不展示模型效果 delta、模型成本或 live latency。
5. 首屏状态明确写“Live model evaluation not yet qualified”；BIRD、live handoff 和模型
   消融结果列为未执行，不给占位分数。

**`live_qualified`（只有五层门禁 + sealed runs 均通过后启用）**：

1. 仍先展示可重放 Evidence Contract。
2. 展示真实 DeepSeek 到异协议目标模型的 handoff、TransitionManifest/ContextManifest 和
   两个预注册 estimand。
3. 展示抗污染 FactQA/14 配置的 paired delta、quota value、成本估算、live latency 与
   失效分布，每个数字链接 sealed run。
4. 展示 RiskInsight 离线结论，明确它不等于真实业务预测。
5. 最后展示 BIRD 外部分数与局限，明确自建分数不可对标公开榜单。

消融矩阵是支持证据，不再自称项目第一主交付物。第一主交付物是可由陌生人运行的
Evidence + Context + Eval 闭环。

## 17. 发布声明纪律

- “SQL 自动计算 ground truth”改为“答案值从冻结快照执行派生”；问题语义仍需人工设计。
- “第三方独立验证”仅用于 bundle 自包含或第三方可获取同 digest 快照的情况。
- `verification_level=fixture_tested` 的 provider 不得写“支持”，只能写“提供契约适配配置”。
- 合成风险 recall 不得写成业务预测准确率。
- 认证矩阵、报告和录屏中的每个数字必须链接到 immutable run artifact。
- README metadata 必含 `release_mode`；`fixture_only` 模板出现 live 模型名义结果、paired
  model delta/cost/latency 或 BIRD 分数时，`release check` 必须失败。
- 用户已选择 Apache-2.0；公开仓库的 `LICENSE`、`pyproject.toml` 和 README 必须一致，第三方
  数据、模型和依赖的许可证仍由独立 attribution gate 核验。
- live 使用以用户接受本地作品集风险为依据；任何公开材料不得声称 OpenCode 对 benchmark
  与输出归档提供了专项书面许可。

## 18. 参考资料

- [OpenCode Go 官方模型、端点、限额与隐私](https://opencode.ai/docs/go/)
- [OpenCode Go 文档源码（用于 allowlist digest）](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/web/src/content/docs/go.mdx)
- [OpenCode Terms of Service](https://opencode.ai/legal/terms-of-service)
- [OpenCode Privacy Policy](https://opencode.ai/legal/privacy-policy)
- [MiMo-V2.5 原生多模态与 1M context](https://mimo.mi.com/docs/en-US/news/latest/v2.5-open-sourced)
- [MiniMax 官方 API 概览](https://platform.minimaxi.com/docs/api-reference/api-overview)
- [Kimi API 官方概览](https://platform.moonshot.cn/docs/overview)
- [GLM 官方 HTTP API](https://docs.bigmodel.cn/cn/guide/develop/http/introduction)
- [DeepSeek 官方 OpenAI-compatible API](https://api-docs.deepseek.com/guides/json_mode/)
- [Qwen/DashScope OpenAI compatibility](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope)
- [BAAI bge-small-zh-v1.5 固定 revision](https://huggingface.co/BAAI/bge-small-zh-v1.5/commit/7999e1d3359715c523056ef9478215996d62a620)
- [BAAI bge-reranker-base 固定 revision](https://huggingface.co/BAAI/bge-reranker-base/commit/2cfc18c9415c912f9d8155881c133215df768a70)
- [Inspect AI](https://inspect.aisi.org.uk/)
- [OpenCode Context Architecture](https://github.com/anomalyco/opencode/blob/dev/CONTEXT.md)
- [Kimi CLI Sessions and Context](https://moonshotai.github.io/kimi-cli/en/guides/sessions.html)
- [LiteLLM](https://github.com/BerriAI/litellm)
- [OpenTelemetry GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions-genai)
- [in-toto Attestation Framework](https://github.com/in-toto/attestation)
- [OpenLineage Specification](https://github.com/OpenLineage/OpenLineage/blob/main/spec/OpenLineage.md)
- [InsightBench](https://arxiv.org/abs/2407.06423)
- [VarBench](https://arxiv.org/abs/2406.17681)
- [BIRD](https://bird-bench.github.io/)
- [Judging the Judges: Position Bias](https://arxiv.org/abs/2406.07791)
- [Judge Reliability Harness](https://arxiv.org/abs/2603.05399)
- [Alibaba Token Plan usage boundary](https://help.aliyun.com/en/model-studio/token-plan-personal-overview)
- [Volcano Coding Plan OpenAI-compatible gateway](https://developer.volcengine.com/articles/7616625541761597476)

## 19. 书面设计验收

该设计只有在以下条件满足后才进入逐任务实施计划：

- 用户确认跨模型增强没有改变原 FactQA/RiskInsight 产品目标。
- 用户确认在知悉 OpenCode Terms 风险后继续进行本地、小规模作品集 live 评测。
- 独立 spec reviewer 未发现阻断性契约冲突。
- 用户已确认 Apache-2.0，标准 LICENSE 与项目元数据已加入开发前资产。
- 后续 Roadmap/Specs 能从本设计派生单一口径，不再保留 19/14/7、CI 重叠、fallback、
  RBAC 或“零人工标注”等互相冲突的说法。
