# 模型注册、路由与 Canonical Context

> 本文 §1-§7、§9 是 provider-neutral 规范；§8 是当前已配置的 endpoint 实例附录，可替换、可移除，
> 移除后规范部分不变（见 [ADR-010](DECISIONS.md#adr-010)）。实现状态仍为计划。

## 1. 分层

| 层 | 职责 | 不负责 |
| --- | --- | --- |
| EndpointProfile | origin、transport、auth/Connection、data class、region、retention/terms、price source | 模型能力猜测 |
| ModelProfile | model id、context/output limits、tools/structured output/vision/reasoning 能力、来源 | 网络发送、业务路由 |
| CapabilityAttestation | 某日期/endpoint/model/feature 的 fixture/live probe 证据与有效期 | 回写 immutable profile |
| TokenCounter | 该 profile 的 context fit 计数级别与校准误差分布 | 成本判断、路由决策 |
| Transport | wire serialization、stream/tool/error normalization、pre-send wire capture | context 选择、policy、fallback |
| ModelRouter | 合规、能力、context fit、质量资格、延迟顺序选择 | 绕过 AgentVersion allowlist |
| Context Compiler | 从 canonical state/Knowledge/Memory/Tools 生成实际请求 | 保存 transcript 为真相 |

业务 runtime 只依赖 provider-neutral request/result/tool events，不直接导入 provider SDK response。

## 2. 三种协议

首版只实现项目实际需要的协议：

| transport | 典型 path | 必测差异 |
| --- | --- | --- |
| `openai_chat` | `/chat/completions` | messages、tool_calls、JSON mode、stream chunks |
| `openai_responses` | `/responses` | input/output items、function calls、reasoning metadata、stream events |
| `anthropic_messages` | `/messages` | system、content blocks、tool_use/result、thinking blocks、stream events |

这不是重做 LiteLLM。adapter 只做协议差异；项目能力来自 profile/attestation、canonical state、policy/
budget 和 actual-wire manifest 在同一 Run 中闭环。任何协议只有 fixture contract 通过时标
`fixture_tested`，受限实网 probe 通过才标对应 endpoint/model 的 `transport_verified`。

## 3. Profile 与资格

```text
effective capability = immutable profile claim + latest valid attestation
```

未知字段为 `null` 并 fail closed，不取“常见默认”。attestation 绑定 profile/endpoint digest、probe suite、
observed_at、expires_at、raw scrubbed artifact；过期或 profile 更新后自动失效，不回写 profile。

资格分层：`declared -> fixture_tested -> transport_verified -> agent_task_verified`。handoff 是有向 edge +
task suite 的资格，不是单模型徽章；某次 EvalRun 完整/无效也不回写全局 profile。

## 4. Context Compiler

输入：canonical projection、AgentVersion/Profile/Policy、当前 Task、authorized Knowledge、scoped Memory、
allowed Tools、recent conversation。输出顺序：

1. 建 authoritative/conversational/recoverable/opaque inventory。
2. policy/classification 过滤 model egress，确定 completion obligations。
3. 按 §7 的计数级别计算 system/schema/tool/knowledge/memory/conversation/output reserve。
4. 先引用大 tool/result artifact，再移除 recoverable 正文，再摘要旧 conversation。
5. 仍不足时 task split 或选择允许的更大 context profile。
6. authoritative 仍装不下则 `context_refusal`。
7. 生成 provider-neutral Context IR，transport 序列化。
8. pre-send capture 实际 wire body，复核 inventory/classification/digest 后发送。

第 4-5 步每级最多尝试 `max_compaction_attempts`（默认 3）。达到上界仍不满足即进入 refusal，
**不允许循环压缩**；上界与每级尝试记录写入 `ContextManifest`，使拒绝原因可解释。

`context_refusal` 不是死胡同，有两条留痕出口（[ADR-007](DECISIONS.md#adr-007)）：

- **显式授权降级**：向触发者/Approver 展示将被丢弃的 authoritative 条目清单，确认后以新 Attempt 执行，
  manifest 标 `authoritative_waived`，写 audit + canonical event；该 Attempt 产出的 claim 强制降级为
  Inference，不得标 Fact。
- **epoch 回退**：回退到上一个 `ContextEpoch` 并改选更大 context 的 ModelProfile 重试，生成新
  `TransitionManifest`，不复用旧 Attempt 的 manifest。

opaque hidden reasoning 只可在当前 Attempt 内按 provider 语义临时继续，任何 terminal state 后销毁正文；
不写 Event、Memory、Evidence 或 TransitionManifest。

### 4.1 wire capture 的实现位置

pre-send capture **必须发生在 HTTP transport 层**（自定义 httpx transport 的
`handle_async_request`），不得挂在 SDK 调用层——SDK 内部重试会重新序列化 body，流式与非流式序列化
路径也不同，挂错层会让 `wire_digest` 成为无效证据。配套三条约束（[ADR-001](DECISIONS.md#adr-001)）：

- provider SDK 一律 `max_retries=0`；重试上移为显式新 Attempt + 新 ContextManifest，使
  「一次发送 = 一个 manifest」成为结构不变量。
- capture 与 send 在同一 transport 调用内完成，digest 计算失败即拒绝发送，不允许先发后补记。
- 该 transport 是唯一出网路径；架构测试断言所有 provider SDK 实例都注入了受控 transport。

## 5. 跨模型与压缩

模型切换是显式 ContextEpoch transition：

- `TransitionManifest` 绑定 source head、target profile、authoritative inventory、projection/transform 和结果。
- `ContextManifest` 为每个 Attempt 绑定 source refs、Context IR、omit/transform、token estimate 与实际 wire。
- `authoritative_state_preservation_rate=100%` 是“结构完整或拒绝”的 invariant，不是模型效果。
- task continuation、answer/evidence quality、constraint/entity retention、cost/latency 在独立 handoff suite 测。
- 旧每 edge 12 条 chain 只作 pilot；正式样本量由 pilot variance 和最小有意义效应冻结。

## 6. 路由与 fallback

路由顺序固定：

```text
Endpoint/data compliance
→ required capabilities
→ context fit / authoritative projection
→ task-specific quality qualification
→ [optional] organization spend guard
→ latency/health preference
```

前四级是硬门禁。**spend guard 默认关闭**：token 支出的默认定位是 ROI 指标而非阻断条件（§7），
组织可显式开启它作为运维保护，但它永远排在合规与能力之后——不能因为便宜就绕过数据出站要求。

AgentVersion 声明 allowlist 和是否允许 fallback。fallback 默认关闭；若启用，必须创建新 Attempt/
ContextManifest、显示目标变化并重新做 egress 检查。不能因为便宜或 provider error 静默换到数据策略不
兼容的模型。

## 7. context fit 与 token ROI

context fit（能否装进 context window）与 token 支出是两件性质不同的事，本项目不再用同一个「budget」
概念表达（[ADR-002](DECISIONS.md#adr-002)）。

### 7.1 context fit —— 硬约束，三级计数

```text
level 1  provider 官方 count_tokens API        → authoritative_count
level 2  官方 tokenizer 本地实现（已知 vocab）  → verified_local_count
level 3  校准估算器 + 保守 margin              → calibrated_estimate
```

- 每个 ModelProfile 显式声明所处级别；**未知即 fail closed**，按 level 3 的保守 margin 处理，
  不取「常见默认」。本地 tokenizer 对 tools/schema/图像不可靠，不得当作 level 2。
- level 3 的 margin 由实测校准得到：用 provider 回传的 `usage.prompt_tokens` 对本地估算器做回归，
  per-(endpoint, model) 维护误差分布，margin 取高分位数。对不提供 counting API 的聚合 endpoint，
  这是唯一可行路径。
- **`context_length_exceeded` 类错误强制映射为 `context_refusal`**，不计入 provider failure；否则
  `authoritative_state_preservation_rate` 会变成靠错误分类洗出来的数字。该事件同时触发对应 profile 的
  estimator 重标定，并把其 context fit 判定临时降至更保守档。

### 7.2 token 会计 —— ROI 指标，不是门禁

按 Run/trajectory 而非按 call 归集（单次调用成本会掩盖整条轨迹的真实开销）：

| 指标 | 定义 |
| --- | --- |
| `weighted_tokens` | `1.0×new_input + 0.1×cache_read + 4.0×output`，权重随 profile 可配 |
| `authoritative_token_share` | authoritative 类内容占实际发送 token 的比例 |
| `evidence_per_kilotoken` | 每千 weighted token 产出的**已验证** Evidence 数 |
| `recoverable_reload_waste` | 同一 recoverable 内容在一个 Run 内被重复载入的 token 量 |
| `context_utilization` | 实际发送 token / 该 profile context window |
| `compression_ratio` | 压缩前后比，分 conversation 与 tool output 两类 |
| `cost_per_completed_task` | 整条 trajectory 加权成本 / 终态 SUCCEEDED 的任务数 |

这组指标进入 S9 sealed eval artifact，并作为 Context Compiler 消融实验的因变量——已有的 prereg、
paired bootstrap、Holm 校正方法可直接复用到「压缩策略 A vs B」这类对照上。

### 7.3 Usage Ledger

- 调度前按最坏输入/输出/tool retry/child reservation 做 reservation；provider 回传后 reconcile。
- 分开记录 `quota_value_usd`、`estimated_vendor_cost_usd`、`incremental_charge_usd` 及 confidence/source。
- 未知价格/usage 时只能记录区间或 unknown，不能给伪精确点估计。
- 滚动窗口账本只观察本项目自身用量，不能声称知道同一账号下其他来源的请求；供应商侧的余额/限额
  设置仍是最终保护。

## 8. 附录 A：当前已配置的 endpoint 实例

> 本节是配置事实，不是架构。新增/更换 endpoint 的路径是「新增一个 EndpointProfile + 走 §3 的
> attestation 分级」，不改任何 Core 代码；S3 用 architecture test 断言这一点。

当前唯一允许 live 的实例为 `opencode-go`，因开发期订阅成本选定，**不代表平台为其定制**。冻结配置在：

- `config/providers/endpoints.yaml`（origin、allowed paths、redirect policy、条款 digest）
- `config/models/opencode-go-profiles.yaml`、`config/models/opencode-go-allowlist.yaml`
- `config/pricing/opencode-go-2026-08-12.yaml`、`config/budgets/opencode-go.yaml`

运行时集合为 `frozen allowlist ∩ endpoint /models ∩ valid attestation ∩ AgentVersion allowlist`；
清单变化只创建新 profile/version，不自动放行。模型数量、各模型的 structured output/tool/thinking
差异均为配置声明或待固化 fixture，`src/` 未实现前不得写成已验证兼容。

live 前置条件：精确 endpoint 匹配、禁止跨 origin redirect、专用 Connection（**禁止通用
`OPENAI_API_KEY`**，见 `.env.example`）、供应商侧余额关闭、数据分类、条款 digest、能力 attestation、
price/usage 记录和 operator 显式动作。无 key 时 fixture-only，不 fallback 到其他付费 provider。

历史 prereg 配置预算 `$43.0231552`（FactQA `$28.47744`、Handoff `$9.2700672`、BIRD `$4.096`、
naked `$1.179648`）属于旧评测方案的配置声明，不是本平台的预算或已消费数字。

## 9. 必测契约

- 每 transport：正常/损坏 stream、tool args、JSON schema、429/5xx/timeout/cancel、usage 缺失。
- profile/attestation：过期、digest drift、unknown required capability、endpoint mismatch。
- context：压缩 priority、source inventory、actual wire tamper、classification redaction、refusal。
- **wire capture corpus**：注入四类篡改（追加隐藏 system message、静默截断 tool schema 字段、
  重试时替换 body、传输层截断），断言 pre-send gate 全部拒绝发送；断言无 manifest 的发送不可能发生。
- **token counter**：三级计数各自的契约；`context_length_exceeded` 必须映射为 refusal 而非 provider
  failure；estimator 校准后的误差分布收敛性。
- **compaction 上界**：注入超大 tool 输出，断言尝试达上界后进入 refusal 而非循环压缩；断言
  `authoritative_waived` 路径产出的 claim 无法标记为 Fact。
- router：每级过滤解释、no silent fallback、usage reservation/reconcile。
- live：只对明确 model/endpoint/date/feature 形成 attestation，不外推整个供应商或套餐。
