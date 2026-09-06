# S10 Studio / Third App 交接与验收记录

> 验收日期：2026-09-06　HEAD：`54fd3dd`　验收依据：specs/s10-studio-third-app.md §7 Gate
> + 三轮独立 subagent 验收（R1 分角色：测试工程师/产品工程师/安全架构师；R2 交叉检验：红队+对照；
> R3 增量复核）+ 设计方 E6 裁决与权威复验
> 执行方：执行 subagent 分波（T1∥T5 → T2∥T6 → T3 → T4+T4b → T4c∥T4d → 修复轮 A∥B → T7）

## 1. 交付范围（plan Task 1–7，checkbox 全勾，偏差见 §4）

| Task | 交付 | 关键提交 |
| --- | --- | --- |
| T1 前端架构冻结 | ViewManifest renderer registry（fail-closed unknown）、AppRunBinding（creatable 标志）、typed client（ETag/CAS 412/428）、SSE 客户端 + cursor resync、{app,routes,state,components,api} 骨架、全视图迁移零行为变化 | 584a1e1/e711703 |
| T2 Studio draft 编辑 | agents API PG 持久化 + draft revision ETag/CAS（原子条件 UPDATE）+ validate 端点（validate_studio_graph 冻结语义）+ 无生命周期旁路 + migration 0016；Studio 13 分区 + 受约束 Task editor | 25c142f/7ac7495 |
| T3 Studio 发布流 | release-readiness（诚实 unknown/missing）、版本 diff（5 kind）、immutable manifest、S9 命令-only 发布流（无第二状态机） | ce92d20/502a9d6 |
| T4 产品旅程 | capabilities/connections/knowledge/memory 路由挂载 + 四视图（Publisher/Builder/Steward 旅程）+ Admin + route→API 双向绑定清单 + full-product 五角色 spec | 40ec4cb/aef3966 |
| T4b Ask/Evidence/Case | cases API（0017）+ evidence 投影端点（canonical 复算，零虚构）+ ask input/result renderer + Case 视图 | c4d8db3/b3dd80a |
| T4c Discover | discover API（0018，5 表 + SoD CHECK）+ triage/gated-action/HumanResolution 旅程 + feed score 无概率语义 | 225cfd4/b1b6a3c |
| T4d 例外 spec | capability-hub（S4）+ memory-center（S7）e2e 3+3 旅程 | 5b78aee/42dddb7 |
| T5 ChangeBrief pack | pack_files.py 通用 loader/conformance（fail-closed 8 类故障注入）+ change-brief pack 全套声明 | fe68512/23f0e7b |
| T6 ChangeBrief 运行时 | pack runtime（planner/synthesis/impact_analysis，零 infra/provider import）+ change-brief-v1 suite（6 fixtures，生产路径密封 6/6）+ renderer 注册 | 4da66b9/c4d13ab |
| 修复轮 A/B | runs.template 持久化（0019）+ pack 模板运行发起（PackPlanSource 通用缝，ask-v1 同缝共证）+ ADR-015 agent_draft.read + 8 Run 面板（evidence/cost 实数据 + 4 诚实 pending）+ ChangeBrief 实渲染 + a11y/responsive smoke | ca376d9/bc515fd、501501f/c08f6ad |
| T7 收口 | Workbench 经 registry creatable 发起 ChangeBrief + change-brief.spec.ts + Gate 执行 + artifacts/gates/s10 + README/progress 口径修复 | 0c44091/920ac3c/82bbc3e + 54fd3dd |

## 2. 权威 Gate（2026-09-06，已验证）

- Gate 四命令全绿：arch+solution_packs **209**；integration/change_brief **6**；e2e
  full-product+change-brief **13**（两 spec 均实际执行）；`eval run --suite change-brief-v1
  --mode offline --seal` **6/6 sealed**（`sha256:3ef722bf…`）；verify --all-sealed 1/1（migrator DSN）。
- 全仓：pytest **3916 passed / 0 failed**（6 skipped / 24 deselected）；ruff 0 / pyright 0；
  make evals **1205 项 / 32 资产**；determinism ✓；build ✓；mock e2e 全套 **55 passed**。
- 通用性证据（§7）：S10 span 126 文件分类（pack 11 / renderers 10 / eval-asset 11 /
  registry-data 2 / generic 62 / tests 30）；删除 ChangeBrief 实证：Core imports/build/arch
  全部存活，仅 change-brief 自身测试红、run 解析 422 fail-closed；ask-v1 与 change-brief
  经同一通用 PackPlanSource 缝共证。明细：artifacts/gates/s10/report.md。

## 3. 验收轮与缺陷闭环

| 轮 | 形式 | 结果 |
| --- | --- | --- |
| R1 分角色 | 测试工程师（测试面质量/门禁/mock 保真）| ACCEPT-WITH-DEFECTS：change-brief.spec 缺失、Gate cmd4 环境、a11y 缺失、tenancy 环境退化 |
| | 产品工程师（旅程/IA/面板/声明边界）| **REJECT**：ChangeBrief 对用户不可见（template 投影缺失/发起路径缺失/渲染 stub）、5/8 面板缺、README 超额声明——最小收口路径被采纳为修复轮任务书 |
| | 安全架构师（genericity/租户/迁移/CAS）| ACCEPT-WITH-DEFECTS（全 Low/Info）：delete-ChangeBrief 实证 SECURE、租户隔离逐端点走查 SECURE、CAS 原子性 SECURE |
| R2 交叉 | 红队（bypass/变异/删除实证）| **RED-TEAM-CLEAN**：修复探针全部 SECURE；4 条非阻塞建议（zero-write 常驻测试、pack digest 口径、cost 面板过取、删除路径专用测试） |
| | 对照（spec/plan/例外台账逐节）| CONFORMANT-WITH-REGISTERED-GAPS：唯一阻塞包 = T7 清单 |
| 修复轮 | A（后端）∥ B（web）| 产品方 REJECT 五项全闭；ADR-015 入册 |
| T7 + E6 | gate 执行 + 设计方裁决 | E6（Workbench 模板字面量 vs 冻结扫描）经 registry creatable 数据面解决，冻结测试零修改 |
| R3 增量 | 假实现清查 + 数字复现 | ACCEPT-WITH-DEFECTS：Gate 数字全部复现吻合；2 个 LOW 台账缺口已收编进报告 §16 |

## 4. 例外与遗留（ADR-012，artifacts/gates/s10/report.md §11/§16）

**继承四例外（S4/S6/S7/S8 e2e）**：**已正式关闭**（2026-09-06）——解锁条件满足（R1/R3 独立核验）+ spec 复执行通过 + operator 会话指令确认（ADR-012 §2，记录见 s10 report §17）。
**新登记**：E2 tenancy e2e：**已关闭**（真实栈 13/13，runbook 修正补记 §3.1）；E3 discover-v1 注册未可执行（无 fixture 语料；解锁=资产冻结流程）；
E4 auditor agent_draft.read deny（冻结矩阵，解锁=设计决策）；E5 Tools/Artifacts/Context/Memory 面板
诚实 pending（解锁=通用后端投影）；E7 真实 evidence 投影暂不含 brief 载荷（renderer 契约面由 mock e2e
证明；解锁=通用投影扩展）；A2 pack bundle digest 口径；route-coverage 清单 4 路由范围；registry.test.ts
偏差（无 JS runner；架构测试+e2e 实质覆盖）。

**跟踪项**：Studio 7 分区诚实占位（后端命令未接通）；Case 生命周期转移 API 未交付（S6 范围余项）；
audit 事件列表端点缺失（S1 余项）；policy ResourceContext 结构性缺口（S9 交接已登记）。

## 5. S11 放行

S10 Gate 全绿、无未决例外（E2 已关闭、四继承例外已关闭），**放行进入 S11**。S11 开工注意：
「未开始」仅剩 S11；四例外已关闭（关闭记录见 report §17）；E3/E5/E7 的
解锁路径均为通用机制扩展（资产冻结/通用投影），不引入 App 条件分支。
