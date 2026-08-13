# ZhiWei 研究发现

> 本文件保留历次研究与第四轮审查证据。当前产品范围与架构以
> `docs/superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md` 为准；下文出现的“删除
> 登录/RBAC”“本地 JSONL/SQLite”或“校招作品集 live”属于旧方案历史，不再指导开发。

## 已动手验证的仓库事实

- `make evals` 通过：84 道模板题、36 道手工题；风险数据 14 个 planted pattern、
  7 个 distractor；验证器报告 110 项检查。
- 题集随资产冻结 `template_id` / `independence_unit_id` / `unit_kind`：120 行 =
  112 unit（108 单轮 + 4 条 F5 chain）= 57 template，由 validator 逐条断言。
- 四类统计单位故障注入（缺 `template_id`、F1 误标 `targets_perturbed_field`、
  残留旧字段 `perturbed`、F5 chain 被拆成 single）均变红且恢复后全绿。
- `make determinism` 通过：21 个发布资产两次重建逐字节一致。
- 人为修改 `xiyouji.db` 后，`make validate` 同时捕获 SQLite/CSV 不一致、基础重建
  不一致、`source_sql` ground truth 不一致和 checksum 不一致；恢复后全绿。
- 抽查 5 道题重跑 `source_sql`，均与 JSONL ground truth 相同。
- 当前仓库没有任何 commit，所有项目文件均未跟踪；不得擅自创建一个只含新设计文档的
  异常根提交，也不得把用户全部现有文件一并提交。

## 独立审查（2026-08-12 第四轮）发现并已修复

- **执行单位与分析单位混写**：`prereg` 曾以 `partition_key=[config_id, independence_unit_id]`
  注册 1,680 个 cell，而真实分析单位只有 112 个（14×112=1,568）。该断言在数学上无法满足，
  campaign 的 exactly-once 校验会永远判不完整。现分开登记 1,680 执行 cell / 1,568 分析 cell，
  A6-2 按 `sample_id` 拆分且同 chain 三行不跨 child。
- **`perturbed` 命名陷阱**：`confirmatory_dataset_partition: perturbed` 与题行同名布尔字段
  冲突，机读会把确认性分母缩成 24，违反项目自己的 `n<30` 规则。字段已改名为
  `targets_perturbed_field` 并在 prereg 显式登记"不是分区"。
- **裸模型诊断悬空**：`baseline_bucket` 全为 null，注释指向并不存在的 `run_baseline.py`，
  且它被写成确认性总体的准入条件。现改为独立注册的 `naked-baseline` suite（有预算、有
  solver、有任务），只披露不改分母——按模型表现事后剔题本身就是 post-hoc selection。
- **handoff 预算少算约 3 倍**：`4.866048` 只等于一条边的量，且漏计 A-prefix 的 live 调用。
  现逐边登记（`3.5536896`/`1.4893056`/`4.2270720`），按边分片成 3 个 child run。
- **S3 gate 上限低于自身 worst case**：`$0.50` < `$0.68124672`，该门禁按设计无法启动。
  现注册 `handoff_smoke` 预算并把 cap 提到 `$0.70`。
- **probe 回写与 profile_digest 循环**：attestation 绑定 profile hash，若 probe 结果回写
  profile 则刚生成的 attestation 立即自我失效。现固定
  `effective_capability = static_profile ∪ latest attestation`，probe 永不回写。
  `unresolved_before_s1_live` 补齐 `structured_output` 等字段——B0 与 A6 全是 Qwen，
  漏登记会让 14 配置 campaign 无预警整体失败关闭。
- **CI 顺序**：`docker compose config --quiet` 写在 S0 基线里，而 `compose.yaml` 到 S6 才存在，
  CI 会从 S0 恒红。现按阶段分块。
- **`--solver fixture` 不存在**：S4 离线门禁引用了一个没有任何任务创建的 solver。
  改为 `--solver zhiwei --mode fixture`。
- 另修：无滚动窗口台账（几个 child 挤进同一个 `$12/5h` 窗口会被服务端截断）、
  Luna 代理价未标注低置信度、`allowed_paths` 归一化规则缺失、CLI 面漂移
  （`--solvers`、`eval resume` 子命令、未实现的 `zhiwei ask`）。

## 企业 Agent Core 实现就绪审查（2026-08-12）

- 修复阶段依赖倒置：最小 Eval core 在 S0、S2 绑定生产 Runtime、S9 扩展 Eval/Release，S10 Studio 只消费
  正式发布服务；早期 suite 不再引用尚未存在的 EvalRun。
- 修复 Python file/package 和 spec/plan 命名冲突；12 阶段一一对应。
- 冻结 Agent Builder 角色、resource/action/scope RBAC 矩阵和发布/审批/高风险准入职责分离；SoD 落到
  Approval/Admission 聚合与 CAS 测试，不只停留在 Rego 单测。
- S1 前置通用 SecretBackend 和加密 AuthSession。AuthSession 为 principal/session 级，首次登录不依赖
  Organization；S4 Connection secret 才使用 per-org AAD。
- S2 建 TaskHandlerRegistry；S3-S7 分别注册 model、InvokeTool、Retrieve、Verify、WriteMemoryCandidate
  正式 handler/Activity，Ask/Discover 不直接访问 DB/provider。
- Capability Runner 明确为独立服务：local 只运行预构建 reference runner，production 使用 per-invocation
  Kubernetes Job；API/worker 无 Docker socket/Kubernetes credential，缺 backend 失败关闭。
- 前端固定 Node 22 + npm/package-lock，所有 Playwright Gate 统一；每个 Gate CLI 有文件、注册和 smoke test。
- LongMemEval 许可/数据不可用时生成 sealed `unavailable` artifact，只允许 core Gate 通过，不解锁外部声明。

## 现有设计必须修正

- “19 个配置”实际只有 14 个唯一配置：1 个 B0 + 13 个变体；ROADMAP 又写成 7 次。
  后续统一为“6 个预注册维度、13 个变体 + B0”，不以配置数量做叙事。
- Wilson CI 适合单个比例，不适合判断同题配对消融差异。主比较使用 sample-level paired
  bootstrap 与 McNemar exact test；预注册多重比较使用 Holm 校正。
- 行哈希只证明重放结果完整性，不能证明 SQL 语义正确。Trace bundle 必须固定数据快照、
  schema、查询、结果规范化和 claim binding；语义正确性由独立 scorer 评价。
- “SQL 计算 ground truth，零人工标注”过度表述。可声称的是“答案值由已发布快照执行
  得出”；自然语言问题到 SQL 的语义映射仍由作者设计并需要审计。
- Risk 的 `snr` 当前主要是声明值，难度校验存在自证循环。应从生成后的真实序列计算
  realized SNR，再派生 difficulty；所有 pattern/distractor 类型都做通用自检。
- 风险 pattern 数量不足以支撑稳定 ECE；后续用多个冻结 seed 构成测试套件。
- 旧 README/API/MODELS 曾按小型作品集裁掉管理台、外部能力与多租户；企业 Agent Core 重构已废止该
  范围，新设计以真实治理闭环实现这些能力。
- `pyproject.toml` 原先引用不存在的 `LICENSE`；用户已选择 Apache-2.0，本轮已补标准许可证、
  元数据和第三方资产边界。

## OpenCode Go 资源与能力

- 官方限额：每 5 小时 `$12`、每周 `$30`、每月 `$60`，按美元用量计算；限额可能调整。
- 用户要求严格禁止套餐外费用。服务端控制台必须保持 `Use balance` 关闭；本地还要做
  run budget、usage ledger、endpoint allowlist 和临界中止。
- OpenCode 通用 Terms 禁止自动或程序化提取 data/Output。Go 文档公开 API endpoint 不能
  自行推出专项授权。用户在知悉风险后明确决定继续本地、小规模、非商业求职作品集 live
  评测；项目记录 risk acceptance，但不得对外声称 provider 已书面许可。
- 实际端点：`https://opencode.ai/zen/go/v1`；真实凭据 `/models` 曾返回 200。
- 官方 current-list 列出 18 个 Go 模型：Grok 4.5、GPT 5.6 Luna、GLM-5.1/5.2、Kimi K2.6/
  K2.7 Code/K3、MiMo-V2.5/V2.5-Pro、MiniMax M2.7/M3、Qwen3.6 Plus/3.7 Plus/
  3.7 Max/3.8 Max、DeepSeek V4 Flash/Pro、Hy3。
- 同一官方页面的 Endpoints/价格表又列出 `minimax-m2.5`，但 current-list 和请求量估算表
  均缺失它。严格 Go 策略按更窄的 current-list，MiniMax M2.5 只做 fixture/profile。
- `/models` 快照还有 7 个未列入 current-list 的 id：`glm-5`、`hy3-preview`、
  `kimi-k2.5`、`mimo-v2-omni`、`mimo-v2-pro`、`minimax-m2.5`、`qwen3.5-plus`；默认禁用，
  不能仅因发现就认为走套餐。
- Go 使用三种协议：多数模型走 Chat Completions；Luna 走 Responses；MiniMax/Qwen 走
  Anthropic Messages。
- 实测 `deepseek-v4-flash`：文本、`json_object`、auto tool call 成功；`json_schema`
  不可用；thinking 下强制 `tool_choice` 被拒；reasoning 字段为 `reasoning_content`。
- 实测 `mimo-v2.5`：文本、严格 `json_schema`、强制 tool call、图像输入成功；reasoning
  字段为 `reasoning`，图像被正确识别。

## 厂商适配边界

- Level A：OpenCode Go，只有产生带时间戳 probe artifact 的模型才提升
  `verification_level`。
- Level B：MiniMax、Kimi、GLM、DeepSeek、Qwen 官方 API 与阿里/火山计划端点，只有
  contract fixture；没有 Key 时保持 `fixture_tested`。
- 阿里 Coding Plan/个人 Token Plan 与火山 Coding Plan 的条款限制自动化脚本或自定义
  后端调用。适配配置不等于允许用于本项目批量评测，必须在文档中写清楚。

## 借鉴的开源方法

- Inspect AI：`Dataset -> Solver -> Scorer -> EvalLog`，eval-set 可恢复执行与独立日志。
- Promptfoo：精确请求缓存、失败退出码、恢复运行、成本跟踪；live 与 replay 必须分开。
- OpenCode/Kimi CLI：持久会话与 provider turn projection 分离；context window 预留输出
  headroom；压缩后丢弃 provider-native reasoning/tool wire message。
- OpenTelemetry GenAI：固定语义版本后用于 agent/tool/inference telemetry；正文默认不采。生产业务真相
  已改为 PostgreSQL/ObjectStore，JSONL 只作 sealed export/eval artifact。
- in-toto/OpenLineage：subject digest、run/job/dataset/version 的可验证 provenance 语义。
- LiteLLM：适合作为行业对照，不作为核心依赖，避免把兼容与计费实现外包掉。

## 方法论定位

- InsightBench 已覆盖植入式经营 insight；VarBench 已覆盖动态变量扰动；BIRD/Spider 已
  覆盖执行结果判分。知微的增量不是分别重新命名这些方法，而是把动态数据、确定性
  scorer、冻结快照、外部重放证据、上下文交接清单和成对消融串成一条审计链。
- 当前主叙事是“企业 Agent Core + Ask/Discover/ChangeBrief 产品闭环”；Evidence/Canonical Context/
  同构评测是核心机制，消融矩阵只是证明局部设计决策的手段。
- F5 已有 4 条三轮 chain，可作为 handoff benchmark 的种子，但样本量不足；应由 SQL
  生成更多指代/约束/证据连续性 chain。

## 参考来源

- OpenCode Go: https://opencode.ai/docs/go/
- Inspect AI: https://inspect.aisi.org.uk/
- OpenCode context design: https://github.com/anomalyco/opencode/blob/dev/CONTEXT.md
- Kimi CLI sessions/config: https://moonshotai.github.io/kimi-cli/en/guides/sessions.html
- InsightBench: https://arxiv.org/abs/2407.06423
- VarBench: https://arxiv.org/abs/2406.17681
- BIRD: https://bird-bench.github.io/
- OpenTelemetry GenAI: https://github.com/open-telemetry/semantic-conventions-genai
- in-toto Attestation: https://github.com/in-toto/attestation
- OpenLineage: https://github.com/OpenLineage/OpenLineage/blob/main/spec/OpenLineage.md
- LLM judge position bias: https://arxiv.org/abs/2406.07791
- Judge Reliability Harness: https://arxiv.org/abs/2603.05399
- 阿里 Token Plan: https://help.aliyun.com/en/model-studio/token-plan-personal-overview
