# 知微 ZhiWei — AI 编码代理须知

本文件对所有参与本仓库开发的编码代理生效（opencode、Claude Code 等）。与任何默认行为冲突时以本文为准。

## 项目与事实源

企业 Agent Core 平台。S0–S2 已过阶段 Gate 收口；S3–S8（Models/Context、Capability Hub、
Knowledge、Evidence/Ask、Memory、Discover/Actions）实现已入库并通过全仓 Gate；S9–S11
（Eval/Release、Studio、Production Reference）未开始。

事实源优先级（冲突时上位优先）：

1. `docs/superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md` — 冻结总设计（架构约束）
2. `docs/DECISIONS.md` — ADR-001~012（机制级算法与流程例外，总设计冻结后补齐）
3. `specs/s0-foundation.md` … `specs/s11-production-reference.md` — 各阶段实现规格
4. `docs/superpowers/plans/2026-08-12-s*.md` — 任务级执行计划，按 checkbox 逐条推进

发现规格无法实现时：**先提交最小反例、受影响 invariant、候选方案和迁移影响，再改规格**。
不允许在代码里静默创造第二套架构。

## 不可破坏的纪律

- **评测先行**：判分器与基准资产先于被评测的能力。评测走生产 Runtime，不写「评测专用」旁路。
- **声明纪律**：每项主张标 `已验证 / 配置声明 / 计划实现 / 未验证`。**没有 Gate artifact 支撑的数字
  不得写进任何文档、README、UI 或提交信息**——包括「行数」「覆盖率」这类看似无害的数字。
- **冻结资产只读**：`evals/` 下的语料、题集、风险数据、CHECKSUMS 不得修改。任何改动前后
  `make evals && make determinism` 必须全绿。
- **不调用 live 模型**：CI、测试、Compose 启动一律不发真实请求。live 只由 operator 显式触发。
- **不读 `.env`**：测试与工具链不得加载它。
- **fail closed**：未知 schema、未知能力、ACL 不确定、策略引擎不可用——一律拒绝，不取「常见默认」。

### 关于 model provider 凭据

Agent 运行时的 provider 统一为 OpenAI 兼容风格，默认 endpoint 使用标准键名
`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`（见 `.env.example`）。这三个变量是**部署期
override，优先级高于 `config/providers/endpoints.yaml`**——企业自部署的内部 LLM 直接配置即可接入。

`endpoints.yaml` 是**已审查档案库，不是允许清单**：未登记的 endpoint 不被阻断，而是落入
`unverified` 信任档（能用，但数据分类上限降至 PUBLIC、能力全部 unknown、不可支撑对外能力声明）。
真正的数据门禁是 `network_zone × classification_ceiling`，不是 URL 匹配。详见 ADR-011。

不得在代码里硬编码 endpoint origin，也不得放宽 `runtime_registration_floor`。

## 开发流程：RED → GREEN → REVIEW

每个 Task 严格按此推进，不允许先写实现再补测试：

```
1. RED     写失败测试，运行并确认它确实失败（且失败原因正确）
2. GREEN   写最小实现让测试通过
3. REVIEW  检查是否为「刚好骗过测试」的假实现；跑该阶段 Gate
4. COMMIT  按 plan 中的 Suggested commit 边界提交
```

**测试文件是契约，不是可调整的障碍。**

- B/C 档可由同一执行方完成 RED 和 GREEN；RED 测试必须先单独提交，进入 GREEN 后测试即锁定。
- A 档的关键契约与安全测试由设计/验收方先冻结；执行方可在 RED 阶段补充实现级测试，但不得修改
  已冻结测试。
- GREEN 阶段禁止放宽断言、加 `skip` / `xfail`、改期望值或注释用例。测试确实有错时，必须说明
  原契约为什么错误、回到 RED 阶段修订，并重新确认预期失败；A 档修订还需设计/验收方确认。
- GREEN 完成后运行 `make handoff-check HANDOFF_BASE=<RED commit>`，验证锁定测试与 `evals/` 未漂移。

## 职责与风险分工

工作按风险分三档，逐 Task 分级见 `docs/DEV_ALLOCATION.md`。档位约束工作流和验收强度，**不绑定
具体模型或开发工具**：

| 档 | 工作流 | 判据 |
| --- | --- | --- |
| A | 先冻结设计、不变量和关键测试，再由执行方实现，最后独立验收 | 错误不一定被测试捕获，或后果不可逆：安全边界、并发/事务、密码学与 digest、核心不变量、契约冻结、统计方法 |
| B | 执行方完成 RED → GREEN，设计/验收方在 Task 或阶段 Gate 复核 | 契约明确、行为可被测试完整覆盖 |
| C | 执行方端到端完成，自动 Gate 兜底，阶段收口抽查 | 机械转换 + 确定性验证 |

默认职责分配如下，但可按额度和可用性替换工具；交接单记录的是职责和产物，不把模型名称写成前置条件：

- **设计/验收方**：优先使用 GPT/Opus，负责规格与计划、A 档不变量和关键测试、UI 视觉设计与
  视觉验收、代码审查及阶段 Gate。
- **执行方**：优先使用 DeepSeek，负责大部分任务的 RED、GREEN、修复、自动检查和前端实现；A 档
  同样由执行方写实现，只是关键契约先冻结、最终验收独立进行。
- **operator**：只负责必须由人显式触发的 live、外部 OAuth、破坏性故障和发布动作。

**如果你是执行方**：

- 只读 Task 必需上下文，只改计划或交接单白名单内的文件。
- B/C 档可在 RED 阶段创建或修订本 Task 的测试；A 档已冻结的关键测试只读。
- 遇到规格歧义、GREEN 阶段需要改锁定测试、需要动白名单外的文件、或发现设计缺陷时——**停下来
  报告，不要自行决策**。B/C 档测试修订若只是正常 RED 设计，不视为异常。
- 不引入新的第三方依赖。需要时停下来报告。
- 不为了让测试通过而硬编码返回值——这会在 REVIEW 阶段被退回重做。
- UI 以已批准的视觉稿和 journey 为契约；执行方负责实现，不自行改变视觉方向。

## 命令

```bash
make evals            # 重建并校验冻结基准资产（110 项）
make determinism      # 两次干净重建，断言逐字节一致
make handoff-check HANDOFF_BASE=<RED commit>  # 校验 GREEN 阶段锁定测试与 evals/ 未漂移
uv run pytest -q
uv run ruff check .
uv run pyright
```

各阶段完整 Gate 见对应 `specs/s*.md` 的 Gate 小节。Gate 全绿才能进入下一阶段。环境阻塞的 Gate 项
按 ADR-012 例外机制处理：显式登记（阻塞项/根因/解锁条件/复执行时点）并经 operator 确认后，阶段为
「有条件收口」而非「收口」；未登记的 Gate 项缺失一律按未通过处理。spec 必需测试场景不得只存在于
默认 deselect 的 marker 之后（详见 ADR-012 §决策 5）。

## 代码约定

- Python 3.11+，uv 管理依赖；行宽 100（`pyproject.toml` 已固定 ruff 规则集，不依赖其默认值）。
- domain 层不导入 FastAPI/Temporal/SQLAlchemy/provider SDK；依赖方向见 `docs/ARCHITECTURE.md` §2。
- 前端固定 Node 22 + npm/package-lock，不经 uv 启动 TS 工具链。
- 注释写「为什么」，不写「是什么」。项目既有代码的注释密度和风格是基准。

## 提交

- 不把多个 Task 混进同一提交。执行方同时承担 RED/GREEN 时，先提交 RED，再按 plan 中的
  `Suggested commit` 提交 GREEN 实现。
- Conventional Commits，作用域取模块名：`feat(contracts):`、`fix(runtime):`。
- 不在提交信息里写未经 artifact 支撑的效果数字。
