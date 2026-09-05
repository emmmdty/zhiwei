# S9 Eval / Release / Observability 交接与验收记录

> 验收日期：2026-09-06　HEAD：`f3d5ba7`　验收依据：specs/s9-eval-release-observability.md §7 Gate
> + 多轮独立 subagent 验收（R1 三路分域）+ 交叉检验 subagent（R2 红队 + 对照）+ 权威终轮全量 Gate
> 执行方：执行 subagent 分波（T1/T3/T6 → T2 → T4 → T5 → T7 → T8 + 两轮缺陷/差距修复）

## 1. 交付范围（对应 plan Task 1–8，35/35 checkbox 完成）

| 域 | 交付 | 关键文件 |
| --- | --- | --- |
| T1 campaign/执行模式 | campaign 划分（精确覆盖/终态推进/child resume）、六模式 BindingSpec（live 仅 `for_live` operator token，消费点全路径 `ensure_live_gate`）、EvalRun 冻结 prereg/model/source/attempt manifest 引用、migration 0013 | `evals/{campaigns,bindings}.py`、`0013_evals_campaigns.py` |
| T2 统计/评分/密封 | McNemar 精确双侧、独立性单位层配对 bootstrap、Holm、Wilson n/CI、完整失败分母（refused/error 不剔题）、scorer 隔离（ScorerInput extra=forbid）、human 协议五要素、ADR-002 七项 ROI 指标入密封载荷（参与 seal digest，旧载荷向后兼容）、eval.report 工件、runner 编排 | `evals/{statistics,reports,runner,sealing}.py`、`evals/scorers/` |
| T3 外部/盲测/变形 | bird/promptfoo/inspect/longmemeval/locomo 适配器（preflight + 机器可读 unavailable，绝不静默下载）、holdout key 边界（无 env/文件发现）、metamorphic 注册（disclose-only） | `evals/external/` |
| T4/T4b release/claims | 生命周期 draft→…→retired + SoD、cohort 路由（user>workspace>default、fail closed）、rollback 仅新 Run、security suspend 压倒 pin、Claim 状态机（仅已复核密封件可升级、fixture 拒绝于 offline 边）、migration 0015、API 路由 releases/claims/evals | `agents/{release,claims,rollout}.py`、`api/{releases,claims,evals}.py`、`0015_release_claims.py` |
| T5 checker/attestation | strict release checker（claims 块 marker 语义 + 相邻数字必须等于 bound_value）、模板填充（SealedValue provenance）、HMAC attestation（dry-run 永不签名/写盘）、CLI `eval verify --all-sealed` / `eval report` / `release check --strict` / `release attest` | `release/{checker,templates,attestation}.py`、`cli/{release,evals}.py` |
| T6 telemetry/costs | W3C traceparent API 中间件、10 层 span 接线（metadata-only、默认 no-op）、GenAI semconv pin（1.36.0）、closed failure taxonomy（17 machine codes）、Cost Ledger（reserve/reconcile/variance 不门禁、canonical 持久化）、redaction（sentinel/PII 扫描）、migration 0014 | `telemetry/{traces,metrics,logs,costs,failures,redaction,fastapi}.py`、`0014_cost_ledger.py` |
| T7 web | evals/releases/observability/costs 四 feature（scope 标签、分母、unknown 如实呈现、Auditor 只读、report 需显式 scope 输入）+ canonical run timeline（trace journey）+ e2e `eval-release-observability.spec.ts`（mock 与生产字段逐一比对、fail-loud） | `apps/web/src/features/*`、`apps/web/e2e/` |
| T8 Gate 重跑 | README claims 块绑定 Claim Registry（10 marker，零裸数字）、`deploy/seed_s9_gate_claims.py`（bind_value 服务路径）、artifacts/gates/s9 权威证据 | `README.md`、`deploy/seed_s9_gate_claims.py`、`artifacts/gates/s9/` |

## 2. 权威终轮 Gate（2026-09-06，已验证）

- `make evals` 822 / `make determinism` ✓ / ruff 0 / pyright 0 / **pytest 3776 passed / 0 failed**（6 skipped / 20 deselected）
- 密封 13 件（11 suite 含 security-v1 14/14 + legacy 26/26 + longmemeval planned/unavailable）→ `eval verify --all-sealed` **14/14**
- Claim Registry 11 条（10 offline_verified + longmemeval planned）；README claims 块 0 findings（strict，50 files）
- `release attest --dry-run` signed=false 无写盘；e2e 8/8
- 明细与 digest：`artifacts/gates/s9/report.md` §14 + `sealed-runs.json`

## 3. 验收轮与缺陷闭环

| 轮 | 形式 | 结果 |
| --- | --- | --- |
| R1 | 3 路独立分域验收（evals / release / telemetry+web+gate），只读 + REPL 独立复算 + 逃逸探针 | 4 项 ACCEPT；3 项 ACCEPT-WITH-DEFECTS：live 门 `model_copy` 绕过、checker 相邻数字逃逸、bound_value 写路径不可达、UI report 缺必填 scope（M/M/M-MAJOR + LOW×4） |
| 修复轮 | RED→GREEN（`21887ce`/`c3a1b14`） | 全部关闭；全量 3706 passed |
| R2 | 交叉检验：红队（复执行探针 + 跨域 bypass + 变异检验）+ 对照（spec/plan 逐节、claim 溯源、例外台账） | 修复项全部 SECURE；租户穿越/密封件溯源/attest 无钥拒绝 SECURE；NEW-1（claim_id 夹带，可利用）+ NEW-2（offline 边缺变异钉）+ 3 处未登记 GAP |
| R2 修复 | 两执行者并行（`7c2bc40`/`f3d5ba7`、`eb8b28f`/`7bf0ab8`） | NEW-1 charset 三层拒绝；NEW-2 设计方冻结补钉（`07e2ed3`）；FailureCode +5；demo 面；span 10 层 + traceparent；security-v1；trace journey |
| R3 | 权威终轮（设计/operator 亲自执行） | §2 全绿 |

设计方 RED 修订三处（均记录于 commit message）：usage-metric 算术 4520（原 4700 与 ADR-002 权重矛盾）、
scorer 隔离测试 model_validate 化（pyright 结构性误报）、TemplateFilling 夹具补 evidence 绑定 + `_scan`
漏参一行修。

## 4. 例外与遗留（ADR-012）

**本轮登记**：Reliability/Performance 层级 suite（解锁：S11 确定性故障/固定负载 runner）；报告工件
延迟列（离线确定性路径无 wall-clock 语义，随 live 密封产生）。既有登记维持：live/shadow/human
suite（operator-only）、外部四件套 claim planned、S4/S6/S7/S8 e2e（复执行时点 S10 Gate）、
hidden reasoning 四持久化面测试（S3 债务）。

**跟踪项（非阻塞，S10 注意）**：`/route` 的 suspended 为调用方声明（advisory，强制在 capability
gateway）；policy gate `authorize_mutation` 空 ResourceContext 使 `review_publish` 等 cell 结构性
不可达（S1/S2 policy 矩阵设计缺口，域层 SoD 仍有效）；TaskFailed 尚无生产方写 machine code
（taxonomy 消费端就绪）；bindings/human 协议暂无生产消费者（按 plan Task 1 契约范围）。

## 5. S10 放行

S9 Gate 全绿，无未登记缺口，**放行进入 S10**。S10 开工注意：S4/S6/S7/S8 e2e 例外复执行并入
S10 Studio Gate；web 已有四 feature + App.tsx 导航/只读模式为 Studio 收敛基线；S9 API 面
（evals/releases/claims/observability）即 Studio 的发布流集成对端。
