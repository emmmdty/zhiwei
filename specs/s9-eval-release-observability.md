# S9 - Eval, Release, Observability and Cost Governance

> Status: frozen implementation specification  
> Depends on: S8  
> Unlocks: S10

## 1. Goal

把 Dataset/EvalSuite/EvalRun、Agent release、Claim Registry、OpenTelemetry、Cost Ledger 和 failure taxonomy
接入同一 Runtime，使每个公开声明能追到 sealed artifact，并支持 staged/canary/rollback。

## 2. Required modules

```text
src/zhiwei/evals/{datasets,suites,runner,bindings,statistics,sealing,reports}.py
src/zhiwei/evals/{scorers,external}/
src/zhiwei/agents/{release,claims,rollout}.py
src/zhiwei/telemetry/{traces,metrics,logs,costs,failures,redaction}.py
src/zhiwei/api/{evals,releases,claims,observability}.py
apps/web/src/features/{evals,releases,observability,costs}/
deploy/observability/
```

## 3. Eval runtime

S0 已提供 Dataset/Suite/EvalRun/version、executor port 与基本 sealing，S2 已将 executor 绑定生产 Agent
Runtime。本阶段在其上增加 campaign、全部 execution modes、统计/人评、外部诊断、发布和运营治理；
不得新建第二套 EvalRun 表或绕过已有 Runtime。

fixture/replay/offline/live/shadow/human 只替换 Model/Source/Tool binding，使用同一 AgentVersion/TaskGraph/Runtime/
Policy/Evidence。Dataset/Suite immutable version；EvalRun 冻结 config/code/source/model/attempt/prereg manifests。

所有注册 sample/unit terminal 才 seal；partial 可 resume。确定性 scorer 与 solver 隔离；human/judge 仅用于
inference/utility，并保存 rubric/blinding/order/calibration/agreement。

层级 suite：contracts、Knowledge、Context/handoff、Memory、Evidence/Action、Ask、Discover、Security、
Reliability/Performance。external BIRD/LongMemEval/LoCoMo/Promptfoo/Inspect adapters 分开报告。

## 4. Statistics and anti-self-proof

- prereg 固定 estimand/unit/denominator/exclusion/stopping/primary/secondary/multiplicity。
- internal frozen + blind holdout + external diagnostic + metamorphic/fault injection。
- proportion 报 n/CI，paired experiment 使用正确 independence unit；partial/error/refusal/retry 入分母/终态。
- naked diagnostic 只披露不剔题；12-chain handoff pilot 先 power analysis 再冻结 confirmatory。
- existing 120/112/57、Risk planted、`$43.0231552` 的范围按 docs 保持，禁止升级为平台总证据。

## 5. Release and claims

Agent lifecycle draft→sandbox→evaluated→review→staged→published→deprecated/retired。Release manifest 固定 Agent/
Pack/Model/Knowledge/Memory/Capability/Policy/Eval digests、approver、rollout/rollback。

Claim Registry 状态 planned/implemented/offline-verified/live-verified/retired。模板变量只能由 sealed artifact
填充；release checker 扫 README/docs/demo manifest，阻断无 artifact 数字、fixture/live 混写和过期 claim。

staged/canary 以 Workspace/user cohort 和 version pin 路由；rollback 只影响新 Run，在途 Run按安全策略完成/
终止。security suspend 可立即阻断 capability，不受 release pin 保护。

## 6. Telemetry and cost

- W3C trace + OTel spans for API/Run/Task/model/retrieval/memory/tool/policy/approval/evidence/eval。
- 默认 metadata only，正文采集按 policy；固定 GenAI semconv revision并有 no-secret/PII scan。
- Cost Ledger reserve/reconcile，记录 price source/confidence、tokens、cache、retry/child/tool external cost。
- **token ROI 指标**（[ADR-002](../docs/DECISIONS.md#adr-002)）：按 Run/trajectory 而非按 call 归集
  `weighted_tokens`、`authoritative_token_share`、`evidence_per_kilotoken`、`recoverable_reload_waste`、
  `context_utilization`、`compression_ratio`、`cost_per_completed_task`，写入 sealed eval artifact。token
  支出是 ROI 指标不是门禁；组织可选开启的 spend guard 单独记录，不与这组指标混用。这组指标同时是
  Context Compiler 压缩策略消融的因变量，沿用既有 prereg / paired bootstrap / Holm 校正方法。
- failure taxonomy 固定 machine code；dashboard 从 canonical/projection/OTel 构建，不从字符串日志猜状态。

## 7. Required tests and Gate

```bash
uv run pytest tests/unit/evals tests/unit/telemetry tests/contract/evals -q
uv run pytest tests/integration/evals tests/integration/release tests/security/telemetry_redaction -q
npm --prefix apps/web run test:e2e -- eval-release-observability.spec.ts
uv run zhiwei eval verify --all-sealed
uv run zhiwei release check --strict
uv run zhiwei release attest --dry-run
```

Gate 还必须运行 blind/external/fault suites 中实际可合法获取的部分；缺数据/许可时相应 claim 保持 planned，
不得生成空成功报告。

## 8. Claim boundary

公开表按 mode/model/version/date/corpus/environment 报质量、成本、延迟和失败。未完成 S11 时不写生产 SLO。
