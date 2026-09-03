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
| [ADR-011](#adr-011) | Endpoint 分级信任、运行时注册与模型热切换 | S3 | accepted |
| [ADR-012](#adr-012) | 契约测试层级、Gate 例外与读路径授权（S0–S2 复审修订） | S0–S3 | accepted |

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

#### spike-01 执行结论（2026-08-13）

代码 `spikes/wire_capture/`，证据 `spikes/wire_capture/evidence/spike-01-wire-capture.json`
（`uv run python spikes/wire_capture/run_spike.py`，退出码 0 = 全部断言通过）。
环境：`openai==2.54.0`、`httpx==0.28.1`、Python 3.11。全程无真实网络请求——流量只走 loopback
mock endpoint 或进程内 `httpx.MockTransport`。

证据构造方式：每次发送产生两份独立 digest——客户端在 `handle_async_request` 里算的，和接收端
按 `Content-Length` 读满后自己算的。断言是这两份相等。只比对 `request.content` 无法排除
「对象里有、socket 上没有」。

**1. capture 点可行——方案 C 成立，不降级到方案 B。【已验证】**

四种情况的客户端 digest 与服务端 digest 逐一相等：非流式（S1）、流式（S2）、`max_retries=2`
下的三次重试逐次对应（S3a）、~8 MiB body（S4）。pre-send gate 抛出时服务端接收 0 次（S5），
「先发了再补记」在结构上不可能。

**2. 流式路径与非流式一致。【已验证】**

`stream=True` 走同一个 `handle_async_request`，body 只多一个 `"stream":true` 字段，未见
`stream_options` 之类的额外注入。捕获层不缓冲响应，SSE delta 仍分批到达（S2，`timing_dependent`）。
**流式不需要第二套捕获路径**——本 ADR「问题」小节列的失真路径 #2（流式与非流式序列化路径不同）
在 `chat.completions` 上不成立。

**3. 无需自建反向代理。** 由第 1、2 条推出。方案 B 保留为降级预案，本 spike 未触发它。

**4. 捕获实现有两条约束，不写就等于没捕。【已验证】**

- **hash 的必须是 `request.stream` 会产出的字节，不能读 `request.content`。** httpx 把
  `content=request.stream` 交给 httpcore，`_content` 只是缓存，两者可分叉：捕获后改写
  `_content`，服务端收到的仍是原始 body（S6-T1）。正确顺序是 `aread()` → 用一个自建的
  `AsyncByteStream` 把 stream 钉死在这份 bytes 上 → 算 digest → 过 gate → 放行。
  钉定用 httpx 公开 ABC 实现，不依赖 `httpx._content` 私有符号。
- **capture 必须是最内层 transport。** 在捕获层下方再插一层并做**等长**替换，两份 digest 分叉，
  且 `Content-Length` 交叉校验拦不住（S6-T2/T3）。钉定挡不住下游替换——那只能靠架构约束。
  故 `specs/s3-models-context.md` 的「该 transport 是唯一出网路径」需收紧为
  **「CaptureTransport 之下只能是真实 HTTP transport，且 Core 不得自建 `AsyncClient`」**，
  并加 architecture test。

**5. `max_retries=0` 从「保守偏好」升级为有源码依据的结构必需。【已验证】**

`openai/_base_client.py` 的重试循环内是 `self._build_request(options, retries_taken=n)`——每次
重试**重新构造并重新序列化** request，并注入不同的 `x-stainless-retry-count`。实测一次
`create()` 对应 3 次 wire 发送（S3a），SDK 调用层只看到 1 次。本次三次发送的 body 逐字节相同，
即失真表现为「3 次发送 1 个 manifest」而非「manifest 绑错 body」；但这依赖 SDK 当前行为，
`max_retries=0` 才能把「一次发送 = 一个 manifest」变成不依赖 SDK 实现的不变量（S3b 实测恰好 1 次）。

**6. 逻辑请求与 wire body 不可互推。【已验证】** 调用方传入 key 顺序 `model, messages,
temperature`，wire 上是 `messages, model, temperature`。字段集合相同，字节不同——调用层重新
序列化算 digest 的所有变体就此出局。

**7. 大 body 不切 chunked，但整份驻留内存。【已验证 + 需新增约束】** httpx 对 `json=` 一律先
编码成 bytes，~8 MiB 与几十字节同一路径。代价是 body 全量在内存里，S3 需按 profile 设
`max_wire_body_bytes`，超限走 `context_refusal`，不是发出去再说。

**未验证边界**（不得据此对外声明）：只跑了 `chat.completions`；**Responses 协议与 anthropic SDK
未跑**；同步 transport 路径未实测（httpx `read()`/`aread()` 源码对称属阅读推断）；multipart/files
请求未覆盖（`aread()` 会把整份文件读进内存，S4 引入文件型能力时须重评）；loopback 为明文 HTTP，
digest 绑定的是 **TLS 之前的 plaintext body**——这正是所需语义，但不等于验证了 TLS 之后的字节。

#### 对 S3-T5 的最小实现骨架建议

```text
src/zhiwei/models/presend.py
    PinnedBody(httpx.AsyncByteStream, httpx.SyncByteStream)  # 只用公开 ABC
    WireCapture(frozen)      # body_sha256 / body_len / content_length / redacted headers / url
    PreSendGate(Protocol)    # (WireCapture, bytes) -> None，抛出即拒发
    CaptureTransport(httpx.AsyncBaseTransport)
```

`handle_async_request` 的顺序是硬约束，任一步换位都会让 digest 失去意义：

```text
1  body = await request.aread()          # materialize，此时序列化已全部完成
2  request.stream = PinnedBody(body)     # 钉定：唯一可被 inner 读到的就是这份 bytes
3  capture = WireCapture(...)            # digest + Content-Length 交叉校验 + header 脱敏
4  gate(capture, body)                   # Context IR / inventory / classification / 目标 profile
                                         # 任一不匹配或 digest 计算失败 → 抛出，inner 从未被调用
5  persist manifest (digest 落 PG，脱敏 body 落 ObjectStore，永不落 auth header)
6  return await self.inner.handle_async_request(request)
```

配套的三条结构约束：

- provider SDK 一律 `max_retries=0`；重试由 Runtime 上移为显式新 Attempt + 新 ContextManifest。
- 唯一的 `AsyncClient` 工厂在 `models/` 内，`transport=CaptureTransport(inner=AsyncHTTPTransport())`；
  architecture test 断言 inner 是真实 HTTP transport 且 Core 无其他 client 构造点。
- `ModelProfile.max_wire_body_bytes`，超限在 Context Compiler 阶段即 refusal。

测试侧：S3 的 wire tamper corpus 可完全用 `httpx.MockTransport` 离线跑（S7 已验证），**不需要
socket**；四类篡改各跑流式与非流式两遍。**等长篡改必须进 corpus**——长度对不上的篡改会在客户端
被 h11 以 `LocalProtocolError: Too little data for declared Content-Length` 挡在发送之前（实测），
根本走不到 digest 这一层，只测它等于没测。

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

#### spike-02 执行结论（2026-08-13）

代码 `spikes/token_calibration/`，证据 `spikes/token_calibration/evidence/spike-02-token-calibration.json`
（`uv run python spikes/token_calibration/run_spike.py`，退出码 0 = 全部断言通过）。全程无真实网络请求——provider
actual 值由确定性模拟器生成（`hashlib` 派生，跨运行逐字节一致）。

**1. 三级计数契约可行——level 3 校准估算器可安全降级。【已验证】**

用 `len(text)/4` 粗估算器 + 线性校准（`actual = scale × estimated + bias`）在 50 个样本上训练后，
测试集 MAE 显著下降：6 类内容中 3 类降至 0（json/mixed/long），其余降至个位~数十
（english_text 42.55、chinese 5.15、python 4.75，见 evidence JSON）。关键发现：**校准是
均衡器**——即使是粗糙的字符级估算器，经线性校准后也能达到近零或个位误差。对第三方聚合 endpoint（不提供 counting API 的模型），这是唯一可行路径。

**2. 99th percentile margin 覆盖最坏情况。【已验证】**

训练集 99th 分位误差作为 margin，在 held-out 测试集上覆盖率 ≥ 90%（S3）。margin=0 出现在
训练集与测试集误差均极小的情况下——这不是缺陷，而是校准充分的信号。

**3. `context_length_exceeded → context_refusal` 映射正确。【已验证】**

`classify_provider_error()` 函数将 `context_length_exceeded` 映射为 `context_refusal`（不计入
provider failure），其余错误类型映射为 `provider_failure`（S4）。该映射同时触发 estimator 重标定。

**4. 重标定可更新参数。【已验证】**

注入新样本后，scale/bias 参数更新，margin 从 0 变为 1（S5）。这验证了 ADR-002「该映射事件同时
触发对应 profile 的 estimator 重标定」的要求。

**5. fail-closed 对未知 profile 有效。【已验证】**

`token_counting_level=None` 默认走 level 3 保守 margin（S6）。未知即 fail closed，不取「常见默认」。

**6. 大输入不导致崩溃。【已验证】**

110K 字符输入在所有估算器上正常完成（S7）。

### 实现约束（由 spike 推导）

1. **hash 必须用 `hashlib`，不能用 Python `hash()`**：`hash()` 受 `PYTHONHASHSEED` 影响，跨运行
   不确定——校准参数和证据必须可复现。
2. **train/test 必须用不同 seed**：否则测试集是训练集的子集，校准误差虚假为零。
3. **level 1/2 的具体 tokenizer 验证留待 S3 实现阶段**：本 spike 验证的是 level 3 校准方法论的
   可行性，不替换真实 tokenizer。level 2（官方 tokenizer）在 S3 中通过 `tiktoken` 或 `tokenizers`
   库实现时需独立验证。

### 后果

- level 3 校准路径从「理论上可行」升级为「已验证」。S3 的 `TokenCounter` 可直接复用
  `spikes/token_calibration/calibrator.py` 中的校准逻辑。
- 对不提供 counting API 的第三方聚合 endpoint，项目有合法的降级路径，不需要为此降低产品承诺。
- 影响 `specs/s3-models-context.md`、`docs/MODELS.md` §7。

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

### 2026-09-03 增补：append 保序语义与声明边界（S0–S2 复审反例驱动）

复审发现四处实现与本决策的偏差/歧义（到达序追加、单边声明放行、运行时静默覆盖、冲突不落账），
消歧如下：

1. **append 保序 = `(task_id, attempt_no)` 的纯函数**。「按稳定 task id 保序」的准确含义：合并结果
   序列由写者的 `(task_id, attempt_no)` 决定，与事件到达/任务完成顺序无关；同一逻辑图两次执行
   （含并行调度抖动后的重放）必须产出**逐字节相同**的合并序列。按到达/完成序追加不满足本语义。
   同一 `(task_id, attempt_no)` 的重复投递按幂等去重。
2. **单边声明 = 拒绝**。发布期校验的对象是「每个潜在写者」：字段被 K 个可能并行的节点写入时，
   K 个节点都必须声明与字段一致的 merge 策略，任一写者未声明即发布失败。「仅一方声明即放行」
   不属于本决策（这正是 LangGraph「未声明即拒绝」的原义）。
3. **运行时兜底 fail closed**。运行时遇到未声明策略的写入（未经发布校验的图，如 Planner 产物），
   拒绝该事件或降级为 conflict_preserving 并落 ConflictRecord，**禁止静默覆盖**已合并值。
4. **ConflictDetected 必须作为 canonical event 落账**（含双方 task/attempt、Evidence refs、检测
   时刻），不允许只存在于内存投影——否则 Run 重放后冲突证据消失。

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

### 2026-09-03 增补：第三层「静态证明」的可判定化（S0–S2 复审反例驱动）

原第三层「发布前静态证明每条可能循环的路径上存在单调递减的界」对一般依赖图是不可判定的研究级
问题——属「写清了要求、没写清算法」的典型。消解为两个**可判定检查**加构造性论证：

| 层 | 可判定检查 | 时机 |
| --- | --- | --- |
| 静态 | 委托依赖图（AgentVersion 的 delegate 依赖 + SolutionPack 依赖 + agent-as-tool provider 边）必须为 **DAG**；任何环（含 A→B→A、经 tool provider 交替构成的环）发布失败。自委托必须显式声明并附深度上限 | 发布时 |
| 结构 | `delegation_chain` 为**共享计数**（Delegate 与 Agent-as-tool 两条路径追加同一 chain 并递增同一深度），参与 CAS；子 Run 继承并递增；硬上界为 `min(max_delegation_depth, 自委托声明上限)`——无自委托声明时即 `max_delegation_depth` | 运行时创建 ChildTask 时 |

**终止性论证**：DAG 无环 ⟹ 每条路径有限；每条委托边严格消耗剩余深度 ⟹ 深度沿路径单调递减。
原「单调递减界证明」由该构造直接成立，不再是独立的证明义务。运行时硬上界作为纵深防御保留
（防发布校验被绕过或图在发布后被篡改）。

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
   工具链，也避免每接一个 provider 就发明一个键名。这三个环境变量的**优先级高于配置文件默认值**；
   它们指向未登记 endpoint 时如何处理，由 [ADR-011](#adr-011) 定义（不是拒绝接入，而是降级信任档）。

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

<a id="adr-011"></a>

## ADR-011：Endpoint 分级信任、运行时注册与模型热切换

**影响阶段**：S3 Models & Context　**状态**：accepted　**取代**：ADR-010 第 0 条中的 allowlist 硬门禁

### 问题

ADR-010 曾规定「`OPENAI_BASE_URL` 必须与 `config/providers/endpoints.yaml` 中已登记 endpoint 的
`base_url` 完全一致，否则 fail closed」。这条规则有两个错误：

1. **企业自部署的内部 LLM 不可能预先登记在项目配置里**。vLLM/TGI/Ollama 部署在客户 VPC 内，
   base_url 由运维在部署时决定，本项目的仓库配置无从枚举。按原规则，最应该被支持的部署形态反而
   被拒绝。
2. **它把两个不同的决定绑死了**：「能不能连」是运维决定，「能发什么数据、能声称什么能力」是治理
   决定。原设计用前者来实现后者，代价是牺牲了可部署性，而治理其实并没有因此变强——一个在
   allowlist 里的公网第三方 endpoint，未必比一个内网自部署 endpoint 更该收到机密数据。

此外，原设计完全没有回答**模型热切换**：同一 base_url 下换模型，与跨 base_url 换模型，是两件
需要不同处理的事。

### 竞品调研

| 来源 | 做法 | 可借鉴点 |
| --- | --- | --- |
| [LiteLLM](https://www.litellm.ai/ai-gateway) | 自部署 proxy，用**版本化的配置文件注册** 100+ provider，自部署模型与云 API 并存；虚拟 key、预算、fallback 集中管理 | **「配置即注册」而非硬编码白名单**；自部署与云端同等公民 |
| [企业 AI Gateway 控制面](https://medium.com/@adnanmasood/llm-gateways-for-enterprise-risk-building-an-ai-control-plane-e7bed1fdcd9c) | 所有出站调用先过控制面，PII 检测、注入筛查、策略执行**在信任边界内**完成 | 门禁应设在「数据是否离开信任边界」，而不是「URL 是否在清单里」 |
| [受监管部署评估](https://predictionguard.com/blog/self-hosted-vs-cloud-llm-deployment-guide) | 受监管工作负载**常常要求**自托管、VPC 隔离或气隙部署，无外部数据出境 | 自部署是一等场景，不是例外 |
| [模型中途切换的代价](https://www.mindstudio.ai/blog/never-switch-models-mid-conversation-ai-agents) | **prompt cache 是 model+provider 特定的**，切换即丢失全部缓存（TTFT 损失 50–80%）；out-of-distribution context 是训练方式的后果，非某家实现选择 | 切换有实际成本，必须显式呈现而非静默发生 |
| [llm-switcher](https://github.com/fanqi1909/llm-switcher) | 本地 proxy 提供单一 endpoint，热切换上游而不重启客户端 | 切换应对上游透明，但本项目要求留痕 |

### 决策

#### 1. `endpoints.yaml` 从「允许清单」改为「已审查档案库」

不在库里 **≠ 不能用**，而是 **= 属性未知**。库里记录的是已经过审查的 endpoint 属性：条款 digest、
计费模式、数据分类上限、网络区域、attestation。

#### 2. 配置优先级（高 → 低）

```text
1. Run / AgentVersion binding 显式指定的 model ref     ← 产品内正式路径
2. OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY      ← 部署期 override，高于配置文件
3. endpoints.yaml 的 default_endpoint_id
```

环境变量是**部署期 override**，运维配了就生效。系统不拒绝它，但会据此决定信任档。

#### 3. 三级信任档（trust tier）

| tier | 来源 | 能力假设 | 可声称 |
| --- | --- | --- | --- |
| `reviewed` | 档案库有记录且条款 digest 未过期 | 档案声明 + 有效 attestation | 按 attestation 分级声称 |
| `operator_declared` | 运维经 env 或管理台注册并声明了属性，经 Security Admin 确认 | 全部 unknown，需 probe | 需 attestation 后方可声称 |
| `unverified` | 仅有 base_url，无任何声明 | 全部 unknown | **不可**用于任何对外能力声明 |

**接入不被阻断，被约束的是「能发什么」和「能声称什么」**——后者正是项目既有资格分层
（`declared → fixture_tested → transport_verified → agent_task_verified`）的自然延伸。未登记
endpoint 起点是 `declared`，能用，只是不能声称已验证。

#### 4. 真正的数据门禁是 network zone × classification ceiling

替代原来的 URL 白名单：

| `network_zone` | 含义 | 默认 `classification_ceiling` |
| --- | --- | --- |
| `internal` | 企业内网 / VPC / 气隙，数据不离开信任边界 | `CONFIDENTIAL`（可由组织策略提升至 `RESTRICTED`） |
| `external` | 公网第三方服务 | `INTERNAL`（提升需显式风险接受 artifact） |
| `unknown` | 未声明 | `PUBLIC`（最保守） |

这比「是否在清单里」有意义得多：企业自部署的内网 vLLM 应当**允许**发送 CONFIDENTIAL，而公网第三方
即便在清单里也未必可以。原设计恰好把这个关系搞反了。

pre-send gate 取 `context 中实际数据分类 ≤ endpoint classification_ceiling` 的交集，不满足即拒绝发送
——门禁仍然存在，只是设在了正确的位置。

#### 5. 模型热切换分两类

| 类型 | 场景 | 要求 |
| --- | --- | --- |
| **同 endpoint 换 model** | 同一 base_url 下切换（如 `qwen-plus → qwen-max`） | 新 ModelProfile + 新 Attempt；egress 策略不变；跨 epoch 时生成 `TransitionManifest` |
| **跨 endpoint 换 model** | 更换 base_url（如内网 vLLM → 公网 API） | 新 EndpointProfile + Connection；**必须重新执行 egress 检查**——若目标 `classification_ceiling` 低于当前上下文实际分类则**拒绝切换**；重新 attestation；必生成 `TransitionManifest` |

两类都遵守既有的 authoritative-or-refuse：目标 profile 装不下完整 authoritative inventory 时拒绝切换，
而不是静默截断。

**缓存代价必须显式呈现**（竞品调研的直接产物）：切换会使 prompt cache 完全失效。`TransitionManifest`
记录 `cache_invalidated: true` 与预估的重建成本，UI 在切换前展示；该成本进入 ADR-002 的
`weighted_tokens`（`cache_read` 权重 0.1 会如实反映缓存丢失）。不因为切换「技术上可行」就当它免费。

#### 6. 运行时注册必须留痕

经 env 或管理台引入的 endpoint 在首次使用时写 canonical event + audit：base_url、trust tier、
network zone、classification ceiling、声明人。**不允许静默换 endpoint**——这是与 llm-switcher 那类
「对上游透明」工具的关键区别。

### 后果

- 企业自部署 LLM 成为一等支持场景：配置 `OPENAI_BASE_URL` 即可接入，无需修改本仓库任何配置。
- 治理强度不降反升：门禁从「URL 匹配」变为「数据分类 × 网络区域」，后者才是真正要防的东西。
- 环境基线测试相应调整：不再断言 base_url 已登记（那会阻断合法的自部署场景），改为断言**档案库
  自身 schema 完整**；「未登记 → unverified 档」的行为断言属于 S3 的 RED。
- 影响 `config/providers/endpoints.yaml`、`.env.example`、`docs/MODELS.md`、`specs/s3-models-context.md`。

---

<a id="adr-012"></a>

## ADR-012：契约测试层级、Gate 例外与读路径授权 —— S0–S2 复审修订

**影响阶段**：S0–S2（回补修复轮）、S3+（前置纪律）　**状态**：accepted

### 问题

S0–S2 收口后的多路代码复审暴露的不是孤立 bug，而是四类**规格级**缺口：

1. 契约只写了「必须成立」，没写「必须在哪里被验证」——导致系统性模式「域层正确、接线失效」：
   机制存在于带单测的域模块中，生产路径上没有执行点，单测全绿但契约从未生效。
2. Gate 因环境无法执行（无 docker/Keycloak）时没有合法处置路径——阶段带病收口，纪律文本
   「Gate 全绿才能进入下一阶段」与实际流程脱节。
3. 读路径授权只规定了 mutation PEP——org 级读端点与 SCIM 的可见性边界留白。
4. spec 必需测试场景只存在于默认 deselect 的 slow 标记之后——「全绿」不含策略变更类安全场景。

### 反例清单（驱动本决策的最小反例，均已源码核实）

| # | 反例 | 位置 | 缺口类型 |
| --- | --- | --- | --- |
| 1 | 审批 SoD 三层防御（域层/PG store/DB CHECK）比较的 requester 恒为常量 `agent-runtime`，人类 principal 未从 StartRun 穿透；域层单测用真实字符串所以全绿 | `workflows/activities/runtime.py:271` | 测试层级 |
| 2 | `effect_unknown` 禁自动重试：`ActionReceiptManager`/`FailureTaxonomy` 零生产调用方，workflow `_interpret` 重试循环无门 | `workflows/agent_run.py:477` | 测试层级 |
| 3 | 委托三层界（环检测/深度上界/共用计数）均未接线，spec §6 要求的发布期环拒绝测试缺失 | `agents/versions.py:82`、`runtime/delegation.py` 零引用 | 测试层级 |
| 4 | 真实 OPA 下 workspace 创建恒 deny（`configure_workspace` 要求 workspace-scoped 角色，创建时无 workspace 上下文）；集成测试用 FakeOPA 恒 allow 掩盖 | `api/workspaces.py:179` + `policies/zhiwei/authz.rego` | Fake 边界 |
| 5 | 真实 Keycloak 登录 e2e 13/13 失败，S1 Gate 仍收口 | `artifacts/gates/s1-t6/report.md` | Gate 例外 |
| 6 | `refresh_session` 零生产调用方（域层 ~400 行 + 约 20 个安全测试为死代码，计数口径：直接调用 refresh_session 的 security 套件用例 18 个），会话 30min idle 即强制登出 | `identity/sessions.py:622` | 测试层级 |
| 7 | `replay-check` 注释声称「不同会话/事务」，实际两次载入在同一 session 同一事务快照内；交接单以【已验证】登记 | `cli/runtime.py:85` | 声明纪律 |
| 8 | 任意 org member 可 GET 全量 membership+角色绑定（无 PEP）；SCIM User 资源 identity-global，跨 org principal 可读（200/404 构成存在性 oracle） | `api/memberships.py:115`、`identity/scim.py:165` | 读路径授权 |
| 9 | `policy change during request`（S1 spec §5 必需场景）唯一实现位于 `-m slow`，默认 Gate 永不执行 | `tests/integration/policy/test_opa_sidecar_slow.py` | Gate deselect |

### 决策

#### 1. 契约测试层级（必要而非充分条件）

spec Required tests 中的**跨组件不变量**（经 API → workflow/policy → DB 生产路径才能成立的契约），
验收测试必须至少有一条位于 integration/contract 层并走**真实生产路径**。domain 层单测是必要而非充分
条件——「域正确」不构成「契约生效」的证据。RED 冻结关键契约时须同时指明层级。

S2 修复轮必须回补的接线级契约：SoD requester 穿透、effect_unknown 重试门、委托三层界、Synthesize
降级、handler registry 完整性预检、RunPaused/RunResumed 落账、replay-check 探针独立性、Redis
event_sink 生产组装接线。

#### 2. Gate 例外机制

「Gate 全绿才能进入下一阶段」补充唯一例外路径：环境阻塞的 Gate 项必须以**显式例外条目**记录于
阶段交接单——含阻塞项、根因、解锁条件、复执行时点四要素，并由 operator 确认。此时阶段状态为
「**有条件收口**」，不得表述为「收口/全绿」；例外项必须在后续依赖该能力的阶段 Gate 前复执行并
转绿。凡未走例外登记的 Gate 项缺失，一律按 Gate 未通过处理。

#### 3. Fake 件边界

FakeIdP/FakeOPA/Fake ticket 测试是必要而非充分条件：

- 授权矩阵每个 mutation happy path 至少一条**真实 OPA bundle** 集成测试；FakeOPA 恒 allow 不得
  作为矩阵语义的验证依据。
- OIDC 登录 happy path 至少一条**真实 Keycloak** 集成测试。
- Fake 与真实栈的语义分歧（时钟偏移容差、redirect_uri 校验、矩阵 cell 匹配）是缺陷信号，须登记
  差异清单并在真实栈测试转绿前保持例外条目。

#### 4. 读路径授权（PERMISSIONS §3.1 读 cell 的机读化）

| 读场景 | 允许角色 | 语义 |
| --- | --- | --- |
| membership/角色绑定列表 | org_owner、security_admin（workspace_admin 限本 workspace 成员） | Member 仅「读自身」 |
| workspace 列表 | org 内 active 成员 | 目录语义；workspace 级资源仍按 workspace membership |
| SCIM Users/Groups | org 作用域 | 仅返回与本 org 存在 Membership 关联的 principal；跨 org 一律 404（与不存在同文案） |
| runs/approvals/agents 读 | 走 PEP（read cell） | 不允许仅依赖 RLS+membership 判定可见性 |

#### 5. Gate 与 deselect

spec Required tests 场景不得只存在于默认 deselect 的 marker 之后；Gate 命令必须显式包含这些场景
（如显式 `-m slow` 或逐文件列出），或将其移入默认套件。Gate 输出须列出 deselect 清单并逐项说明
为何可 deselect。

### 后果

- S2 修复轮（代码回补）在 S3 开工前执行；本决策与 ADR-005/008 增补一起回写 `specs/s0`、`specs/s1`、
  `specs/s2`；AGENTS.md 同步 Gate 例外机制。
- e2e（Playwright 五角色 journey、runtime-approval）在真实 Keycloak 可用前以例外条目存在，
  不宣称全绿。
- 已收口阶段的「有条件收口」状态由修复轮消除：例外条目转绿或转为显式债务登记后才可宣称收口。

---

## 附：新增 spike（补入总设计 §16）

| 风险 | 验证 | 失败后的合法降级 |
| --- | --- | --- |
| wire capture 保真性（ADR-001，**P0**） | httpx transport 层在流式/重试/大 body 下能否稳定取得最终 body；四类篡改语料全捕获 | 改用自建反向代理捕获，manifest 通过 correlation id 关联；不降级为 SDK 层 hook |
| token estimator 校准（ADR-002，**P0**） | 对每个 endpoint/model 用回传 usage 回归本地估算，误差分布是否稳定收敛 | 该 profile 的 context fit 判定固定为最保守档，并在 profile 上标注 `calibrated_estimate` |
| SCIP 多语言索引（**P1**） | 目标语言各自的 SCIP indexer 能否在受控构建环境产出索引 | 降级 tree-sitter + 精确搜索；**必须同时声明 CodeRef 精度损失**（symbol 级降为 span 级） |
