# 知微 ZhiWei

> 企业 Agent Core：把内部知识、用户/团队记忆、模型、工具和长期任务编排成可验证、可治理的
> Agent Apps。

## 当前状态

项目按 S0–S11 阶段推进（见 [docs/ROADMAP.md](docs/ROADMAP.md)）：

- **已完成**：冻结总设计与 ADR-001~012、12 份阶段规格与任务计划、评测资产（`make evals` 1205 项
  validator、`make determinism` 32 个发布资产两次重建逐字节一致）。
- **S0–S2**（Foundation / Tenancy / Runtime）：阶段 Gate 收口，交接单见 `docs/handoffs/`。
- **S3–S8**（Models/Context、Capability Hub、Knowledge、Evidence/Ask、Memory、Discover/Actions）：
  核心实现已入库，通过全仓 Gate：ruff 0、pyright 0、pytest 全量通过（含 OPA + PostgreSQL
  集成层），CI 在每次 push/PR 复验。
- **S9**（Eval/Release/Observability）：收口 2026-09-06。**S10**（Studio/Third App）：本轮入库。
  **未开始**：S11 Production Reference。

纪律不变：不调用 live 模型；没有 Gate artifact 支撑的数字不写进任何文档。当前不存在模型效果、
检索质量、成本、延迟、吞吐或生产可用性声明——上述测试结果证明的是实现纪律与契约正确性，
不构成 Agent 效果证明。

## 公开声明（Claim Registry 绑定）

下表数字只以 `{{claim:ID}}` marker 出现，渲染值由 Claim Registry 中 artifact-verified 的
claim 从 sealed EvalRun 填充（`zhiwei release check` 扫描本块；无 artifact 支撑的数字会被
拦截）。口径（mode/model/version/date/corpus/environment）以各 claim 的 registry scope
为权威，本块不重复抄写。

<!-- claims:start -->
<!-- 口径：mode=offline · model=reference-fixture · environment=offline-fixture ·
     口径日期 2026-09-05。全部为离线确定性执行，不是 live 模型效果，也不是平台总证据。 -->

| 声明（语料内口径） | 绑定值（sealed artifact） |
| --- | --- |
| 抗污染事实问答语料内回归（corpus-internal，非平台总证据） | {{claim:factqa-v1.accuracy}} |
| 知识文档检索判分（corpus-internal） | {{claim:knowledge-doc-v1.retrieval}} |
| 代码与 GitHub 检索判分（corpus-internal） | {{claim:knowledge-code-github-v1.retrieval}} |
| 跨源检索判分（corpus-internal） | {{claim:knowledge-cross-source-v1.retrieval}} |
| 知识 ACL 与新鲜度判分（corpus-internal） | {{claim:knowledge-acl-freshness-v1.retrieval}} |
| 企业记忆生命周期判分（corpus-internal） | {{claim:enterprise-memory-v1.pass}} |
| 数值风险发现 planted-target recall（冻结合成经营数据内口径） | {{claim:numeric-risk-v1.recall-d0}} |
| Discover blind 快照判分（corpus-internal） | {{claim:discover-blind-v1.blind-pass}} |
| Agent Runtime 生产契约单位终态 | {{claim:runtime-contract-v1.contract-pass}} |
| Ask 行为契约单位终态 | {{claim:ask-v1.contract-pass}} |
<!-- claims:end -->

外部基准（LongMemEval 等）数据/许可未就绪，相应 claim 保持 planned，不在上表出现。历史
资产数字（120/112/57、Risk planted、`$43.0231552`）维持 docs 中的窄口径标注（语料内部
边界，不升级为平台总证据），见 [docs/BENCHMARK.md](docs/BENCHMARK.md) 与
[docs/RISK_EVAL.md](docs/RISK_EVAL.md)。生产 SLO 不存在：S11 未开始前不作任何可用性承诺。

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

## 运行测试

```bash
uv sync
docker compose -f deploy/compose/compose.test.yaml up -d --wait postgres opa
uv run pytest -q
```

授权与租户隔离的集成测试依赖 compose 栈中的 PostgreSQL（`127.0.0.1:55432`）与 OPA
（`127.0.0.1:8181`）；`live`/`slow` 标记默认 deselect，不会发起真实模型请求。

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
- [Agent 实施计划](docs/superpowers/plans/README.md)

## 开发状态规则

每项声明必须标为 `已验证 / 配置声明 / 计划实现 / 未验证`。阶段 Gate、sealed artifact 和 Claim
Registry 是升级声明的唯一依据；文档完整、目录存在、fixture 演示或代码行数都不构成能力证明。

## License

项目代码采用 [Apache License 2.0](LICENSE)。第三方模型、数据、Skill、MCP server、代码索引和依赖
保留各自许可证与服务条款，导入和发布均需经过 attribution/admission gate。
