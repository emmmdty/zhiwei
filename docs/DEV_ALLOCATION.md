# 开发职责、风险分级与交接

> 本文规定各 Task 的风险档位、职责边界、并行方式、交接与验收。
> 纪律见 [AGENTS.md](../AGENTS.md)（对所有编码代理生效的唯一指令源；`CLAUDE.md` 只是导入它并追加
> Claude Code 专属条目）。此处只解决「由哪类职责做、何时做、怎么验收」。
>
> **档位不绑定具体模型或开发工具。** 默认优先用 GPT/Opus 承担设计与验收、DeepSeek 承担执行，
> 但可按额度和可用性替换；交接单必须按职责与产物写，不得把模型名称写成开工或 Gate 前置条件。
> 这里提到的开发工具与 Agent 系统运行时 model provider 无关；后者由 `docs/MODELS.md` 管理。

## 1. 分档判据

| 档 | 默认工作流 | 判据 |
| --- | --- | --- |
| **A** | 设计/验收方冻结设计、不变量和关键测试 → 执行方实现 → 独立验收 | **错误不一定被测试捕获，或后果不可逆**：安全边界、并发/事务、密码学与 digest、核心不变量、契约冻结、统计方法 |
| **B** | 执行方完成 RED → GREEN → 自动检查，设计/验收方在 Task 或阶段 Gate 复核 | 契约明确、行为可被测试完整覆盖：repository/CRUD、adapter、parser、CLI、UI |
| **C** | 执行方端到端完成，自动 Gate 兜底，阶段收口抽查 | 机械转换 + 确定性验证：fixture 数据、样板导出、同一模式的第 N 个实现、文档同步 |

判据的关键不是「难不难」，而是**测试红了能不能兜住**。RLS 策略写对了但连接池没清 tenant context，
测试可能全绿而隔离已破——这类归 A。序列化一个 tool_call 结构写错了，golden fixture 立刻红——这类归 B。

### 1.1 职责而非模型

| 职责 | 默认工具偏好 | 产物与边界 |
| --- | --- | --- |
| 设计/验收方 | GPT/Opus | 规格、计划、A 档不变量与关键测试、UI 视觉稿与验收、独立代码 review、阶段 Gate |
| 执行方 | DeepSeek | 大部分 RED 测试、GREEN 实现、修复、自动检查和前端落地；A 档也由执行方实现 |
| operator | 人工 | live、外部 OAuth、破坏性故障、发布等必须显式授权的动作 |

工具偏好不是强制绑定。同一个工具可在不同 Task 承担不同职责，但 A 档最终验收必须与关键路径实现
保持独立；无法做到独立验收时，该主张只能标为 `未验证`，不得降级 Gate 来换取通过。

## 2. 逐阶段风险分级

### S0 Foundation — 全项目地基，A 档密度最高

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 包与质量配置 | B | settings 的 secret-safe repr 由 RED 覆盖 |
| 2 canonical contracts | **A** | RFC 8785、float bits、Unicode NFC、digest —— 全项目依赖，错了下游全返工 |
| 3 PG schema/roles/tenant | **A** | app role 不得 owner/BYPASSRLS、默认 deny RLS 骨架 |
| 4 canonical event + outbox | **A** | sequence CAS、advisory lock、digest chain、同事务提交 |
| 5 artifact manifest 协议 | **A** | digest verify、orphan 回收窗口、原子性 |
| 6 最小 Eval core | B | 有 `make evals` 兜底 |

### S1 Tenancy & Policy

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 principal/org/membership | B | |
| 2 SecretBackend + OIDC BFF | **A** | AES-GCM、AAD 绑定、session 加密与轮换 |
| 3 RBAC + OPA | **A** | 授权判定，SoD |
| 4 FORCE RLS + audit | **A** | 租户隔离最后一道防线 |
| 5 SCIM + 生命周期 | B | |
| 6 role-aware Web shell | B | |

### S2 Agent Runtime

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 AgentDefinition/SolutionPack 版本 | B | |
| 2 Task Graph + pure reducer | **A** | ADR-005 三类 merge 策略、conflict-preserving |
| 3 Temporal durable shell | **A** | workflow determinism、replay |
| 4 command/signal/outbox 桥接 | **A** | 幂等、去重、确定性 workflow id |
| 5 approval + effect 语义 | **A** | ADR-008 委托界、执行前重授权、`effect_unknown` |
| 6 Eval executor 绑定 | B | |
| 7 delegation / SSE / UI | **拆**：delegation 界 **A**；SSE 与 UI B | |

### S3 Models & Context — 项目含金量最高的阶段

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 Model/Endpoint/Profile/Attestation schema | **A** | 契约冻结 |
| 2 三 transport | **拆**：第一个 transport **A**（定模式 + golden 结构）；后两个 **C**（照模式复制） | |
| 3 canonical context types + reducer | **A** | 四类上下文语义 |
| 4 budget/compression/Context IR | **A** | ADR-002 三级计数、ADR-007 压缩上界与 refusal 恢复 |
| 5 **bind actual wire body** | **A** | ADR-001，全项目最高价值机制。httpx transport 层捕获 + 四类篡改语料 |
| 6 transitions + Router | **A** | |
| 7 fixture 资格 / live probe | B（fixture）+ **operator 手动**（live） | live 不得由任何 AI 工具自动触发 |

### S4 Capability Hub

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 capability 生命周期 | B | |
| 2 catalog 发现与隔离区 | B | |
| 3 SecretBackend + Connections | **A** | per-org AAD、凭据不外泄 |
| 4 MCP client + OAuth | **A** | OAuth 2.1、Resource Indicator、禁 token passthrough |
| 5 OpenAPI / Skills / SDK | B | |
| 6 admission 检查 + 恶意语料 | **A** | 对抗性语料设计 |
| 7 Tool Gateway + 隔离 runner | **A** | 沙箱逃逸边界 |
| 8 Web journey | B | |

### S5 Knowledge Fabric

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 Source Ledger + sync | B | |
| 2 文档/表格 parser | B | stable locator 由 RED 锁定 |
| 3 code/GitHub 接入 | B | SCIP/tree-sitter 集成偏机械；降级触发条件由 A 定 |
| 4 PG + API/MCP 源 | **拆**：ADR-003 的 `reproducibility_level` 判定 **A**；连接器实现 B | |
| 5 OpenSearch + Context Graph | B | |
| 6 Knowledge Planner + ACL | **A** | ADR-006、fail closed |
| 7 Web journey | B | |
| 8 四个 Knowledge suite | **拆**：scorer **A**；语料生成 **C** | |

### S6 Evidence & Ask

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 canonical values + Evidence union | **A** | 编码规范，ADR-003 三级可复算 |
| 2 Claim binding + verifier | **A** | 核心机制 |
| 3 Case 聚合 | B | |
| 4 Ask SolutionPack 契约 | **A** | 契约 |
| 5 Ask planner + renderer 扩展点 | B | |
| 6 Ask Workbench + Evidence explorer | B | |
| 7 eval 整合 | B | |

### S7 Memory

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 MemoryRecord + 生命周期 | B | |
| 2 非对称写入策略 | **A** | 什么能自动确认是安全判断 |
| 3 时态冲突与纠正 | **A** | ADR-009 时态共存 |
| 4 scoped retrieval + Context 投影 | **A** | scope 泄露 |
| 5 revoke/delete cascade | **A** | 删除完整性 |
| 6 Memory Center | B | 含 ADR-009 去重与排序 |
| 7 memory 评测 | B | |

### S8 Discover & Actions

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 Program + 触发器 | B | |
| 2 Signal/Hypothesis/Resolution 分离 | **A** | ADR-004 NegativeProbe 序贯证伪 |
| 3 Numeric Detector Pack 迁移 | **C** | 既有逻辑搬迁，有 regression 兜底 |
| 4 change-driven + 受控探索 | **A** | AnalysisSpec 边界防止模型自由读库 |
| 5 triage/Case/action/memory 串联 | B | |
| 6 Discover/Case UI | B | |
| 7 分层评测 | B | |

### S9 Eval / Release / Observability

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 campaign + 执行模式 | B | |
| 2 scorer 隔离 + 统计 + sealing | **A** | paired bootstrap、McNemar、Holm 校正 |
| 3 external/blind/metamorphic adapter | B | |
| 4 release + Claim Registry | **A** | 声明治理是项目核心纪律 |
| 5 strict release checker + attestation | **A** | |
| 6 OTel + failures + Cost Ledger | **拆**：ADR-002 指标定义 **A**；采集实现 B | |
| 7 Web flows | B | |
| 8 严格 Gate 重跑全部声明 | **A** | 最终判定 |

### S10 Studio & Third App

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 前端架构冻结 + 通用 registry | **A** | 决定 ChangeBrief 能否不改 Core |
| 2 Studio draft 编辑 | B | |
| 3 S9 发布流集成 | B | |
| 4 Knowledge/Capability/Memory/Admin journeys | B | |
| 5 ChangeBrief 定义 | B | |
| 6 ChangeBrief 实现 | B | **必须保持 B 档工作流**——若执行方需要改 Core 才能做完，说明通用性主张不成立，这是最有价值的检验 |
| 7 architecture 证据封存 | **A** | |

### S11 Production Reference

| Task | 档 | 说明 |
| --- | --- | --- |
| 1 镜像 + local-product Compose | B | |
| 2 Kubernetes reference | B | |
| 3 版本与升级流程 | **A** | 迁移正确性 |
| 4 备份与隔离恢复验证 | **A** | 数据安全 |
| 5 确定性故障 runner | **A** | 故障注入设计 |
| 6 固定负载 runner | B | |
| 7 生产拓扑安全与 no-secret 扫描 | **A** | |
| 8 发布与演示证据封存 | **A** | |

## 3. 串行与并行

### 3.1 严格串行的部分

- **阶段之间**：`S0 → S1 → … → S11`，前一阶段 Gate 全绿才能进入下一阶段。这是能力门制的核心，
  不因为工具并行而放宽。
- **阶段内的 A 档基础 Task**：后续 Task 依赖其产出的表、契约与不变量；串行约束来自依赖，不来自
  使用哪一种开发工具。

### 3.2 两条线流水并行

并行围绕职责和文件所有权组织，不围绕模型名称组织：

```
设计/验收线 │ 阶段计划 ──→ A 档不变量/关键 RED ─────────→ review / 视觉验收 / Gate
            │                    │                                  ↑
            │                  交接单                              回收
            │                    ↓                                  │
执行线      │      A 档 GREEN ──┼── B/C 档 RED → GREEN ────────────┘
```

节奏：

1. 设计/验收方先明确阶段计划、Task 档位、依赖顺序和验收证据；A 档还要冻结不变量与关键测试，
   UI Task 还要冻结视觉稿或可判定的视觉准则。
2. 执行方默认使用 DeepSeek。A 档按冻结契约实现；B/C 档由执行方完成 RED → GREEN，RED 必须先
   单独提交，进入 GREEN 后测试锁定。
3. **文件白名单互不重叠且依赖已满足的 Task 可并行执行**，无需等待某个指定模型空闲。
4. Task review 可按风险逐个或批量进行；阶段 Gate 由独立的设计/验收方执行，默认优先使用 GPT/Opus，
   但工具名称不是 Gate 条件。
5. A 档或 UI journey 未通过独立验收时，不能仅凭执行方自测升级为 `已验证`。

### 3.3 可跨阶段提前做的 C 档

总设计允许在不依赖尚未冻结 schema 的前提下提前做：UI renderer 静态部分、评测语料、reference
MCP/OpenAPI server、样板 fixture。这些可以在任何时候交给执行线，**但不得提前冻结依赖未实现
schema 的契约**，也不得以此绕过前置 Gate。

### 3.4 同阶段内可并行的 B 档组（示例）

| 阶段 | 可并行组 | 前置 |
| --- | --- | --- |
| S1 | T5 SCIM ∥ T6 Web shell | T1-T4 完成 |
| S4 | T5 OpenAPI/Skills ∥ T8 Web journey | T1-T3 完成 |
| S5 | T2 文档 parser ∥ T3 code 接入 ∥ T5 索引 | T1 完成 |
| S6 | T3 Case ∥ T6 Workbench | T1-T2 完成 |
| S8 | T5 串联 ∥ T6 UI ∥ T7 评测 | T2-T4 完成 |

## 4. 交接协议

### 4.1 Task 交接单（设计/验收方 → 执行方）

A 档和 UI Task 必须有自包含交接单；B/C 档在计划、白名单和验收命令已经足够明确时可直接按计划执行。
交接单描述职责和产物，不指定必须使用哪个模型：

```markdown
## 交接单 S{n}-T{m}

**目标**：一句话说明要实现什么

**风险与职责**
- 档位：A / B / C
- 设计/验收方：负责哪些契约、视觉标准和 Gate
- 执行方：负责哪些 RED、GREEN 和自动检查

**必读文件**（只读这些，不要通读仓库）
- specs/s{n}-*.md 的第 X 节
- 相关既有实现：src/zhiwei/.../{file}.py

**可修改文件白名单**（只能改这些，其他一律不许动）
- src/zhiwei/.../{a}.py   （新建）
- src/zhiwei/.../{b}.py   （修改）
- tests/...               （B/C 的 RED 阶段可写；A 的冻结测试只读）

**RED 所有权与冻结点**
- A：列出设计/验收方已冻结的关键测试；执行方不得修改
- B/C：执行方先写 RED、确认失败原因正确并单独提交
- RED commit：<commit sha，进入 GREEN 前填写>

**完成判据 GREEN**
$ uv run pytest tests/unit/xxx -q
$ uv run ruff check src tests && uv run pyright
$ make handoff-check HANDOFF_BASE=<RED commit>

**UI 视觉验收**（非 UI Task 删除本节）
- 已批准参考稿或 journey：<path>
- 必须检查的 viewport、状态和交互：<list>
- 验收证据：截图、Playwright artifact 或人工 review 记录

**禁止**
- GREEN 阶段修改已锁定测试，包括加 skip/xfail、放宽断言或改期望值
- A 档执行方修改已冻结的关键测试
- 修改白名单外的文件
- 引入新的第三方依赖（需要时停下来报告）
- 为了让测试通过而硬编码返回值

**遇到以下情况立刻停下并报告，不要自行决策**
- 测试断言看起来是错的
- 规格与既有代码矛盾
- 需要改白名单外的文件才能完成
```

### 4.2 验收（执行方 → 设计/验收方）

验收分为自动证据和独立判断：

| 层 | 手段 | 拦截什么 |
| --- | --- | --- |
| 1 自动 | `make handoff-check HANDOFF_BASE=<RED commit>` | GREEN 阶段改锁定测试、冻结资产漂移 |
| 2 自动 | 该 Task 的 pytest + ruff + pyright | 功能未完成、类型错误 |
| 3 独立 review | 设计/验收方检查 diff、契约、视觉证据和 Gate artifact | **假实现**：硬编码返回值、只覆盖测试用到的分支、吞掉异常、TODO 占位，以及视觉偏离 |

第三层对 A 档、UI journey 和阶段 Gate 不可省；B/C 可在阶段收口批量 review，避免为每个机械 Task
消耗高成本模型额度。review 重点看：有没有 `if input == <测试里的值>`、`except: pass`、只处理测试
路径、TODO 占位，以及边界条件是否真的处理了。

### 4.3 冲突与回滚

- 执行方在独立分支工作：`feat/s{n}-t{m}-<slug>`，通过 review 后合并。
- review 不通过时优先退回执行方修复；若根因是契约不清，先由设计/验收方修订交接单或关键测试，
  再重新进入 RED。不要由验收方静默改实现来掩盖交接缺陷。
- 重复失败时提高验收强度或将 Task 升为 A 档，但仍不绑定具体模型；升级原因记录在本文或交接单，
  作为后续分级判据的校准依据。

## 5. 成本与效果的取舍

- 高成本模型额度优先用于规格、计划、A 档契约、视觉判断、独立 review 和阶段 Gate，不用于承担大部分
  常规实现。
- DeepSeek 默认承担执行主线：B/C 档 RED → GREEN，以及 A 档在冻结契约下的实现与修复。
- A 档不省的是**独立设计与验收强度**，不是指定模型亲自写实现。额度不足时可批量 review、缩小
  验收上下文或更换同职责工具，但不得删减安全不变量、关键测试或 Gate。
- 模型和工具只是可替换资源；任务状态以 commit、测试输出和 Gate artifact 为准，不以工具品牌为准。
- **live 模型调用永远不交给任何 AI 开发工具**，只由 operator 手动触发（S3-T7、S9 live suite）。
