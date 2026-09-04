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

## 2026-08-13：开工准备与多代理协作基线

- 建立初始 Git baseline（107 文件）；`.env` 与凭据文件确认被 `.gitignore` 排除。
- 补齐四项开工前缺口：`/artifacts/` 忽略规则、`tests/` 骨架与环境基线测试、编码代理纪律文件、
  `make handoff-check` 交接门禁。门禁经三类故障注入验证（改测试、新增测试文件、改冻结资产均变红）。
- **凭据键名决定反转**：Agent 运行时统一使用 OpenAI 兼容 provider，因此默认 endpoint 采用生态标准键名
  `OPENAI_API_KEY/BASE_URL/MODEL`，不再要求 per-provider 专用键名。治理改由「值必须已登记」承担——
  `OPENAI_BASE_URL` 必须匹配 `config/providers/endpoints.yaml` 中已登记 endpoint 的 `base_url`，
  由 `tests/unit/test_environment.py` 即刻断言（经未登记 endpoint 失败注入验证）。ADR-010 已补第 0 条。
- 按官方规范建立双工具指令结构：`AGENTS.md` 为唯一事实源（opencode 优先读取），`CLAUDE.md` 用
  `@AGENTS.md` 导入并追加 Claude Code 专属条目。依据：Claude Code 官方文档明确其读 `CLAUDE.md`
  而非 `AGENTS.md`，推荐用导入方式共享；opencode 官方文档的读取优先级为项目 `AGENTS.md` →
  全局 `AGENTS.md` → `CLAUDE.md` fallback。
- 产出 `docs/DEV_ALLOCATION.md`：86 个 Task 按 A/B/C 三档逐个分配，含两工具流水并行模型、
  可并行组、交接单模板与三层验收协议。

## 2026-08-12：实现就绪收口

- 静态检查通过：12 个 spec/plan 一一对应，86 个 Task，Markdown 本地链接无断链，活动文档无旧单用户/
  无 RBAC/无 PostgreSQL 口径，Ruff/Pyright 全绿。
- 资产回归通过：`make evals` 110 项、`make determinism`、`sha256sum -c` 21/21。
- 独立审查修复：最小 Eval core 前置 S0、S2 绑定生产 Runtime、S9 Eval/Release 先于 S10 Studio；统一
  Python package/file 和模块名；冻结 Agent Builder/RBAC/SoD；S1 加密 AuthSession；S3-S7 正式 Runtime
  handlers；S4 独立 Capability Runner；Node 22/npm Gates；CLI command ownership；external unavailable artifact。
- 最终复核进一步修正首次 OIDC 无组织 session、Approval/Admission 聚合职责分离和 S9 artifact 路径。
- 本轮未实现 `src/`、未调用真实 LLM、未读取 `.env`、未运行 GPU。

## 2026-09-03：S2 修复轮（ADR-012 驱动的规格修订 + 三批次代码回补）

- **ADR-012 规格修订**（`fb6d973`）：S0–S2 五路并行代码审查暴露 9 条反例（Critical 1 / High 4
  / Medium 3 / Low 1），以 ADR-012 形式回写 specs/s0–s2 + AGENTS.md（含测试层级契约/Gate 例外/
  Fake 边界/读路径授权/Gate deselect 规则）；ADR-005 增补（append 保序消歧、单边拒绝、运行时 fail
  closed、ConflictDetected 落账）；ADR-008 增补（第三层静态证明可判定化：DAG 环检测 + 共享计数
  构造性论证）。
- **Batch A**（C-1 backfill / H-1 SoD 穿透 / H-2 workspace 策略死锁 / H-6 body 校验 + A-2
  读路径授权/幂等/digest 排序）：`2d0e104` GREEN，独立 subagent 验收通过。
- **Batch B**（H-3 审批原子性+expiry / H-4 Redis 接线 / H-5 探针独立事务 / H-7 委托界发布期
  + 命令层硬上界 / D-3 AttemptAborted / A2-1 digest 排序）：`71b2cd4` GREEN，独立 subagent
  验收通过（缺陷① decide 404 回归延至 Batch C）。
- **Batch C**（① decide 404 / ② expired 权威行回查 / ③ effect_unknown 门 / ④ Pause/Resume
  落账 / ⑤ registry 预检 / ⑥ digest 绑定节点 / ⑦ merge 单边拒绝 / ⑧ ConflictDetected
  canonical event + 冗余去重 / ⑨ Synthesize 降级 / ⑩ S0 JSONB 值域 + seal 复验 / ⑪ 架构
  测试重写）：`33eee3a` GREEN，全量 Gate 全绿（1214 passed / 0 failed / 0 errors）。
- **全量 Gate 状态**：pytest 1214 passed/20 deselected、ruff/pyright 0、evals 110、
  determinism 逐字节一致、replay-check 7/7、eval seal 7/7、handoff-check 干净（基线
  `aa88313`）。交接文档见 `docs/handoffs/s2-repair-round.md`。

## 2026-09-04：S3–S8 未提交批次的全量 Gate 对账与修复

- **背景**：工作区存在一批未提交实现（S3 Models/Context、S4 Capability Hub、S5 Knowledge、S6
  Evidence/Ask、S7 Memory、S8 Discover/Actions 的 src/tests/fixtures + 三份审计报告
  `docs/*AUDIT*.md`），未走 RED→GREEN→COMMIT 流程，`progress.md` 亦无记录。对账发现审计的
  "全绿" 口径是 `pyright src/` + 分子目录跑 pytest，从未跑过全仓 pyright、全量 pytest 与
  integration/security 层。
- **对账暴露并已修复**（详见本轮 git diff）：
  - pytest 全量收集炸出 19 个 ModuleNotFoundError：新增测试目录的 `__init__.py` 使
    models/capabilities/evidence/knowledge/memory/context 等同名顶层包在 prepend 导入模式下互相
    抢占 `sys.modules`。已在 `pyproject.toml` 固定 `--import-mode=importlib`。
  - S1 锁定契约 `tests/unit/identity/test_secret_contract.py` 的 SecretRef repr 断言被改为打码，
    以适配审计误判 L-4（SecretRef 是不透明引用句柄，repr=句柄值是安全契约本身）。已恢复锁定测试
    原文并还原 `SecretRef.__repr__`；`tests/security/capabilities/test_secrets.py` 安全契约随之回绿。
  - 新增 `tests/integration/context/test_wire_binding.py` 10 处使用废弃的
    `asyncio.get_event_loop().run_until_complete()`，单独跑侥幸通过、全量跑被 pytest-asyncio
    置位的 loop 状态炸出 RuntimeError；已全部改为 `asyncio.run()`。
  - `tests/unit/evidence/test_verifier.py` 过滤子串 `canonical_digest` 与实现 check_id
    `claim_*_canonical_value_integrity` 漂移导致恒失败（审计误标 "pre-existing"）；已对齐。
  - 全仓 pyright 338→0：测试工厂函数 `**overrides: object` 级联类型错误批量改为 `Any`，
    `uuid4` 误用作类型注解改为 `UUID`，openpyxl optional 成员加守卫，`ValidatedOperation`
    多传的 `operation_id` 移除，spike-01 脚本迁移至 httpx2 后复跑 `verdict: FEASIBLE`（exit 0）。
  - ruff 5→0。
- **当前 Gate 口径（全仓，2026-09-04 终态）**：pytest 2938 passed / 6 skipped / 20 deselected /
  0 failed / 0 errors；ruff 0；pyright 0；`make evals` 110 项全过；`make determinism` 逐字节一致；
  spike-01 复验通过。此前 16 项 OPA/PG live 集成红灯经恢复 Docker Desktop WSL 集成并拉起
  `deploy/compose/compose.test.yaml`（postgres + opa）后全部转绿，**未启用 ADR-012 例外**。
- **冻结资产核验**：`evals/` 既有资产未被修改（determinism/checksums 全绿）；新增
  `evals/knowledge/` 4 个语料文件尚未注册进 validator，属于 S5 的「评测先行」未完成债务。
- **流程债务**：该批次整包未提交且混跨 S3–S8 多个 Task，无法按 per-Task 边界补拆提交；
  8 个 spec 要求的安全测试目录与 5 个 E2E Playwright spec 仍缺（审计自认 P2 遗留）。
- **CI 上线**（GitHub Actions，push/PR 触发）：backend job 与本地 Gate 同口径（compose 测试栈
  postgres+opa → evals → determinism → ruff → pyright → 全量 pytest），frontend job 跑
  Node 22 npm build。过程中修复三处 CI 揭示的问题：dev extra 缺失（工具链在
  optional-dependencies）、runner 注入 FORCE_COLOR 切断 CLI 契约断言（conftest 进程级钉死
  rich Console）、authlib httpx_client 硬依赖 httpx 未声明（exact sync 后 OIDC import 链断裂，
  本地 venv 残留包掩盖）。远程仓库 `origin = github.com/emmmdty/zhiwei`，README/AGENTS.md
  已同步实现状态。

## 待办

- S3–S8 未提交批次：先由 operator 决定整批验收/提交边界（无法按原 per-Task 边界补拆），
  验收通过前不据此声称 S3–S8「已收口」。
- S3–S8 批次按模块边界分批入库（deps 迁移 → 既有模块加固 → S3–S8 各阶段 → 接线 →
  solution packs → 审计文档与 spec 修订）；整批验收以全量 Gate 为准，不声称逐 Task 收口。
- S9 Eval/Release、S10 Studio/Third App、S11 Production Reference 未开始。
- S2 修复轮登记的开放债务（详见 `docs/handoffs/s2-repair-round.md` §7）：SCIM group
  审计同事务、Child-run delegation 集成测试、SSE 心跳/游标下推、Web SSE 客户端等。
- S5「评测先行」债务：`evals/knowledge/` 语料未注册进 validator（110 项口径未覆盖）。
- 审计 P2 遗留：8 个 spec 要求的安全测试目录、5 个 E2E Playwright spec、hypothesis
  reducer property tests、S5 dense index 生产化选型（pgvector/FAISS）。
- token estimator spike（P0）仍未执行；wire capture spike 已于 2026-09-04 在 httpx2 下复验。
- 委托集成测试：S3 Delegate handler 实现后必须补 integration 级委托链 + 环检测端到端 + 两路径
  共用计数验证。
