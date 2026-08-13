# S10 - Agent Studio, Complete Product Shell and Third App

> Status: frozen implementation specification  
> Depends on: S9  
> Unlocks: S11

## 1. Goal

将 S1-S9 已有 API/页面收敛为完整 Web 产品，并用 ChangeBrief 第三个 Solution Pack 证明 Agent Core 可扩展。
不新增空管理页面，不用自由画布掩盖未定义执行语义。

## 2. Information architecture

```text
Workbench / Cases / Knowledge / Agent Studio / Capability Hub / Memory / Admin
```

通用 Run panels：Task Graph、Evidence、Tools、Approval、Artifacts、Context、Cost、Memory。App 通过
ViewManifest 注册 input/result renderer；Core UI 不写 Ask/Discover/ChangeBrief 名称条件。

## 3. Agent Studio

分区：Overview、Instructions、Knowledge、Memory、Tools、Task、Triggers、Model、Budget、Evidence、Evals、
Access、Release。每个编辑动作创建 draft revision；保存使用 ETag/CAS；版本 diff 展示 dependency/permission/
budget/schema 变化。

Task editor 只允许 Core primitives 和 typed ports，实时校验 DAG、capability、input/output、budget、completion
obligations。发布按钮调用正式 validate/eval/review/stage/publish commands，不直接改状态。

Studio 只调用 S9 已实现的 Eval/Release/Claim services；不得在前端或本阶段新增另一套发布状态机。

## 4. ChangeBrief Solution Pack

```text
solution-packs/change-brief/{pack.yaml,agent.yaml,task_graph.yaml,skills,schemas,views,evals}/
```

GitHub commit/PR trigger -> code/GitHub Knowledge -> impact analysis Skill -> Retrieve/Analyze/Verify/Synthesize ->
Verified Change Brief。输出 affected symbols/dependencies/tests、related PR/issues/reviews/checks、risks、unknowns
和 CodeRef/GitHubRef。

不得新增 Core handler、数据库列、API route 或 renderer conditional 专供 ChangeBrief；需要的新通用 primitive
必须先按设计变更流程证明 Ask/Discover 也可消费。

## 5. Complete journeys

- Admin：organization/members/policy/audit/cost health。
- Publisher：Capability import→admit→connect→test→publish/suspend。
- Builder：Knowledge/Memory/Tools/Model/Task/Eval→publish。
- Member：Ask/Discover/ChangeBrief→Evidence/Case/action。
- Approver/Auditor：approval digest/ActionReceipt/policy/evidence/run review。

每页覆盖 loading/empty/error/permission/stale/conflict/offline reconnect；无后端 action 的控件不得出现。

## 6. Required tests

- architecture/import test：Core 不导入 Solution Pack；App registry data-driven；删除 ChangeBrief 不影响 Core。
- pack conformance：同一 schema/installer/release/runtime；invalid pack fails closed。
- full role Playwright journeys、responsive/accessibility smoke、SSE reconnect、CAS conflict。
- screenshot/DOM 中 fixture/replay/live/status/limitations 明确且从 API 派生。
- no dead page/action：route→API contract coverage 清单。

## 7. Gate

```bash
uv run pytest tests/architecture tests/contract/solution_packs -q
uv run pytest tests/integration/change_brief -q
npm --prefix apps/web run test:e2e -- full-product.spec.ts change-brief.spec.ts
uv run zhiwei eval run --suite change-brief-v1 --mode offline --seal
```

Gate artifact 必须显示 ChangeBrief 的 code diff 只位于 Solution Pack/通用 renderer 注册，不靠人工声称通用。

## 8. Claim boundary

可声明三个 App 复用公共 Core 和固定 journey 完整；不能写“支持任意 App”或用 UI 页面数量代表生产完整。
