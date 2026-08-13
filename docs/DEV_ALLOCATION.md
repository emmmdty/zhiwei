# 开发工具分工、并行与交接

> 本文规定 86 个 Task 由哪个工具承担、如何并行、如何交接与验收。
> 纪律见 [CLAUDE.md](../CLAUDE.md)；此处只解决「谁做、何时做、怎么验收」。
>
> **Opus 5 与 deepseek-v4-flash 在本文中一律指开发工具**（Claude Code / opencode 的驱动模型），
> 与 Agent 系统运行时使用的 model provider 无关——后者由 `docs/MODELS.md` 的 EndpointProfile 管理。

## 1. 分档判据

| 档 | 承担者 | 判据 | 占比 |
| --- | --- | --- | --- |
| **A** | Claude Code + Opus 5 | **错误不一定被测试捕获，或后果不可逆**：安全边界、并发/事务、密码学与 digest、核心不变量、契约冻结、统计方法 | ~45% |
| **B** | Opus 5 写 RED → opencode + deepseek-v4-flash 写 GREEN | 契约明确、行为可被测试完整覆盖：repository/CRUD、adapter、parser、CLI、UI | ~50% |
| **C** | opencode + deepseek-v4-flash 全包 | 机械转换 + 确定性验证：fixture 数据、样板导出、同一模式的第 N 个实现、文档同步 | ~5% |

判据的关键不是「难不难」，而是**测试红了能不能兜住**。RLS 策略写对了但连接池没清 tenant context，
测试可能全绿而隔离已破——这类归 A。序列化一个 tool_call 结构写错了，golden fixture 立刻红——这类归 B。

## 2. 逐阶段分配

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
| 6 ChangeBrief 实现 | B | **必须由 B 档完成**——若实现方需要改 Core 才能做完，说明通用性主张不成立，这是最有价值的检验 |
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
- **阶段内的 A 档基础 Task**：后续 Task 依赖其产出的表、契约与不变量。

### 3.2 两条线流水并行

真正的并行不在「同档任务之间」，而在**两个工具之间**：

```
Opus 5 主线   │ S(n) A 档实现 ──→ S(n) B 档 RED 批量产出 ──→ review ──→ S(n) Gate ──→ S(n+1)…
              │                          │                     ↑
              │                        交接单                 完成回收
              │                          ↓                     │
flash 副线    │            ┌──── B 档 GREEN（可开多会话并行）───┘
              │            └──── C 档（fixture / 样板 / 文档同步）
```

节奏：

1. Opus 进入阶段 S(n)，先完成该阶段全部 **A 档基础 Task**（表、契约、不变量）。
2. Opus 为该阶段全部 **B 档 Task 批量写 RED**，一次性产出交接单。
3. flash 并行认领 B 档 —— **文件白名单互不重叠的 Task 可同时开多个会话**。
4. flash 完成一个，Opus review 一个；同时 Opus 推进剩余 A 档。
5. 阶段 Gate 由 Opus 跑，不交给 flash。

### 3.3 可跨阶段提前做的 C 档

总设计允许在不依赖尚未冻结 schema 的前提下提前做：UI renderer 静态部分、评测语料、reference
MCP/OpenAPI server、样板 fixture。这些可以在任何时候交给 flash 副线，**但不得提前冻结依赖未实现
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

### 4.1 交接单（Opus → flash）

每个 B 档 Task 交接时产出一份自包含的交接单，flash **不需要理解整体架构**即可执行：

```markdown
## 交接单 S{n}-T{m}

**目标**：一句话说明要实现什么

**必读文件**（只读这些，不要通读仓库）
- specs/s{n}-*.md 的第 X 节
- 相关既有实现：src/zhiwei/.../{file}.py

**可修改文件白名单**（只能改这些，其他一律不许动）
- src/zhiwei/.../{a}.py   （新建）
- src/zhiwei/.../{b}.py   （修改）

**当前 RED**
$ uv run pytest tests/unit/xxx -q      # 预期：N failed，失败原因是 ModuleNotFoundError / 未实现

**完成判据 GREEN**
$ uv run pytest tests/unit/xxx -q      # 预期：N passed
$ uv run ruff check src tests && uv run pyright
$ make handoff-check                    # 预期：tests/ 未被改动

**禁止**
- 修改 tests/ 下任何文件（包括加 skip、改断言、改期望值）
- 修改白名单外的文件
- 引入新的第三方依赖（需要时停下来报告）
- 为了让测试通过而硬编码返回值

**遇到以下情况立刻停下并报告，不要自行决策**
- 测试断言看起来是错的
- 规格与既有代码矛盾
- 需要改白名单外的文件才能完成
```

### 4.2 验收（flash → Opus）

三层验收，前两层自动、第三层人工：

| 层 | 手段 | 拦截什么 |
| --- | --- | --- |
| 1 自动 | `make handoff-check` | 改测试、改白名单外文件 |
| 2 自动 | 该 Task 的 pytest + ruff + pyright | 功能未完成、类型错误 |
| 3 人工 | Opus review | **假实现**：硬编码返回值、只覆盖测试用到的分支、吞掉异常、TODO 占位 |

第三层不可省。小模型最典型的失败模式不是「写不出来」，而是**写出刚好骗过测试的实现**。
review 重点看：有没有 `if input == <测试里的值>`、有没有 `except: pass`、边界条件是否真的处理了。

### 4.3 冲突与回滚

- flash 在独立分支工作：`feat/s{n}-t{m}-<slug>`，Opus review 后合并。
- review 不通过时：**退回重做，不由 Opus 直接改**——否则分工失去意义，且掩盖了交接单写得不够清楚
  这一真正问题。第二次仍不通过则该 Task 升档为 A。
- 升档记录在本文件，作为后续判据的校准依据。

## 5. 成本与效果的取舍

- A 档不省。它占的是「错了要返工整个下游」的位置，返工成本远高于模型成本。
- B 档是成本节约的主战场：RED 由 Opus 写一次，GREEN 由 flash 做，且 GREEN 通常是 Task 中体量最大的部分。
- C 档几乎无风险，全部交给 flash。
- **live 模型调用永远不交给任何 AI 开发工具**，只由 operator 手动触发（S3-T7、S9 live suite）。
