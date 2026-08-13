# ZhiWei 设计与开发交接进度

## 2026-08-12：历史评测地基

- 冻结 FactQA/Risk 资产，验证 `make evals` 110 项、`make determinism` 21 个资产逐字节一致。
- 题集冻结为 120 行、112 independence unit、57 template；四类统计单位故障注入均被捕获。
- 第四轮 adversarial review 的统计、预算、capability attestation 等修复保留在 `findings.md`。
- 旧方案以 Evidence/FactQA/Canonical Context/RiskInsight 和校招演示为边界；该产品范围已被后续企业
  Agent Core 设计取代，旧评测事实仍然有效。

## 2026-08-12：企业 Agent Core 重新定位

- 用户明确否决“通过删风险项缩小产品”：保留新 MCP/Skills/Tools 来源与鉴权、Web 管理、多组织、
  用户/团队记忆、代码/GitHub 知识、后台 Discover 和真实动作闭环。
- 逐节确认九个设计部分：产品层次、Runtime/Context、Knowledge、Memory、Ask/Discover、Capability Hub、
  multi-org security、Eval/Release/Observability、工程/部署。
- 确认 Agent Core 是主产品；Ask/Discover 是两个深 App；ChangeBrief 是第三 App 通用性证明。
- 检索并记录 Temporal、LangGraph context/memory、MCP OAuth/security/tasks/registry、Agent Skills、GitHub App、
  Sourcegraph、OpenSearch、LongMemEval/LoCoMo、InsightBench、OIDC/SCIM/RLS、OTel、Inspect/Promptfoo、
  MinIO/Garage 等选型依据。

## 2026-08-12：冻结文档重构

- 重写 `docs/superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md` 为唯一架构事实源；
  旧 `verifiable-portable-agent-design.md` 标为历史基线。
- 统一重写 README、PRODUCT、ARCHITECTURE、DATA_MODEL、API、PERMISSIONS、ROADMAP、
  PORTFOLIO_NARRATIVE、MODELS、RISK_EVAL、THIRD_PARTY_DATA；更新 BENCHMARK、EXPERIMENTS、CONVENTIONS。
- 将阶段从旧 S0-S7 重构为 S0-S11：Foundation、Tenancy、Runtime、Models/Context、Capability Hub、
  Knowledge、Evidence/Ask、Memory、Discover/Actions、Eval/Release、Studio/Third App、Production Reference。
- 生成 12 份实现规格和 12 份 Codex/Claude Code 任务级计划；每份计划采用 checkbox、精确路径、
  RED/GREEN、Gate 和建议 commit boundary。
- 本轮未修改 `src/`、未调用真实 LLM、未读取 `.env`、未运行 GPU。

## 2026-08-13：机制级评审与决策补齐

- 架构评审识别九处机制缺口，共同特征是「写清了要求、没写清算法」：wire capture 层、context fit 计数、
  Evidence 快照缺失、Discover 证伪、并行合并、Evidence ACL 时态、refusal 恢复、委托环、memory 收敛。
- 对每处做竞品检索后形成 `docs/DECISIONS.md`（ADR-001 至 ADR-010），每条含竞品对比表、候选方案与选择理由。
  关键结论：wire capture 无竞品可循（proxy 派有 body 无语义、SDK 派有语义无 body、TEE 派解决的是服务端
  问题），故采用 httpx transport 层捕获 + SDK 禁用内部重试；Discover 证伪锚定 POPPER 的序贯证伪；
  并行合并采用 LangGraph「未声明策略即拒绝」并扩展 conflict-preserving；memory 保持 Zep 式时态共存
  并补 Mem0 式写入去重。
- token 预算重新定位：context fit 是硬约束（三级计数 + 实测校准），token 支出降为 ROI 指标体系
  （weighted_tokens / authoritative_token_share / evidence_per_kilotoken 等），spend guard 默认关闭。
- OpenCode Go 去特化为一个 EndpointProfile 实例；`docs/MODELS.md` §1-§7 恢复 provider-neutral，实例事实
  移入附录 A。
- 修复六处前后冲突：`.env` 通用 `OPENAI_*` 键与专用 Connection 策略矛盾、pyproject entry point 与
  description/keywords 旧定位、ARCHITECTURE 目录树 `api/` 重复、PORTFOLIO_NARRATIVE 无 artifact 支撑的
  行数数字、ROADMAP 北极星与 S0-S2 Gate 表述矛盾（现区分平台 journey 与 Agent journey）。
- 决策已回写 S2/S3/S4/S5/S6/S7/S8/S9 spec 与总设计 §0/§16；§16 新增三个 spike（wire capture 保真性、
  token estimator 校准、SCIP 多语言索引），前两个不依赖前置阶段。
- 回归全绿：110 项 validator、21 个资产 determinism 逐字节一致、Ruff 通过、Pyright 0 error、
  全仓库 Markdown 本地链接与 ADR 锚点无断链。本轮未实现 `src/`、未调用真实 LLM、未修改 evals 资产。

## 待办

- 项目所有者确认初始 Git baseline 后，直接从 S0 开发；不再停留在同层方案扩写。
- 本地 `.env` 仍持有旧的 `OPENAI_API_KEY/BASE_URL/MODEL`，需迁移为 `OPENCODE_GO_API_KEY` 并删除原键
  （该文件已被 `.gitignore` 覆盖，不会进入 baseline）。
- wire capture 与 token estimator 两个 P0 spike 可在 S0 之前独立执行。

## 2026-08-12：实现就绪收口

- 静态检查通过：12 个 spec/plan 一一对应，86 个 Task，Markdown 本地链接无断链，活动文档无旧单用户/
  无 RBAC/无 PostgreSQL 口径，Ruff/Pyright 全绿。
- 资产回归通过：`make evals` 110 项、`make determinism`、`sha256sum -c` 21/21。
- 独立审查修复：最小 Eval core 前置 S0、S2 绑定生产 Runtime、S9 Eval/Release 先于 S10 Studio；统一
  Python package/file 和模块名；冻结 Agent Builder/RBAC/SoD；S1 加密 AuthSession；S3-S7 正式 Runtime
  handlers；S4 独立 Capability Runner；Node 22/npm Gates；CLI command ownership；external unavailable artifact。
- 最终复核进一步修正首次 OIDC 无组织 session、Approval/Admission 聚合职责分离和 S9 artifact 路径。
- 本轮未实现 `src/`、未调用真实 LLM、未读取 `.env`、未运行 GPU。
