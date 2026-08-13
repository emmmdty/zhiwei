# spike-01：httpx transport 层 wire capture 保真性

对应 [ADR-001](../../docs/DECISIONS.md#adr-001) 与总设计 §16 spike 表第一行（P0）。

**要回答的问题**：在自定义 httpx transport 的 `handle_async_request` 里，能否稳定拿到发往
OpenAI 兼容 endpoint 的**最终序列化 request body**——非流式、流式、SDK 内部重试、大 body 四种
情况都成立？不成立就得按 §16 降级到方案 B（自建反向代理）。

**结论**：可行，`verdict: FEASIBLE`。不降级。但捕获实现有两条约束不写就会失效，见下文「关键发现」。

## 怎么跑

```bash
uv run python spikes/wire_capture/run_spike.py
```

退出码 0 = 全部断言通过；证据写入 `evidence/spike-01-wire-capture.json`。

不发真实网络请求：流量只走 `127.0.0.1` 上的 loopback mock endpoint（`mock_endpoint.py`）或
进程内的 `httpx.MockTransport`。不读 `.env`——`api_key` / `base_url` 全部显式传参，堵死 SDK 的
环境变量回退。

## 证据是怎么构造的

只看 `request.content` 只能证明「httpx 的对象里有这些字节」。要证明「socket 上流过的就是这些
字节」，必须由**接收端独立再 hash 一次**。所以每个场景都有两份 digest：

- 客户端侧：`CaptureTransport` 在 `handle_async_request` 里算的 `body_sha256`；
- 服务端侧：loopback endpoint 按 `Content-Length` 读满后自己算的 `body_sha256`。

断言是这两份相等（篡改场景则断言它们**不等**，用来标定失效条件）。

## 场景

| 场景 | 问题 | 结果 |
| --- | --- | --- |
| S1 非流式 | 捕获 digest == 服务端 digest？ | 相等；Content-Length 与实际长度一致；凭据头已脱敏 |
| S2 流式 `stream=True` | 捕获点与 digest 是否与非流式一致？响应还增量吗？ | 同一捕获点；body 仅多 `"stream":true`；4 个 delta 分批到达，capture 层未缓冲响应 |
| S3a `max_retries=2` + 429 | 一次逻辑调用 = 几次实际发送？ | SDK 调用层看到 1 次，transport 与服务端各看到 3 次；`x-stainless-retry-count` = 0/1/2 |
| S3b `max_retries=0` + 429 | 是否恰好一次发送？ | 恰好 1 次，`RateLimitError` 直接上抛 |
| S4 ~8 MiB body | 大 body 是否仍完整可捕？ | digest 相等；仍走 `Content-Length`，未切 chunked |
| S5 pre-send gate 拒绝 | 拒绝时请求真的没出去？ | 服务端接收 0 次；`captures` 为空 |
| S6 捕获点完整性 | digest 与 wire 在什么条件下分叉？ | 见「关键发现」 |
| S7 MockTransport | 同一捕获层能否离线跑？ | 能——S3 的 tamper corpus 不需要 socket |

## 关键发现

### 1. wire 的真相是 `request.stream`，不是 `request.content`

httpx 的 `_transports/default.py` 把 `content=request.stream` 交给 httpcore。`request._content`
只是一份缓存，两者可以分叉：

- **S6-T1**：捕获后改写 `request._content`，服务端收到的仍是原始 body。
- 推论：任何读 `request.content` 计算 digest 的实现，都可能 hash 到一份从未发送的 body。

正确做法是 `body = await request.aread()` 之后**把 stream 重新钉死**在这份 bytes 上
（`capture.py` 的 `PinnedBody`），让「我们 hash 的」与「inner transport 能读到的」是同一个对象。
`PinnedBody` 只继承 httpx 的公开 ABC，不碰 `httpx._content` 私有符号——transport 是长期依赖点。

### 2. 「捕到的就是发出去的」还依赖一条架构不变量

- **S6-T2/T3**：在捕获层**下方**再插一层并替换 stream，capture digest 与服务端 digest 分叉，
  且因为是等长篡改，`Content-Length` 交叉校验也拦不住。pin 挡不住这种情况——pin 解决的是
  `_content`/`stream` 分叉与不可重放 stream，不是下游替换。

这里的「等长」是刻意的：换成不等长的 body，客户端 h11 会先抛
`LocalProtocolError: Too little data for declared Content-Length`，请求根本发不出去（实测），
测不到 digest 那一层。S3 的 tamper corpus 必须包含等长篡改，否则等于没测。

所以 S3 必须有 architecture test 断言：**CaptureTransport 之下只能是真实 HTTP transport**，
且 Core 任何模块不得自建 `AsyncClient`。规格里「该 transport 是唯一出网路径」要收紧成
「capture 必须是最内层 wrapper」。

### 3. `max_retries=0` 不是保守偏好，是结构必需

`openai/_base_client.py` 的重试循环里是
`request = self._build_request(options, retries_taken=retries_taken)`——**每次重试重新构造并重新
序列化 request**，并注入不同的 `x-stainless-retry-count`。S3a 实测：一次 `create()` 对应 3 次
wire 发送。挂在 SDK 调用层的 hook 只能看到 1 次，那 2 次没有 manifest 的发送就是 ADR-001 说的
「看起来严谨、实则无效的证据」。

本次实测中三次重试的 **body 逐字节相同**（只有 header 变），所以失真表现为「3 次发送 1 个
manifest」而不是「manifest 绑错 body」。但这依赖 SDK 当前行为，不能当作保证——`max_retries=0`
把它变成结构不变量。

### 4. 逻辑请求与 wire body 不可互推

S1 里调用方传入的 key 顺序是 `model, messages, temperature`，wire 上是
`messages, model, temperature`。即使字段集合完全相同，调用层重新序列化一次也得不到同一串
bytes。这一条直接否掉「在调用层算 digest」的所有变体。

### 5. 大 body 不改变捕获路径

httpx 对 `json=` 一律先编码成 bytes 再包 ByteStream，不因体积切到 chunked。~8 MiB 与几十字节
走完全相同的一条路径。代价是**整份 body 会驻留内存**——S3 要按 profile 设一个 body 上限，
超限直接 refusal，不是发出去再说。

## 未覆盖（明确标注为「未验证」）

- 只跑了 `openai==2.54.0` 的 `chat.completions`，`httpx==0.28.1`。**Responses 协议与
  anthropic SDK 未跑**——两者同基于 httpx，但捕获点行为需各自验证后才能声明。
- **同步路径未实测**。httpx 的 `Request.read()` 与 `aread()` 源码结构对称，但这是阅读推断，
  不是实测结果。
- **multipart / files 请求未覆盖**。那类 body 是 `MultipartStream`，`aread()` 会把整份文件读进
  内存。模型请求路径目前不含此类，但 S4 引入文件上传型能力时必须重新评估。
- loopback 是明文 HTTP。digest 绑定的是 **TLS 之前的 plaintext body**——这正是要证明的语义
  （「本进程实际发出了什么」），但不能被读成「证明了 TLS 之后的字节」。

## 文件

- `capture.py`：`CaptureTransport` 原型 + `PinnedBody`。这是 S3-T5 `models/presend.py` 的骨架来源。
- `mock_endpoint.py`：loopback OpenAI 兼容 endpoint，负责产出第二份独立 digest。
- `run_spike.py`：场景与断言，写出证据文件。
- `evidence/spike-01-wire-capture.json`：证据。digest 类字段跨运行稳定；`delta_arrival_offsets_s`
  与 `elapsed_s` 依赖机器，对应断言标了 `timing_dependent`。

本目录不进 `src/`，也不进 `tests/`：它是一次性的可行性验证，不是产品代码，也不参与任何 Gate。
