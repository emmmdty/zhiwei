# S10 Gate Report（specs/s10 §6-8，plan Task 7 执行记录）

执行日期：2026-09-06　执行者：S10-T7 executor（autonomous）
执行性质：faithful execution（R2 交叉检验工作清单逐项执行）；本报告为逐命令 verbatim 记录。

> **E6 已解除（设计方裁决，2026-09-06）**：Workbench 不再硬编码 pack 模板 id——
> renderer 注册表 AppRunBinding 增设 creatable 标志（fail-closed 默认 false），
> ask-v1 / change-brief 标 true、discover-v1 保持 false（例外 E3）；通用层经
> state/appTemplates.ts 纯转发访问器消费（零字面量），features→renderers 直接
> import 禁令与 App 名称扫描保持冻结原样。架构 6/6 + 全量 pytest 3916 全绿复验。

## 0. 环境

```text
工作区起始: c08f6ad（clean tree, main）
证据产出 HEAD: 920ac3c（含本任务 RED/GREEN 两提交；本报告与 README/progress 修复随提交 c 入库）
Python:  3.11.15（uv）
Node:    v24.15.0（apps/web engines >=22 <23；build/e2e 正常完成）
PostgreSQL: 17.6 @127.0.0.1:55432（compose zhiwei-s0：postgres/keycloak/opa 全部 healthy）
pytest DSN: ZHIWEI_TEST_ADMIN_DSN=postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test
            ZHIWEI_TEST_APP_DSN=postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test
            ZHIWEI_TEST_IDENTITY_DSN=postgresql://zhiwei_identity@127.0.0.1:55432/zhiwei_test
CLI 环境（gate cmd 4d/4f，E1 runbook）:
            ZHIWEI_DATABASE_URL=postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test
            ZHIWEI_OBJECT_STORE_ROOT=/tmp/opencode/s10-object-store（本轮新建，先 rm -rf）
            ZHIWEI_PROFILE=test ZHIWEI_RELEASE_MODE=fixture_only
            （verify 复核按 s9 先例换 migrator DSN，见 §7）
未读取 .env；全程未发出任何 live 模型请求（release_mode=fixture_only，eval --mode offline）。
```

## 1. 执行顺序说明（权威证据口径）

沿用 s9 报告的同一教训：全量 `pytest -q` 经 tests/integration/foundation/test_database.py
的 drop/recreate fixture 重建 zhiwei_test（schema 回 head、数据清零）。本轮执行顺序为
**4a → 4b → 4c → 全量 pytest/ruff/pyright/make evals/make determinism/build/全量 mock e2e →
4d（迁移核对 + 密封）→ 4f（密封复核）**——密封在全量 pytest 之后产出，无清库作废问题，
§6 JSON 即权威证据。全量 pytest 后 Identity e2e 种子被清（tenancy e2e 影响见 §8/E2）。

## 2. Gate cmd 4a — 架构/pack 契约（E6 修复后复验：**209 passed**，见文末补记）

```text
$ uv run pytest tests/architecture tests/contract/solution_packs -q
1 failed, 208 passed in 3.52s

FAILED tests/architecture/test_app_boundaries.py::TestWebGenericLayersHaveNoAppConditionals::test_generic_layers_do_not_branch_on_app_names
E       AssertionError: assert ['apps/web/src/features/workbench/Workbench.tsx:\\bask\\b',
                               'apps/web/src/features/workbench/Workbench.tsx:\\bdiscover\\b',
                               'apps/web/src/features/workbench/Workbench.tsx:change[-_]?brief'] == []
```

三条 violation 全部来自 GREEN 提交 `920ac3c` 对 Workbench.tsx 的白名单内改动（pack 模板
id 字面量 + 注释）；该测试文件为 A 档冻结契约（文件头明示 GREEN 阶段不得修改），且任何
「运行时正确但躲过正则」的拼写（字符串拼接/charcode 等）属伪装字面量的假实现——按纪律
不做。solution_packs 契约（含 test_core_boundary.py 删除弹性、test_change_brief.py
conformance）与架构测试其余部分全绿（208 passed）。

## 3. Gate cmd 4b — ChangeBrief 生产路径集成

```text
$ uv run pytest tests/integration/change_brief -q
6 passed in 19.01s
```

六个冻结 fixture 全链路（Trigger→Run→TaskGraph→Evidence→brief artifact）+ honest unknowns
负例 + conformance/注册表 fail-closed 负例，全过。

## 4. Gate cmd 4c — 两个 spec 的 e2e（均已真实执行）

```text
$ npm --prefix apps/web run test:e2e -- full-product.spec.ts change-brief.spec.ts
13 passed (13.3s)
```

13 = full-product 8 + **change-brief 5**（本轮新落盘 spec：member 建单旅程 / VerifiedBrief
渲染 / 诚实 pending ×2 / auditor 只读）。RED 阶段记录（提交 `0c44091`，GREEN 之前）：

```text
$ npm --prefix apps/web run test:e2e -- change-brief.spec.ts        # RED（Workbench 无 change-brief 模板）
1 failed
  e2e/change-brief.spec.ts:406 › …member run origination › …
  Error: getByLabel('Template').getByRole('option', { name: 'change-brief' }) — element(s) not found
4 passed (12.2s)
```

（唯一失败即 R2 gap #4 本体：模板选择器缺 change-brief——失败原因正确；journey b/c/d 在
RED 即过，因其断言面（renderer 槽位/诚实 pending/evidence 渲染契约）已由 fix-B（`501501f`/
`c08f6ad`）锁定落地，本 spec 是其 member 视角旅程补全。）

## 5. Gate cmd 4d — schema + change-brief-v1 密封（权威证据）

```text
$ rm -rf /tmp/opencode/s10-object-store && mkdir -p /tmp/opencode/s10-object-store
$ export ZHIWEI_DATABASE_URL=postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test
  ZHIWEI_OBJECT_STORE_ROOT=/tmp/opencode/s10-object-store
  ZHIWEI_PROFILE=test ZHIWEI_RELEASE_MODE=fixture_only
$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
$ uv run alembic current
0019_run_template (head)

$ uv run zhiwei eval run --suite change-brief-v1 --mode offline --seal
time=2026-09-06T12:17:02.864 level=WARN msg="Cluster Id in Cluster Metadata config is not a valid uuid. Generating a new Cluster Id" component=metadata-initializer
{"suite": "change-brief-v1", "mode": "offline", "executor": "change-brief-pack", "production_path": "RunCommandService->AgentRunWorkflow->Retrieve->Analyze(impact-analysis skill)->VerifyHandler->Synthesize->EmitArtifact", "registered_units": 6, "terminal_units": 6, "status_counts": {"completed": 6}, "eval_run_id": "fec444fe-4bb9-4acd-a9b3-a2f55ee542a0", "organization_id": "f0ce8bae-cb5a-4a37-9e65-8c39fa342a53", "workspace_id": "72a3572e-ae23-4bfc-88e9-87db0af9428f", "sealed": true, "seal_digest": "sha256:3ef722bf81a2bf9aba92aff11543d9a60c5929095ad86c31ce14d64bbd081ef4"}
```

head = `0019_run_template` ✓（计划要求）。6/6 单位 completed、密封成功（stderr 的 Temporal
dev server WARN 与 ask-v1 密封同型，如实留档）。

## 6. Gate cmd 4e — 全仓回归

```text
$ uv run pytest -q            # 全量、单进程
1 failed, 3915 passed, 6 skipped, 24 deselected, 8 warnings in 296.23s (0:04:56)
（执行轮唯一 failed = §2 E6；设计方裁决修复后复验 **3916 passed / 0 failed**，见文末补记。）

$ uv run ruff check .
All checks passed!

$ uv run pyright
0 errors, 0 warnings, 0 informations

$ make evals
[gen] 模板题合计 84 题 → evals/questions/
[manual] 手工题合计 36 题 → evals/questions/manual/
[risk] 36 期 · 营收 588 行 · 应收 720 行 · 供应 432 行 · 现金流 144 行
[checksums] 32 个产物 → evals/CHECKSUMS.sha256
[validate] 1205 项校验全部通过

$ make determinism
[determinism] ✓ 两次干净重建产物逐字节一致
[checksums] 32 个产物 → evals/CHECKSUMS.sha256

$ npm --prefix apps/web run build
✓ built in 717ms（tsc -b + vite build，无类型错误）

$ npm --prefix apps/web run test:e2e -- accessibility.spec.ts architecture.spec.ts
  ask-evidence.spec.ts capability-hub.spec.ts change-brief.spec.ts
  discover-case-action.spec.ts eval-release-observability.spec.ts full-product.spec.ts
  memory-center.spec.ts runtime-approval.spec.ts studio-draft.spec.ts studio-release.spec.ts
55 passed (44.7s)
（全量 mock 模式 e2e，除 tenancy.spec.ts 外 12 个 spec 全部执行并全绿。）
```

`make evals` 口径：**1205 项校验 / 32 个冻结资产**（README/progress 修复即取此 verbatim
数字，见 §12）。

## 7. Gate cmd 4f — 密封复核（两个 DSN 如实留档）

```text
$ ZHIWEI_DATABASE_URL=…zhiwei_app…      uv run zhiwei eval verify --all-sealed
{"checked": 0, "verified": 0, "failures": []}
（exit 0——app DSN 在 FORCE RLS 下看不到租户行的 vacuous 成功，不计 Gate 结论，s9 §4 同款。）

$ ZHIWEI_DATABASE_URL=…zhiwei_migrator… uv run zhiwei eval verify --all-sealed
{"checked": 1, "verified": 1, "failures": []}
```

**Gate 口径：checked=1 / verified=1 / failures=[]（migrator/maintenance DSN）**。本轮全量
pytest 重建库后仅 change-brief-v1 一个密封件在场（checked=1 与 §5 权威密封一一对应）。

## 8. tenancy e2e（E2，re-run-at-gate）

```text
$ docker compose -f deploy/compose/compose.test.yaml --profile identity ps
zhiwei-s0-keycloak-1   Up 19 hours (healthy)   127.0.0.1:8080->8080
zhiwei-s0-opa-1        Up 16 minutes (healthy) 127.0.0.1:8181->8181
zhiwei-s0-postgres-1   Up 23 hours (healthy)   5432/tcp
```

栈在场且 healthy，但**未执行 tenancy.spec.ts**：全量 pytest 的清库 fixture 已把 Identity
e2e 种子一并清除（只读探针：`select count(*) from principals` → 2，
`fd1b9dab-…`（owner 种子）不存在），而工作清单明确「do NOT bring up or seed yourself」——
播种属 operator 保留动作。复执行时点 = Gate 复执行轮，程序 = `docs/handoffs/s1-tenancy-e2e-repair.md`
§3（compose up --wait → alembic → `deploy/seed_identity_e2e.py` → `deploy/serve_identity_e2e.py`
→ tenancy.spec.ts）。

## 9. 通用性证据（specs/s10 §7：diff 分类，不靠人工声称）

`git diff f8a10f0..HEAD --stat`（S10 全程，126 文件，+24735/−823）分类：

| 类别 | 文件数 | 明细 |
| --- | --- | --- |
| solution-packs/**（ChangeBrief pack 定义+runtime） | 11 | pack.yaml / agent.yaml / task_graph.yaml / schemas/verified-brief.yaml / skills/impact-analysis.yaml / views/{input,result}.yaml / evals/change-brief-v1.yaml / runtime/{impact_analysis,planner,synthesis}.py |
| apps/web/src/renderers/**（App UI 注册层） | 10 | registry.ts / index.ts / ask/{index,input,result} / discover/{index,input,result} / changeBrief/{input,result} |
| eval 资产层 | 11 | evals/change-brief/*.yaml ×6 + evals/CHECKSUMS.sha256 + evals/scripts/validate_corpus.py + src/zhiwei/evals/change_brief_suites.py + src/zhiwei/evals/executors/change_brief.py + src/zhiwei/evals/pack_templates.py |
| 注册表数据 | 2 | src/zhiwei/cli/evals.py / src/zhiwei/cli/assets.py |
| tests（pytest） | 17 | architecture ×2 / contract/api ×3（含 test_pack_template_runs.py）/ contract/solution_packs ×1 / integration ×7 / unit ×4 |
| tests（e2e） | 13 | apps/web/e2e/**（含 change-brief.spec.ts、route-coverage.ts） |
| 通用基础设施（其余全部） | 62 | migrations 0016-0019、api/{agents,cases,discover,evidence,releases,runs}、app.py、agents/{pack_files,task_graph}、persistence/*、discover/*、cases/*、web App/routes/state/components/lib/features、Makefile、docs、policies |

- **通用文件抽查（8 处，零 App 名字面量）**：src/zhiwei/api/cases.py、api/evidence.py、
  persistence/models.py、runtime/planner.py、app.py、apps/web/src/features/runs/RunDetailView.tsx、
  features/runs/EvidencePanel.tsx、lib/api.ts —— `rg -i "change[-_]?brief"` 全部 0 命中。
- **ask-v1 共同锻炼引用**：tests/contract/api/test_pack_template_runs.py 以同一 generic seam
  （POST /api/v1/runs → PackTemplatePlanSource → pack 队列 worker）同时真实执行 ask-v1 与
  change-brief 并断言 detail["template"]/["mode"]——Core 机制无任何 per-App 分支。
- **删除 ChangeBrief 弹性证明**（R2 CR-1 结果，本轮复验一致）：arch（TestCoreDoesNotKnowChangeBrief、
  test_core_boundary.py）+ cli 套件全绿（208 passed，见 §2）、`npm run build` 绿（§6）、
  pack runtime 零 Core 导入（TestPackRuntimeDiscipline）、run 解析对缺失绑定 fail closed
  （discover-v1 422 同路径，test_pack_template_runs.py::test_discover_v1_…_refused_at_creation）。
  注：本轮 4a 的唯一红项（E6）是 web 通用层字面量扫描，与删除弹性/导入方向断言无关。

## 10. Claim boundary（specs/s10 §8）

change-brief-v1 已密封（§5，seal_digest `sha256:3ef722bf…`）→ **available-for-registration**。
本轮**未创建任何 claim 行**：claim 登记走 S9 Claim Registry 流程（seed + bind_value），
明确不在 T7 范围（R2 工作清单指令）；可声明边界 = 三个 App（Ask/Discover/ChangeBrief）
复用公共 Core 与固定 journey 完整，不声称「支持任意 App」，不以 UI 页面数量代表生产完整。

## 11. 例外/阻塞台账（ADR-012 登记）

### 11.1 既有例外 S4/S6/S7/S8 —— 解锁条件已满足（待 operator 确认形式收口）

| 例外条目 | 解锁条件（原文） | 本轮状态 |
| --- | --- | --- |
| s4-capability-hub-e2e-exception | features/capabilities 实装 + spec 按 mock 先例落盘 | **已满足**：CapabilitiesView.tsx 实装（609 行）；capability-hub.spec.ts 在全量 e2e 55 passed 内 |
| s6-ask-evidence-e2e-exception | ask evidence/case 旅程 spec 落盘 | **已满足**：ask-evidence.spec.ts 全绿 |
| s7-memory-center-e2e-exception | memory-center 旅程 spec 落盘 | **已满足**：memory-center.spec.ts 全绿 |
| s8-discover-case-action-e2e-exception | discover triage/action 旅程 spec 落盘 | **已满足**：discover-case-action.spec.ts 全绿 |

**形式收口需 operator 确认（ADR-012 §2）**——四条目的四要素与解锁证据齐备，但「有条件
收口 → 收口」的状态翻转不在执行方权限内，本报告仅登记解锁条件已满足的事实。

### 11.2 S10 新例外（草案表）

| ID | 阻塞项 | 根因 | 解锁条件 | 复执行时点 |
| --- | --- | --- | --- | --- |
| E1 | gate cmd 4 需显式 env runbook | CLI 面不读 .env（纪律），环境须进程级显式注入 | 本报告 §0 已固化本轮实际使用的完整 env（含 pytest DSN 三件套 + CLI 四变量 + 全新 object store） | 已解除（本轮按此执行） |
| E2 | tenancy.spec.ts 未执行 | 栈在场但 Identity e2e 种子被全量 pytest 清库清除；播种为 operator 保留动作 | operator 按 s1-tenancy-e2e-repair.md §3 复播种（或授权执行方播种） | Gate 复执行轮 |
| E3 | discover-v1 注册但不可执行 | pack 声明在库、仓库内无可执行 fixture 绑定资产（pack_templates.py fixture_unit_id=None → 创建期 422）；Workbench 选择器不含它（死控件纪律） | 资产冻结流程补 discover fixture 绑定后仅需补一行绑定数据 | 资产冻结流程 |
| E4 | auditor agent_draft.read deny | 冻结权限矩阵如此（policy 层） | 设计方裁决 | 设计方裁决后 |
| E5 | Tools/Artifacts/Context/Memory run 面板诚实占位 | 后端投影未落地（PendingPanel 如实声明） | 通用后端投影落地 | 投影实现后 |
| **E6** | **Gate 4a 红：web 通用层 App 名字面量扫描**（本轮唯一红项） | **R2 工作清单第 1 项与冻结 A 档契约冲突**：Workbench.tsx TEMPLATES 的 pack 模板 id 字面量（ask-v1/change-brief）落在 `tests/architecture/test_app_boundaries.py` 的 WEB_GENERIC_LAYERS（含 features/）× WEB_BANNED_PATTERNS 扫描面内。反例：§2 三条 violation。任何「运行时正确但躲过正则」的写法属伪装假实现，不做；合法数据宿主（components 桥接导出绑定的可执行模板 id，或 apps/web/src/api/ 数据模块——后者不在 WEB_GENERIC_LAYERS 内，镜像后端 pack_templates.py「注册表是数据」豁免模式与 b982b45 给 cli/assets.py 补豁免的先例）都在本轮白名单之外 | **设计方（A 档）裁决模板 id 的合法数据宿主**并对冻结扫描作对应说明/修订后，执行方按重授权白名单落地；随后复跑 4a（3.5s）+ 4c + 全量 pytest | 设计方裁决后（Gate 复执行轮） |
| E7 | 真实路径 evidence 投影未携带 brief（本轮新发现，如实登记） | api/evidence.py RunEvidenceView 只投影 canonical 的 claims/answer/unknowns 词汇；ChangeBrief 的 brief 真相在 canonical["brief"]（reducer TaskCompleted 合并，集成测试直读），pack 不发 claims → 真实 GET /runs/{id}/evidence 的 claims=[]，web renderer（fix-B 契约：canonical_value 结构化 claim 载荷）在真实后端上恒为诚实 pending。mock 侧（architecture.spec (f) / change-brief.spec (b)）建立的是 renderer 契约面 | 通用投影补 brief 显影（如 generic canonical dict 透传或 pack 侧结构化 claim 发射——须先证明 Ask/Discover 亦可消费） | 投影机制设计后 |

## 12. README/progress 陈旧修复（R2 §7）

- README.md L10-11：`make evals` 110 项 → **1205 项**、`make determinism` 21 个 → **32 个**
  （数字取自本轮 `make evals` verbatim 输出，§6）；L16：`**未开始**：S9 Eval/Release、
  S10 Studio/Third App、S11 Production Reference` → `S9 收口 2026-09-06、S10 本轮入库、
  S11 未开始`（镜像 progress.md 口径）。claims 块与其余数字未触碰。
- progress.md 待办行（L225，非工作清单估算的 ~244）：同款修正（划线 + 2026-09-06 更新注记）。
- 注：S10「本轮入库」指实现入库；Gate 收口状态以本报告 §11 E6 为准（未收口）。

## 13. 提交

```text
0c44091 test(web): RED change-brief member journey (S10-T7)            — apps/web/e2e/change-brief.spec.ts
920ac3c feat(web): originate change-brief runs from workbench         — Workbench TEMPLATES（E6 阻塞点，见 §11）
<final>  (提交 c)                                                      — README/progress + artifacts/gates/s10/**
```

（artifacts/ 属 gitignore 范围，按 s9 先例 `git add -f` 纳入。工作清单建议的提交 c 标题
`test(platform): prove third app extensibility` 因 E6 未决不宜声称「证明完成」，改用如实
标题并在本节登记偏差。）

## 14. handoff-check

```text
$ make handoff-check HANDOFF_BASE=0c44091
[handoff] ✓ 锁定测试与 evals/ 相对 0c44091 未漂移
（README/progress/artifacts 漂移由提交 c 制裁，符合工作清单 VERIFY 条款。）
```


---

## 15. E6 裁决与权威复验（设计方/operator，2026-09-06）

- **裁决**：App 模板 id 字面量的唯一合法居所是 renderer 注册表（renderers/）——
  `AppRunBinding` 增设 `creatable` 标志（默认 false，fail closed），ask-v1/change-brief
  置 true；discover-v1 维持 false（E3 未解锁前不得出现永败控件）。通用 Workbench 经
  `state/appTemplates.ts`（纯转发、零字面量）消费 `listCreatableTemplates()`。
  features→renderers 直接 import 禁令与 App 名称扫描未放宽，冻结测试零修改。
- **权威复验**：`pytest tests/architecture tests/contract/solution_packs` → **209 passed**；
  全量 `pytest -q` → **3916 passed / 6 skipped / 24 deselected / 0 failed**；
  `npm run build` ✓；e2e change-brief 5 + architecture 7 + full-product 8 → **20 passed**
  （change-brief 走 registry 数据路径，journey (a) 创建→template/mode 投影→renderer
  解析全绿；明细 5+7+8。R3 复核注：seal 行复验受全量 pytest 清库顺序影响，以 §5/§7
  原始 artifact 为准，gate 复执行轮重跑 4d/4f。）
- **Gate 4 系列终值**：4a 209 passed；4b 6 passed；4c 13 passed（两 spec 均实际执行）；
  4d seal `sha256:3ef722bf81a2bf9aba92aff11543d9a60c5929095ad86c31ce14d64bbd081ef4`
  （6/6，§5 原记录仍有效）；4f verify 1/1（migrator DSN）。E6 关闭，S10 Gate 四命令
  全部可执行且绿。


## 16. R3 增量复核补记（2026-09-06，台账缺口收编）

- **registry.test.ts 偏差登记**（plan Task 1 列名文件，未创建）：仓库无 JS 单测
  runner（仅 Playwright），新增 runner = 新依赖（纪律禁止）。registry 语义由
  Python 架构测试 + e2e（unknown binding、SSE resync、creatable 路径）覆盖，
  实质覆盖成立；S11 如引入 runner 需先补此文件。
- **A2（pack conformance digest 口径）登记**：bundle 完整性锚 = pack.yaml 自身
  content_digest；task_graph/schemas/skills/runtime/*.py 未入 digest 钉，且
  solution-packs/ 不在 evals/CHECKSUMS.sha256 冻结清单内。当前篡改证据链 =
  git 历史 + 评审；解锁：扩展 CHECKSUMS 清单至 solution-packs 或 pack bundle
  全文件 digest（资产冻结流程）。
- **E7 维持登记**：真实后端 evidence 投影暂不含 brief 载荷（claims=[] for pack
  runs），renderer 契约面由 mock e2e 证明；解锁 = evidence 投影纳入 canonical
  brief 载荷（通用投影扩展）。


## 17. E2 关闭与四例外 operator 确认（2026-09-06）

- **E2（tenancy e2e 真实栈）关闭**：按 s1-tenancy-e2e-repair.md §3 程序（含本轮补记
  §3.1 的两项修正：identity 角色 DSN、NO_PROXY 代理旁路），HEAD 54fd3dd，
  `tenancy.spec.ts` **13/13 passed**（18.7s）。根因三件套：陈旧后端进程（17h 前旧代码
  占用 8000）、identity 数据面 DSN 误配（须 zhiwei_identity 角色）、shell 代理劫持
  localhost。模拟通道 55/55 与真实栈 13/13 共同构成 e2e 全量证据。
- **四继承例外（S4/S6/S7/S8 e2e）正式关闭**：解锁条件逐项核验满足（R1/R3 独立确认），
  operator 于 2026-09-06 会话指令授权按证据链判定（ADR-012 §2 确认项，会话记录为凭），
  四份 handoff 已加关闭节。至此 S10 无未决例外；E3/E4/E5/E7/A2 为已登记跟踪项
  （非 Gate 阻塞，解锁路径在案）。
