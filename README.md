# 知微 ZhiWei

> 企业 Agent Core：把内部知识、用户/团队记忆、模型、工具和长期任务编排成可验证、可治理的
> Agent Apps。

## 当前状态

`release_mode: design_and_benchmark_assets`

项目已冻结产品设计、实施规格和评测资产；`src/` 尚未实现。因此当前仓库**不是可运行产品**，
也不存在模型效果、检索质量、风险发现能力、成本、延迟、吞吐或生产可用性数字。

当前仅以下事实有本地证据：

- `make evals`：冻结的 FactQA/Risk 资产通过 110 项 validator。
- 题集统计单位为 120 行、112 个 independence unit、57 个 template。
- `make determinism`：21 个发布资产两次干净重建逐字节一致。
- 四类统计单位故障注入可被 validator 捕获。

这些结果证明的是评测资产纪律，不证明 Agent Core 或 Ask/Discover 已经实现。

## 最终产品

知微是 Web-first、多用户、多工作空间的企业 Agent 应用平台：

- **Agent Core** 统一提供 Task Graph、durable Run、Canonical Context、跨模型投影、Knowledge
  Fabric、用户/团队/Case Memory、Capability Hub、Evidence、权限、评测和发布。
- **Ask** 是首个高级知识研究 Agent，跨文档、业务代码/GitHub、结构化数据与授权系统回答问题，
  将 Fact/Quote 绑定到可复算 Evidence，并明确区分 Inference/Recommendation。
- **Discover** 是持续风险发现 Agent，由 schedule、webhook 和 source delta 触发，从 Signal 形成带
  支持/反证的 RiskHypothesis，经人工处置进入 Case 和受审批动作。
- **ChangeBrief** 是第三个轻量 App，用 GitHub 触发、代码知识和同一 Evidence Contract 证明
  Agent Core 不依赖 Ask/Discover 专用分支。

```text
Workbench / Cases / Knowledge / Agent Studio / Capability Hub / Memory / Admin
                                  │
                                  ▼
Agent Core: Runtime + Context + Knowledge + Memory + Models + Tools + Policy + Evidence
                                  │
                   Ask / Discover / ChangeBrief / future Apps
```

## 不是普通 RAG + Text-to-SQL

1. **模型不是状态仓库**：权威任务状态由事件和 reducer 管理，按 ContextManifest 编译到三种 wire
   protocol；模型切换时完整迁移 authoritative inventory，装不下就拒绝发送。
2. **知识不是一锅向量库**：文档、表格、代码/GitHub、数据库和 API 保留源原生结构、ACL、时态与
   snapshot；Context Graph 只导航，Evidence 必须回到 Source Ledger。
3. **工具不是写死的函数**：MCP、OpenAPI、Agent Skills、SDK provider 和 Agent-as-tool 经过目录发现、
   准入、Connection/OAuth、版本绑定、策略、审批、隔离执行和撤销。
4. **记忆不是聊天摘要**：user/team/case memory 有来源、敏感度、candidate/confirm/conflict/revoke/
   delete 生命周期；后台 Discover 无权读取个人记忆。
5. **输出可被反驳**：事实 Claim 绑定 snapshot、typed canonical value、文本 span 与 verifier；动作使用
   独立 ActionReceipt，不能拿 citation 冒充执行成功。

## 产品形态

- 普通用户在 Workbench 使用 Agent Apps，在 Evidence/Tool/Approval/Context/Cost 面板检查过程。
- Builder 在 Studio 选择知识、记忆、模型、Tools、Skills、Task Graph、预算和评测后发布 Agent。
- Capability Publisher 在 Hub 从官方 MCP Registry、组织 Git、MCP URL、OpenAPI 或 SDK 导入能力，
  完成检查、准入、连接和版本更新。
- Organization/Workspace/Group/User/ServiceAccount 通过 OIDC/SCIM、RBAC + OPA + PostgreSQL RLS
  治理；API/SDK 可将已发布 Agent 嵌入其他系统。

## 计划中的本地完整部署

`local-product` 将使用 Docker Compose 启动 Web/API、PostgreSQL、Temporal dev、workers、OpenSearch、
Garage、Redis、Keycloak、OPA、OpenTelemetry 和 reference MCP/OpenAPI。它使用真实运行时、权限、
知识、记忆、工具和 Evidence，只把外部企业系统与 LLM 替换为 fixture/replay；启动不会自动调用
真实模型。本地禁止 GPU，默认检索模型为固定 revision 的 CPU BGE。

生产形态是 Kubernetes/reference adapters，不在未完成负载、故障、备份恢复与安全测试前宣称 HA/SLO。

## 快速验证现有资产

```bash
uv venv
uv sync --extra evals --extra dev
make evals
make determinism
```

以上命令不调用真实 LLM。live 需要显式启用某个已配置的 EndpointProfile connection，并通过 endpoint
allowlist、数据分类、能力 attestation、供应商侧余额关闭和 operator 显式动作；当前已配置的实例见
[docs/MODELS.md 附录 A](docs/MODELS.md)。历史 prereg 中的配置预算数字属于旧评测方案，不代表实际
花费或运行结果。

## 文档入口

- [冻结总设计](docs/superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md)
- [架构决策记录](docs/DECISIONS.md)
- [产品章程](docs/PRODUCT.md)
- [系统架构](docs/ARCHITECTURE.md)
- [数据模型](docs/DATA_MODEL.md)
- [API 契约](docs/API.md)
- [身份、权限与安全](docs/PERMISSIONS.md)
- [模型与 Canonical Context](docs/MODELS.md)
- [Knowledge/Eval 基准边界](docs/BENCHMARK.md)
- [实验与声明纪律](docs/EXPERIMENTS.md)
- [Discover/Risk 评测](docs/RISK_EVAL.md)
- [S0-S11 能力门](docs/ROADMAP.md)
- [对外叙事与声明注册表](docs/PORTFOLIO_NARRATIVE.md)
- [Codex/Claude Code 实施计划](docs/superpowers/plans/README.md)

## 开发状态规则

每项声明必须标为 `已验证 / 配置声明 / 计划实现 / 未验证`。阶段 Gate、sealed artifact 和 Claim
Registry 是升级声明的唯一依据；文档完整、目录存在、fixture 演示或代码行数都不构成能力证明。

## License

项目代码采用 [Apache License 2.0](LICENSE)。第三方模型、数据、Skill、MCP server、代码索引和依赖
保留各自许可证与服务条款，导入和发布均需经过 attribution/admission gate。
