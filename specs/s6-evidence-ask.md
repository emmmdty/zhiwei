# S6 - Evidence Contract and Ask App

> Status: frozen implementation specification  
> Depends on: S5  
> Unlocks: S7

## 1. Goal

交付扩展后的 Evidence Contract、deterministic verifier 与首个完整 Ask Solution Pack。Ask 必须跨文档、
代码/GitHub 和结构化源完成研究任务，并在 Workbench 中展示 Claim/Evidence、Artifacts、Run 与验证结果。

## 2. Required modules

```text
src/zhiwei/evidence/{canonical_values,refs,claims,bundles,verifier,errors}.py
src/zhiwei/cases/{domain,commands,repositories}.py
src/zhiwei/runtime/handlers/verify.py
solution-packs/ask/{pack.yaml,agent.yaml,task_graph.yaml,skills,schemas,views,evals}/
apps/web/src/features/{ask,evidence,cases}/
tests/{unit/evidence,contract/evidence,integration/ask,e2e/ask}/
```

## 3. Evidence contract

实现 QueryReplay/CellRef/DocRef/CodeRef/GitHubRef/ApiRef/AgentRef/PatternRef tagged union。Fact/Quote Claim 绑定
answer digest、code-point span/digest、claim type、canonical value 和 EvidenceRefs。Inference/Recommendation
只绑定 supporting/contradicting inputs，不标 deterministic verified。

**可复算等级**（[ADR-003](../docs/DECISIONS.md#adr-003)）：每条 EvidenceRef 携带 `reproducibility_level`：

| level | 复算方式 | 可支撑的 claim |
| --- | --- | --- |
| `replayable` | 在原 snapshot 上重执行得逐字节相同结果 | Fact / Quote |
| `copy_frozen` | 原查询不可重放，但结果集副本已冻结并 digest | Fact / Quote |
| `reference_only` | 仅有定位符，内容未冻结 | **仅** Inference / Recommendation |

`copy_frozen` 绑定 `{sql, typed_params, schema_snapshot_digest, executed_at, result_copy_digest, row_count}`，
副本经既有 canonical value 编码规范化后走 `temporary upload → digest verify → immutable key → PG manifest`
协议写入。一个 Answer 中混用不同 level 时，Claim/Evidence Map 必须逐条显示 level，不得整体呈现为
「已验证」。

**ACL 时态语义**（[ADR-006](../docs/DECISIONS.md#adr-006)）：可复算性与可见性解耦——Evidence 永远可被
系统复算（审计/eval 依赖），但对用户的可见性按**当前** ACL 重新校验并 fail closed；冻结时的 ACL
snapshot 只用于解释「当时为何可见」。失权时 Run 视图渲染 `evidence_access_revoked` 占位，不静默移除。

`zhiwei verify evidence` 对 bundle/schema/version/source/snapshot/locator/query/result/value/claim span/digest 分层
验证，稳定退出码：0 success；2 input/schema；3 source/snapshot；4 replay/value；5 claim/span；6 digest/
artifact；7 authorization/private boundary。hash 不证明题意或发布者身份。

## 4. Ask contract

AskTaskSpec：question、desired artifact、source/entity/time scope、risk、completion obligations。Planner 只使用
Core primitives，Finding 必须携带 evidence/status。Answer schema：sections、claims、conflicts、unknowns、
artifacts、execution summary、verification、next actions。

- Fact/Quote 无有效 Evidence 不能 final。
- 冲突并列且解释 source/version/time；不足则 clarify/partial/abstain。
- 一个 reference task 必须同时使用 code/GitHub、document 和 DB/API Evidence。
- 用户可把 Answer/selected Evidence 创建或附加到 Case；不复制 transcript。

### 4.1 Case lifecycle

Case 遵循以下状态机：

```text
created → active → triaged → resolved → archived
```

| 状态 | 触发 | 说明 |
| --- | --- | --- |
| `created` | 用户创建 Case | 初始状态，可附加 Evidence/Answer |
| `active` | 有 Agent 正在处理 | Case 被分配给 Ask/Discover 任务 |
| `triaged` | 人工分类完成 | severity/category/owner 确定 |
| `resolved` | Resolution 已应用 | 修复/缓解/确认误报均已记录 |
| `archived` | 用户或策略归档 | 不可逆，只读访问 |

**跨 App 可见性**：Ask App 只能访问 `created`/`active`/`triaged` 状态的 Case；
Discover App 可访问全部状态（含 `resolved`/`archived` 用于模式学习）。
状态转换必须写入 Case 的 canonical event log，不允许静默跳变。

## 5. Workbench

三栏 UI：App/Case navigation；主 Ask 交互与 structured artifact；Run/Evidence/Tool/Context/Cost/Memory panels。
点击 Claim 精确打开 source locator、canonical value、stale/classification 和 verify result。Fixture/replay/live
醒目标注；刷新后从 Run projection 恢复。

## 6. Evaluation

- 迁移旧 factqa-v1 为 Evidence/SQL regression，不改变冻结资产。
- 新 ask-v1：cross-source task、clarification、conflict、unanswerable、Fact vs Inference、partial/abstain。
- blind holdout 与 author-visible 分开；确定性 claim/evidence scorer + 校准的人评 inference/utility。
- tamper：snapshot/query/result/locator/source version/answer/span/wire/artifact 任一层。
- reproducibility level：`copy_frozen` 副本被篡改时 verify 失败；`reference_only` 返回「不可复算」而非
  验证失败（区分「验证不通过」与「本就不承诺可验证」）；断言 `reference_only` 无法支撑 Fact 类 claim。
- ACL 时态：撤权后同一 Run 的 Evidence 对原用户不可见（渲染占位而非消失）、对 Auditor 可见、
  对 eval 复算通道仍可用。
- Runtime Verify handler 调用 Evidence application service，结果/失败以 canonical Task event 提交。

## 7. Required tests and Gate

```bash
uv run pytest tests/unit/evidence tests/contract/evidence -q
uv run pytest tests/integration/ask tests/security/evidence_access -q
npm --prefix apps/web run test:e2e -- ask-evidence.spec.ts
uv run zhiwei eval run --suite factqa-v1 --mode fixture --seal
uv run zhiwei eval run --suite ask-v1 --mode offline --seal
uv run zhiwei verify evidence tests/fixtures/evidence/valid.bundle
uv run zhiwei verify evidence tests/fixtures/evidence/tampered.bundle && exit 1 || test $? -eq 6
```

Gate artifact 给出 Claim coverage、tamper matrix、partial/abstain、cross-source completion、cost/latency mode。

## 8. Claim boundary

Evidence 证明可复算输入/值/span，不证明所有 inference 正确。旧 FactQA 结果不能外推为高级知识库能力。
