# S9 Gate Report（specs/s9 §7 Gate + §8 claim boundary，Task 8 执行记录）

执行日期：2026-09-06（密封件 UTC 时间戳为 2026-09-05/06 交界）　执行者：S9-T8 executor（autonomous）
执行性质：faithful execution（判分层另行处理）；本报告为逐命令 verbatim 记录

> **执行顺序说明（权威证据口径）**：全量 `pytest -q` 会经
> tests/integration/foundation/test_database.py 的 drop/recreate fixture 重建
> zhiwei_test（schema 回 head、数据清零）。首轮按 runbook 顺序（先密封后全量
> pytest）产出的 13 个密封件与 11 条 claim 因此被清库作废——首轮 `release check
> --strict` 通过时 registry 在场（`{"checked_files": 50, "findings": []}`），复检
> 时已被清空，如实留档于此。权威证据为 **全量 pytest 之后的复执行轮**（§3–§7 的
> JSON/digest 均为复执行轮 verbatim 输出）。两轮的差异仅为随机 run/org/manifest
> UUID 派生的 seal digest；绑定值（sample 聚合）两轮完全一致。

## 0. 环境

```text
HEAD:    a8ff3ea7a4a0d751bcc88023024c2483c7368b83（工作区起始干净；证据产出时含本任务 3 个提交）
Python:  3.11.15（uv）
Node:    v24.15.0（apps/web engines 声明 >=22 <23；npm 未启用 engine-strict，构建/e2e 正常完成）
PostgreSQL: 17.6 @127.0.0.1:55432（compose zhiwei-s0：postgres/keycloak/opa 全部 healthy）
CLI 环境: ZHIWEI_PROFILE=test ZHIWEI_RELEASE_MODE=fixture_only
          ZHIWEI_DATABASE_URL=postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test（租户面）
          ZHIWEI_DATABASE_URL=postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test（系统级面，仅 verify/check/attest）
          ZHIWEI_OBJECT_STORE_ROOT=/tmp/opencode/s9-gate/objects（密封与 claim 复核同一 store）
未读取 .env；全程未发出任何 live 模型请求（release_mode=fixture_only）。
```

## 1. 清库与迁移（步骤 1）

```text
$ uv run python - <<'EOF'   # zhiwei_migrator（ADMIN_DSN 口径，tests/integration/foundation/test_database.py）
DROP DATABASE IF EXISTS zhiwei_test WITH (FORCE); CREATE DATABASE zhiwei_test
EOF
[s9-gate] zhiwei_test dropped and recreated (owner zhiwei_migrator)

$ uv run alembic heads
0015_release_claims (head)

$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_foundation, ...
（0001_foundation → 0015_release_claims 全链 15 个 migration 逐条执行，略）
… Running upgrade 0014_cost_ledger -> 0015_release_claims, S9：Agent release 治理面与 Claim Registry——agent_releases + claim_registry（FORCE RLS）。

$ uv run alembic current
0015_release_claims (head)
```

head = `0015_release_claims` ✓（计划要求）。

## 2. 冻结资产（步骤 2）

```text
$ make evals
[gen] 模板题合计 84 题 → evals/questions/
[gen] 手工题合计 36 题 → evals/questions/manual/
[risk] 36 期 · 营收 588 行 · 应收 720 行 · 供应 432 行 · 现金流 144 行
[risk] 植入模式 14 条 {'easy': 4, 'medium': 6, 'hard': 4}，干扰项 7 条
[checksums] 26 个产物 → evals/CHECKSUMS.sha256
[validate] 822 项校验全部通过

$ make determinism
[determinism] ✓ 两次干净重建产物逐字节一致
[checksums] 26 个产物 → evals/CHECKSUMS.sha256
```

## 3. 全量离线密封（步骤 3，复执行轮 = 权威证据）

**mode 口径说明（与 runbook 文本的单处偏差，原因登记）**：claim 状态机
`_EVIDENCE_EDGES`（src/zhiwei/agents/claims.py，A 档冻结契约）规定
`IMPLEMENTED → offline_verified` 仅接受 `mode="offline"` 的密封件——fixture
密封件升级即被 `ClaimUpgradeDenied` 拒绝。runbook 约束「all modes fixture/offline
ONLY — NEVER live」，故 claim 支撑 suite 以 `--mode offline --seal` 执行；
`legacy-assets`（不支撑任何 claim）按 runbook 原文以 `--mode fixture --seal`
执行。两模式均在允许集合内，且都真实经生产 executor 密封。

### 3.1 seal-empty + legacy-assets（S0 资产面）

```text
$ uv run zhiwei eval seal-empty --check
{"mode": "fixture", "run_status": "succeeded", "eval_run_status": "sealed", "registered_units": 0, "verified": true, "run_id": "1022e081-4965-4a75-962d-b7ff7e2c6163", "eval_run_id": "bd4cff23-6f06-4a5e-b523-0c092c6895be", "manifest_id": "7b6da641-c196-4301-b596-bc9819ca6de3", "seal_digest": "sha256:448d61b3461f892f6143e518afc1166111b49898ea1f4f8a9406e62d39ee66e2"}
（exit 0）

$ uv run zhiwei eval run --suite legacy-assets --mode fixture --seal
{"mode": "fixture", "executor": "legacy", "registered_units": 26, "terminal_units": 26, "eval_run_id": "1d0a0030-3799-42a0-a8b1-10ddae56c3b6", "organization_id": "beb56536-f6ad-4d65-aad1-4f92726e2523", "workspace_id": "f8757321-02dc-4b81-be71-758f0e2abd68", "sealed": true, "seal_digest": "sha256:6123091f74207be708e2ddff8beacc8caa8ff956219312130a0e633a97a239b6"}
（exit 0）
```

### 3.2 Runtime / Knowledge / S6 / S7 / S8 suites（claim 支撑面，--mode offline）

```text
$ uv run zhiwei eval run --suite runtime-contract-v1 --mode offline --seal
{"suite": "runtime-contract-v1", "mode": "offline", "executor": "agent-runtime", "registered_units": 7, "terminal_units": 7, "status_counts": {"completed": 7}, "eval_run_id": "4753fa0d-f88f-47b2-8906-c72dc443f619", "organization_id": "89a1376b-b9c6-41a8-ba50-0079e7364dc3", "workspace_id": "ff5ff40e-12a5-457c-b5d5-14974ed39513", "sealed": true, "seal_digest": "sha256:a0b9de6e63156d202eb2a3fe22da3e85abac77b95600acd15253da23b2c454a2"}

$ uv run zhiwei eval run --suite knowledge-doc-v1 --mode offline --seal
{"suite": "knowledge-doc-v1", "mode": "offline", "executor": "knowledge-retrieval", "production_path": "RetrieveTaskHandler->KnowledgePlanner", "corpus_digest": "sha256:209d480ede352b35738f0e469ea3f13daded8fee334e10caba769bced9a54da5", "registered_units": 15, "terminal_units": 15, "status_counts": {"completed": 15}, "eval_run_id": "7eb70d75-5b2a-482f-9f78-515f43bf8363", "organization_id": "24696885-e971-4362-a854-be992b541a18", "workspace_id": "194e0d9d-514e-4d3d-9aa7-4d73af84a88e", "sealed": true, "seal_digest": "sha256:bba1ce84ff6d8f81c37f1c350f9f2b3d8aed1c4a8985aad5cbece5d2dca7d915"}

$ uv run zhiwei eval run --suite knowledge-code-github-v1 --mode offline --seal
{"suite": "knowledge-code-github-v1", "mode": "offline", "executor": "knowledge-retrieval", "production_path": "RetrieveTaskHandler->KnowledgePlanner", "corpus_digest": "sha256:60c2fbc356dc642fdbd7ba673c9aedefe71c746dbf2db1dc0b2383881d29a3ba", "registered_units": 15, "terminal_units": 15, "status_counts": {"completed": 15}, "eval_run_id": "a42b0356-a4e1-4e86-90b0-00958ba5c01b", "organization_id": "8dc92191-f0da-4fe9-bb15-3730f54ba657", "workspace_id": "fa82f73b-5960-4402-9bac-c86852e6e945", "sealed": true, "seal_digest": "sha256:db214ad95b973a0f797796d42e14518cd1d62935e8fddacee543726765f24479"}

$ uv run zhiwei eval run --suite knowledge-cross-source-v1 --mode offline --seal
{"suite": "knowledge-cross-source-v1", "mode": "offline", "executor": "knowledge-retrieval", "production_path": "RetrieveTaskHandler->KnowledgePlanner", "corpus_digest": "sha256:a4672f97f4339f584082678ed2e45ad0f6c43fb5f89f1d26df139080c82cd1f2", "registered_units": 12, "terminal_units": 12, "status_counts": {"completed": 12}, "eval_run_id": "ca0ecb93-c60c-463a-83da-cc769a5eadbc", "organization_id": "a09baa3e-7d70-4243-a75f-90fd24850ece", "workspace_id": "dae93a12-b387-4356-b9af-7ef01822bc32", "sealed": true, "seal_digest": "sha256:cef42d9b1e6c588ed5920224b024e14f94cd9c3f82f258f34fc7dbd325bf52fc"}

$ uv run zhiwei eval run --suite knowledge-acl-freshness-v1 --mode offline --seal
{"suite": "knowledge-acl-freshness-v1", "mode": "offline", "executor": "knowledge-retrieval", "production_path": "RetrieveTaskHandler->KnowledgePlanner", "corpus_digest": "sha256:956f3eecba1a64da1eed068d56e6843d3939bf42c0d4af3e5abe4282353cbb71", "registered_units": 11, "terminal_units": 11, "status_counts": {"completed": 11}, "eval_run_id": "9a1c25bd-d297-452a-8a5a-c7d5b4c39500", "organization_id": "1d5e3753-4870-43e9-8727-f7cf5ce894e2", "workspace_id": "1800b32b-2aa5-44c4-aed6-9b66460b01d5", "sealed": true, "seal_digest": "sha256:2a26e7ff300578ca55661f27ac522cd39c4e646a299dd6461633ade7a3698ecc"}

$ uv run zhiwei eval run --suite enterprise-memory-v1 --mode offline --seal
{"suite": "enterprise-memory-v1", "mode": "offline", "executor": "memory-lifecycle", "production_path": "WriteMemoryCandidateHandler->MemoryPolicy->CandidateQueue-ConfirmationWorkflow-ConflictManager-ForgetManager", "registered_units": 12, "terminal_units": 12, "status_counts": {"completed": 12}, "eval_run_id": "162382ec-f0ce-4445-b47a-35b8f2f7c51a", "organization_id": "158a5ec5-2ec3-4197-9da2-63917a8fc57e", "workspace_id": "8413d8bb-5c86-4043-b9ec-89126deaab86", "sealed": true, "seal_digest": "sha256:f80afbd188afe9b5ccbff55bf72e93c472e5de6cc15abf5cadc43d454833b813"}

$ uv run zhiwei eval run --suite factqa-v1 --mode offline --seal
{"suite": "factqa-v1", "mode": "offline", "executor": "evidence-sql-replay", "production_path": "FrozenSnapshotReplay->QueryReplayRef->EvidenceVerifier", "registered_units": 120, "terminal_units": 120, "status_counts": {"completed": 120}, "eval_run_id": "551f9c26-0f3a-41e8-bd7b-9858c752d9b2", "organization_id": "10f1385e-83e3-4725-9e32-ecefb7334b5a", "workspace_id": "cc52cfdc-d15b-4cc6-b9af-84879919b64e", "sealed": true, "seal_digest": "sha256:6dd34cdac27d66f2be829c6e362f27b47eede983577dad8b8057667a8e8e4278"}

$ uv run zhiwei eval run --suite numeric-risk-v1 --mode offline --seal
{"suite": "numeric-risk-v1", "mode": "offline", "executor": "numeric-detector-pack", "production_path": "FrozenRiskSnapshot->NumericPatternDetector->Signal->RiskHypothesis->NegativeProbe(deterministic)->FalsificationResult", "registered_units": 22, "terminal_units": 22, "status_counts": {"completed": 22}, "eval_run_id": "25a6f15d-a853-41a1-8419-83cb5cc5a6f3", "organization_id": "64a3ad26-fbc4-43d1-9207-807b8fdee6ee", "workspace_id": "1ecf6d6f-928d-447f-9d1f-2d040a096c5e", "sealed": true, "seal_digest": "sha256:60590e1bb8c67795df22c8d4bc2e28361dd58f2075aa930472fc606d88d36e68"}

$ uv run zhiwei eval run --suite discover-blind-v1 --mode offline --seal
{"suite": "discover-blind-v1", "mode": "offline", "executor": "numeric-detector-pack", "production_path": "FrozenRiskSnapshot->NumericPatternDetector->Signal->RiskHypothesis->NegativeProbe(deterministic)->FalsificationResult", "registered_units": 5, "terminal_units": 5, "status_counts": {"completed": 5}, "eval_run_id": "15c9af47-06a1-417b-9451-9350dee2f6fc", "organization_id": "9eca29d7-96e1-4ca1-ae09-ca652846cfea", "workspace_id": "7599e8d8-512c-4a74-b968-1277c258deea", "sealed": true, "seal_digest": "sha256:c218111db49988823f902e66b4742af9dd903ebeb7a60b960ac641ce00b12c9c"}

$ uv run zhiwei eval run --suite ask-v1 --mode offline --seal
time=2026-09-06T02:49:10.729 level=WARN msg="Cluster Id in Cluster Metadata config is not a valid uuid. Generating a new Cluster Id" component=metadata-initializer
Invalid bundle: Fact claim requires replayable or copy_frozen evidence; got reference_only on DocRef
{"suite": "ask-v1", "mode": "offline", "executor": "agent-runtime", "production_path": "RunCommandService->AgentRunWorkflow->AskTaskGraph", "registered_units": 6, "terminal_units": 6, "status_counts": {"completed": 6}, "eval_run_id": "60bc3587-6cf1-4cf1-91eb-f788d9870e51", "organization_id": "b973dc9a-f912-4b62-bfc4-afeb0c0ac6a7", "workspace_id": "1b39f5e2-7c15-43cf-8c97-b2f5c72871cd", "sealed": true, "seal_digest": "sha256:fbb351f9793e32a5fd92ac14403f13243c470fa926ead16bffa07f4c8a5b1b07"}
（stderr 的 Temporal dev server 启动 WARN 与一条 negative-path 场景日志如实留档；
6/6 单位 completed、密封成功，与首轮行为一致。）
```

### 3.3 外部基准可用性（fail closed 如实）

```text
$ uv run zhiwei eval external-status --suite longmemeval-adapter --seal
{"suite": "longmemeval-adapter", "benchmark": "longmemeval", "external_status": "unavailable", "reasons": [{"code": "missing_file", "path": "evals/external/longmemeval/LICENSE", "detail": "数据许可文件缺失"}, {"code": "missing_file", "path": "evals/external/longmemeval/VERSION", "detail": "数据版本文件缺失"}, {"code": "missing_data_dir", "path": "evals/external/longmemeval/data", "detail": "数据目录不存在"}], "run_kind": "none", "claim": {"benchmark": "longmemeval", "claim_status": "planned/unavailable"}, "eval_run_id": "63891d88-69ff-425f-9e42-6cc24c5bbaf9", "organization_id": "a3b839ea-91a5-4dfe-943d-fea97163f2c1", "workspace_id": "707becc6-39a4-4f32-b34a-53a61588b4dc", "sealed": true, "seal_digest": "sha256:a6774a896c9b1060a9927be46bf9d15fa48ad8b81996db0dcd467f7d93e338f4"}
（exit 0；unavailable → planned/unavailable sealed，机器可读原因在案）
```

密封汇总：13 个 sealed EvalRun（seal-empty / legacy-assets / runtime-contract-v1 /
knowledge×4 / enterprise-memory-v1 / factqa-v1 / numeric-risk-v1 / discover-blind-v1 /
ask-v1 / longmemeval-adapter external-status）。verbatim JSON 数组已固化在
`artifacts/gates/s9/sealed-runs.json`（复执行轮）。

## 4. 系统级密封复核（步骤 4）

```text
$ ZHIWEI_DATABASE_URL=…zhiwei_app… uv run zhiwei eval verify --all-sealed
{"checked": 0, "verified": 0, "failures": []}
（exit 0——但 checked=0 是 app DSN 在 FORCE RLS 下看不到租户行的 vacuous 成功；
命令本身要求系统级 maintenance DSN，见 _verify_all_sealed_flow docstring。如实记录后换 DSN 复跑。）

$ ZHIWEI_DATABASE_URL=…zhiwei_migrator… uv run zhiwei eval verify --all-sealed
{"checked": 13, "verified": 13, "failures": []}
（exit 0；复执行轮密封后再次执行，输出同上 13/13——两轮均真实执行并留档）
```

**Gate 口径：checked=13 / verified=13 / failures=[]（maintenance DSN，复执行轮）**。
app DSN 的空集结果不计入 Gate 结论（空集不构成证据），但如实留档。

## 5. Claim Registry seeding（步骤 5，复执行轮）

脚本：`deploy/seed_s9_gate_claims.py`（沿用 deploy/seed_identity_e2e.py 约定：不读
.env、幂等、机器可读输出）。路径为真实服务路径：
`ClaimRegistryService.register` → 手工 `upgrade(IMPLEMENTED)` →
`upgrade(OFFLINE_VERIFIED, eval_run_id=…)`（服务层从 object store 独立复算密封件，
复算 digest 作证据锚点）→ `render_claim` 以 `SealedValue(source="sealed_artifact",
seal_digest=复算 digest)` 填充模板 → `bound_value` 落库（0015 迁移显式授权的列级
UPDATE；服务层暂无绑定值入口，属种子层职责，已在脚本 docstring 登记）。
绑定值聚合自密封 run 的 EvalSample 终态；claim 的 scope
mode/version/date/corpus 逐字段从密封件（mode、migration revision）与 EvalRun 行
（sealed_at UTC 日期）复制。租户定位仅取步骤 3 verbatim 输出的
eval_run_id/organization_id/workspace_id（`artifacts/gates/s9/sealed-runs.json`）。

```text
$ uv run python deploy/seed_s9_gate_claims.py
[seed] ✓ factqa-v1.accuracy -> offline_verified sha256:6dd34cdac27d66f2be829c6e362f27b47eede983577dad8b8057667a8e8e4278
[seed] ✓ knowledge-doc-v1.retrieval -> offline_verified sha256:bba1ce84ff6d8f81c37f1c350f9f2b3d8aed1c4a8985aad5cbece5d2dca7d915
[seed] ✓ knowledge-code-github-v1.retrieval -> offline_verified sha256:db214ad95b973a0f797796d42e14518cd1d62935e8fddacee543726765f24479
[seed] ✓ knowledge-cross-source-v1.retrieval -> offline_verified sha256:cef42d9b1e6c588ed5920224b024e14f94cd9c3f82f258f34fc7dbd325bf52fc
[seed] ✓ knowledge-acl-freshness-v1.retrieval -> offline_verified sha256:2a26e7ff300578ca55661f27ac522cd39c4e646a299dd6461633ade7a3698ecc
[seed] ✓ enterprise-memory-v1.pass -> offline_verified sha256:f80afbd188afe9b5ccbff55bf72e93c472e5de6cc15abf5cadc43d454833b813
[seed] ✓ numeric-risk-v1.recall-d0 -> offline_verified sha256:60590e1bb8c67795df22c8d4bc2e28361dd58f2075aa930472fc606d88d36e68
[seed] ✓ discover-blind-v1.blind-pass -> offline_verified sha256:c218111db49988823f902e66b4742af9dd903ebeb7a60b960ac641ce00b12c9c
[seed] ✓ runtime-contract-v1.contract-pass -> offline_verified sha256:a0b9de6e63156d202eb2a3fe22da3e85abac77b95600acd15253da23b2c454a2
[seed] ✓ ask-v1.contract-pass -> offline_verified sha256:fbb351f9793e32a5fd92ac14403f13243c470fa926ead16bffa07f4c8a5b1b07
[seed] ✓ longmemeval.external-diagnostic -> planned（外部基准不可用，不解锁质量 claim）
[seed] ✓ 11 条 claim 就绪
（exit 0；首轮执行后复跑亦 exit 0，幂等跳过——"[seed] • longmemeval.external-diagnostic 已存在（保持 planned）"）
```

绑定值明细（bound_value，均由密封 sample 聚合得出；两轮聚合结果一致）：

| claim_id | 绑定值 | 聚合口径 |
| --- | --- | --- |
| factqa-v1.accuracy | `1.000（120/120 samples）` | sealed sample 的 `score` 均值 |
| knowledge-doc-v1.retrieval | `1.000（15/15 samples）` | 同上 |
| knowledge-code-github-v1.retrieval | `1.000（15/15 samples）` | 同上 |
| knowledge-cross-source-v1.retrieval | `1.000（12/12 samples）` | 同上 |
| knowledge-acl-freshness-v1.retrieval | `1.000（11/11 samples）` | 同上 |
| enterprise-memory-v1.pass | `1.000（12/12 samples）` | 同上 |
| numeric-risk-v1.recall-d0 | `0.786（11/14 planted targets）` | planted 单位 matched 比例（与 `risk generate --check` 的诚实口径 0.786 一致） |
| discover-blind-v1.blind-pass | `1.000（5/5 units）` | sealed sample 的 `correct` 比例 |
| runtime-contract-v1.contract-pass | `7/7` | 生产 Runtime 路径终态单位 |
| ask-v1.contract-pass | `6/6` | AskTaskGraph 生产路径终态单位 |

## 6. README claim 表（步骤 6）

README.md 新增「公开声明（Claim Registry 绑定）」小节：`<!-- claims:start -->` /
`<!-- claims:end -->` 块内数字只以 `{{claim:ID}}` marker 出现（10 条
offline_verified claim；planned 的 longmemeval 不入块——checker 对非 verified marker
一律出 finding）。块内不出现任何裸数字（suite 名与版本号含数字，一律不写入块内文本；
ISO 口径日期被 checker 剔除）。历史资产数字（120/112/57、Risk planted、`$43.0231552`）
未加入 README，维持 docs/BENCHMARK.md、docs/RISK_EVAL.md 既有窄口径标注原样（语料
内部边界，未扩写未删除）。无任何生产 SLO 表述。

## 7. strict release check 与 attestation dry-run（步骤 7，复执行轮）

```text
$ ZHIWEI_DATABASE_URL=…zhiwei_migrator… uv run zhiwei release check --strict
{"checked_files": 50, "findings": []}
（exit 0；registry 经 maintenance DSN 系统级读取——checker 必须看到全部租户的 claim。
首轮在清库作废前同输出通过；复执行轮在 claim 重播种后再次通过。）

$ ZHIWEI_DATABASE_URL=…zhiwei_migrator… uv run zhiwei release attest --dry-run
{"signed": false, "provenance": {"commit": "…", "generated_at": "…", "generator": "zhiwei-release-check"}, "content_digests": {…49 个表面文件 digest，含 "artifacts/gates/s9/sealed-runs.json": "sha256:ded6798f831a3b1f…"…}}
（exit 0；signed=false，未写任何文件——dry-run 后 `git status --short` 仅含本任务
预期改动，验证通过。完整 JSON 在执行日志留档。）
```

## 8. 全仓 Gate（步骤 8 + specs/s9 §7）

```text
$ uv run pytest tests/unit/evals tests/unit/telemetry tests/contract/evals -q
282 passed in 1.96s

$ uv run pytest tests/integration/evals tests/integration/release tests/security/telemetry_redaction -q
28 passed in 4.00s

$ uv run ruff check .
All checks passed!

$ uv run pyright
0 errors, 0 warnings, 0 informations

$ uv run pytest -q            # 全量、单进程、步骤 1 的同一干净迁移库
3689 passed, 6 skipped, 20 deselected, 4 warnings in 271.25s (0:04:31)
（注：全量 pytest 会重建 zhiwei_test 数据面——见报告头部「执行顺序说明」；
§3–§7 权威证据在该轮之后产出。）

$ npm --prefix apps/web run build
✓ built in 524ms（tsc -b + vite build，无类型错误）

$ npm --prefix apps/web run test:e2e -- eval-release-observability.spec.ts
4 passed (7.8s)

$ npm --prefix apps/web run test:e2e -- runtime-approval.spec.ts
3 passed (6.1s)
```

## 9. Gate 结论表

| Gate 项 | 命令 | exit | 结果 |
| --- | --- | --- | --- |
| 冻结资产校验 | `make evals` | 0 | 822 项全过、26 个冻结资产 |
| 确定性重建 | `make determinism` | 0 | 两次干净重建逐字节一致 |
| schema | `alembic heads` / `current` | 0 | `0015_release_claims` (head) |
| S0 密封 | `eval seal-empty --check` | 0 | 密封 ✓ verified=true |
| legacy 资产 | `eval run --suite legacy-assets --mode fixture --seal` | 0 | 26/26 密封 |
| S2 契约 | `eval run --suite runtime-contract-v1 --mode offline --seal` | 0 | 7/7 密封 |
| S5 知识 | knowledge×4 `--mode offline --seal` | 0 | 15·15·12·11 密封 |
| S6 Evidence/Ask | factqa-v1 / ask-v1 `--mode offline --seal` | 0 | 120/120、6/6 密封 |
| S7 Memory | enterprise-memory-v1 `--mode offline --seal` | 0 | 12/12 密封 |
| S7 外部 | `eval external-status --suite longmemeval-adapter --seal` | 0 | unavailable→planned/unavailable 密封（fail closed 如实） |
| S8 风险 | numeric-risk-v1 / discover-blind-v1 `--mode offline --seal` | 0 | 22/22、5/5 密封（recall 0.786 如实） |
| 密封复核 | `eval verify --all-sealed`（maintenance DSN） | 0 | checked=13 / verified=13 / failures=[] |
| Claim Registry | `deploy/seed_s9_gate_claims.py` | 0 | 11 条 claim（10 offline_verified + 1 planned），复跑幂等 |
| release 检查 | `release check --strict` | 0 | 50 文件 / findings=[] |
| attestation | `release attest --dry-run` | 0 | JSON `signed:false`，未写文件（git status 验证） |
| spec §7 单测 | pytest unit evals+telemetry, contract evals | 0 | 282 passed |
| spec §7 集成 | pytest integration evals+release, telemetry redaction | 0 | 28 passed |
| lint/type | `ruff check .` / `pyright` | 0 | 0 / 0 errors |
| 全量回归 | `pytest -q` | 0 | 3689 passed / 6 skipped / 20 deselected / 0 failed |
| Web 构建 | `npm --prefix apps/web run build` | 0 | ✓ |
| Web e2e | eval-release-observability.spec.ts | 0 | 4 passed |
| Web e2e | runtime-approval.spec.ts | 0 | 3 passed |

## 10. Claim Registry 表（claim_id → status → seal_digest → scope，复执行轮绑定）

| claim_id | status | seal_digest | mode | model | version | date | corpus | environment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| factqa-v1.accuracy | offline_verified | sha256:6dd34cdac27d66f2be829c6e362f27b47eede983577dad8b8057667a8e8e4278 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | factqa-v1 | offline-fixture |
| knowledge-doc-v1.retrieval | offline_verified | sha256:bba1ce84ff6d8f81c37f1c350f9f2b3d8aed1c4a8985aad5cbece5d2dca7d915 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | knowledge-doc-v1 | offline-fixture |
| knowledge-code-github-v1.retrieval | offline_verified | sha256:db214ad95b973a0f797796d42e14518cd1d62935e8fddacee543726765f24479 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | knowledge-code-github-v1 | offline-fixture |
| knowledge-cross-source-v1.retrieval | offline_verified | sha256:cef42d9b1e6c588ed5920224b024e14f94cd9c3f82f258f34fc7dbd325bf52fc | offline | reference-fixture | 0015_release_claims | 2026-09-05 | knowledge-cross-source-v1 | offline-fixture |
| knowledge-acl-freshness-v1.retrieval | offline_verified | sha256:2a26e7ff300578ca55661f27ac522cd39c4e646a299dd6461633ade7a3698ecc | offline | reference-fixture | 0015_release_claims | 2026-09-05 | knowledge-acl-freshness-v1 | offline-fixture |
| enterprise-memory-v1.pass | offline_verified | sha256:f80afbd188afe9b5ccbff55bf72e93c472e5de6cc15abf5cadc43d454833b813 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | enterprise-memory-v1 | offline-fixture |
| numeric-risk-v1.recall-d0 | offline_verified | sha256:60590e1bb8c67795df22c8d4bc2e28361dd58f2075aa930472fc606d88d36e68 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | numeric-risk-v1 | offline-fixture |
| discover-blind-v1.blind-pass | offline_verified | sha256:c218111db49988823f902e66b4742af9dd903ebeb7a60b960ac641ce00b12c9c | offline | reference-fixture | 0015_release_claims | 2026-09-05 | discover-blind-v1 | offline-fixture |
| runtime-contract-v1.contract-pass | offline_verified | sha256:a0b9de6e63156d202eb2a3fe22da3e85abac77b95600acd15253da23b2c454a2 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | runtime-contract-v1 | offline-fixture |
| ask-v1.contract-pass | offline_verified | sha256:fbb351f9793e32a5fd92ac14403f13243c470fa926ead16bffa07f4c8a5b1b07 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | ask-v1 | offline-fixture |
| longmemeval.external-diagnostic | **planned** | （无——不可用性密封件 sha256:a6774a896c9b1060a9927be46bf9d15fa48ad8b81996db0dcd467f7d93e338f4 是不可用证据，不解锁质量 claim） | — | — | — | — | longmemeval-adapter | — |

## 11. 例外/未执行项（ADR-012 登记）

| 阻塞项 | 根因 | 解锁条件 | 复执行时点 |
| --- | --- | --- | --- |
| live / shadow / human 模式 suite 全部未执行 | 不调用 live 模型是全仓纪律；live 只由 operator 显式触发（release_mode=fixture_only） | operator 显式 live 授权 + production_reference 档 | operator 触发后；live_verified claim 状态机已就绪 |
| BIRD / LoCoMo / Promptfoo / Inspect 外部诊断未执行、claim 保持 planned | 外部数据/许可未就绪（longmemeval 探测输出即同类机器可读原因）；本 Gate 按 runbook 仅探测 longmemeval-adapter | operator 放置许可/version/data 后 `eval external-status --seal` 走 available 分支 | 数据就位后 |
| LongMemEval 质量诊断 | 同上 + 质量诊断需 live 模型 | 同上 | 同上 |
| S4/S6/S7/S8 e2e 例外条目 | 各阶段既有登记（ADR-012 例外，条件四要素在案） | 既有登记的解锁条件 | 最迟并入 S10 Studio Gate 清单（→ 已于 2026-09-06 S10 Gate 复执行关闭，见 artifacts/gates/s10/report.md §17） |
| hidden reasoning 四个持久化面测试 | S3 遗留债务（既有登记） | 既有登记 | 既有登记 |
| pytest 6 skipped / 20 deselected | `addopts = -m 'not live and not slow'`（live/slow 按 ADR-012 §5 显式排除，本轮未加 `-m slow`） | 显式 `-m slow` 复跑 | S1 slow（OPA）已在本轮 Gate 之外既有口径中覆盖；本轮按 runbook 命令清单执行 |

## 12. 与 runbook 的偏差登记（非例外，设计约束所致）

1. **mode=offline 替代 runbook 示例中的 --mode fixture（claim 支撑 suite）**：冻结
   状态机要求 offline 模式密封件才能升 offline_verified；runbook 允许
   fixture/offline 两模式。legacy-assets 保持 fixture 原文执行。见 §3 说明。
2. **`eval verify --all-sealed` 使用 maintenance DSN**：首次以 app DSN 执行得
   `checked=0`（RLS 下空集 vacuous 成功，不计 Gate），按命令 docstring 的系统级
   语义改用 zhiwei_migrator 复跑得 13/13。两次输出均 verbatim 留档（§4）。
3. **`bound_value` 落库路径**：ClaimRegistryService 当前无绑定值入口；种子脚本按
   0015 迁移显式授权的列级 UPDATE（status/evidence/bound_value/updated_at）写入，
   模板填充仍走 render_claim 的 SealedValue provenance 冻结路径。已在脚本
   docstring 登记，属服务层已知缺口（与 api/claims.py 的 policy cell 缺口同性质的
   设计登记，非静默旁路——evidence/digest 防线全部经服务层复算）。
4. **步骤 3–7 复执行**：首轮密封/播种被全量 pytest 的清库 fixture 作废（根因见
   报告头部说明）。复执行轮在 pytest 之后产出全部密封与 claim 证据，并以
   `release check --strict`（findings=[]）+ `eval verify --all-sealed`（13/13）
   终态复核。两轮聚合绑定值一致；首轮输出未计入 Gate 结论但差异仅为随机 UUID
   派生 digest。

## 13. 提交

```text
11c6111 feat(release): seal evidence-backed claim registry             — deploy/seed_s9_gate_claims.py
a4a78e5 docs(claims): bind README claim table to sealed artifacts (S9)  — README.md
6fcf479 test(release): S9 gate evidence and report                     — artifacts/gates/s9/**（首轮）
<final>   test(release): re-seal S9 gate evidence after full-suite DB rebuild — artifacts/gates/s9/**（复执行轮证据与本报告）
```

（报告与 sealed-runs.json 属 artifacts/ gitignore 范围，按既有 gate artifact 惯例
`git add -f` 纳入。）

---

## 14. 权威终轮（R2 修复后，operator 记录 2026-09-06）

R1/R2 验收轮发现并修复缺陷后，清库重密封的全部权威证据（本轮为 Gate 最终口径，
替代 §3–§7 的复执行轮 digest；绑定值口径不变）：

| 项 | 结果 |
| --- | --- |
| `make evals` / `make determinism` | 822 项全过 / 26 资产逐字节一致（R2 后复跑） |
| `uv run ruff check .` / `uv run pyright` | 0 / 0 errors（HEAD f3d5ba7） |
| `uv run pytest -q`（全量单进程） | **3776 passed / 6 skipped / 20 deselected / 0 failed** |
| 清库 + `alembic upgrade head` | `0015_release_claims`（migrator DSN） |
| 密封（`--mode offline`，legacy=fixture，均 `--seal`） | runtime-contract-v1 7/7 · factqa-v1 120/120 · knowledge ×4 15/15/12/11 · enterprise-memory-v1 12/12 · numeric-risk-v1 22/22 · discover-blind-v1 5/5 · ask-v1 6/6 · **security-v1 14/14（R2-B 新增）** · legacy-assets 26/26 · external-status longmemeval→planned/unavailable |
| `eval verify --all-sealed`（migrator DSN） | **checked=14 / verified=14 / failures=[]**，exit 0 |
| Claim Registry seeding（`deploy/seed_s9_gate_claims.py`） | 11 条（10 offline_verified + 1 planned）；bound_value 已改走 R2-F4 的 `ClaimRegistryService.bind_value`（seal digest 匹配校验，直列 UPDATE 移除） |
| `release check --strict`（migrator DSN，默认面 README+docs+demo） | 50 files / **findings=0** / exit 0；surface 明细含 demo missing=true（如实） |
| `release attest --dry-run` | signed=false / content files=49 / 无任何文件写出 |
| e2e | eval-release-observability 4/4（R2 后 8/8 含 runtime-approval）· tenancy 13/13 既有口径 |

密封 digest 全量记录见同目录 `sealed-runs.json`（本轮权威版，13 entries）。

## 15. 验收轮记录（多轮独立 subagent + 交叉检验）

- **R1 独立验收（3 路分域）**：T1–T3 ACCEPT-WITH-DEFECTS（live 门 `model_copy`
  绕过 MEDIUM；runner docstring 过claim LOW）；T4/T4b ACCEPT；T5
  ACCEPT-WITH-DEFECTS（checker 相邻数字逃逸 MEDIUM；bound_value 写路径不可达
  MEDIUM）；T6/T8 ACCEPT；T7 ACCEPT-WITH-DEFECTS（report 调用缺必填 scope
  MAJOR；e2e mock 保真度两处）。
- **缺陷修复轮**（`21887ce`/`c3a1b14`）：F1 live 门改为消费点全路径校验
  （`ensure_live_gate`）；F3 相邻数字必须等于 bound_value 否则
  UNSUPPORTED_NUMBER；F4 新增 `ClaimRegistryService.bind_value`（唯一写入口，
  digest 匹配强制）；F5 UI report 需显式 5 项 scope 输入 + e2e mock 对齐生产
  （422/409/report:null）；F2/F6 文档与重复包裹修正。全量 3706 passed。
- **R2 交叉检验（2 路）**：红队复执行全部修复探针（live 门 5 消费点、checker
  全格式、bind_value 机器可读拒绝、租户穿越、attest 无钥拒绝）全部 SECURE；
  变异检验 4 项 3 红 1 存活（offline 边缺 fixture 拒绝钉——已由设计方冻结测试
  `07e2ed3` 补钉）；NEW-1 可利用发现（claim_id 数字空格夹带）。对照轮：模块
  27/27、Gate 命令 6/6、claim 溯源 11/11 端到端、3 处未登记 GAP。
- **R2 修复轮**（`7c2bc40`/`f3d5ba7` + `eb8b28f`/`7bf0ab8`）：
  claim_id charset 约束（三层拒绝）、FailureCode 补 5 个设计 §12 码、
  release check 默认面加 demo（missing 如实上报）、W3C traceparent 进 API
  中间件 + 10 层 span 接线（metadata-only、默认 no-op）、security-v1 suite
  （14 单元走生产安全缝、负控制验证）、canonical run timeline trace journey。
- **R3 delta 复核**：见 §14 权威终轮（全量 3776 passed + 门禁命令全绿）。

## 16. 例外/未执行项（ADR-012 登记，R2 后增量）

| 阻塞项 | 根因 | 解锁条件 | 复执行时点 |
| --- | --- | --- | --- |
| Reliability/Performance 层级 suite 未建 | 需确定性负载/故障 runner 环境 | S11-T5/T6（确定性故障 runner + 固定负载 runner）落地后按资产冻结流程建 suite | S11 |
| 报告工件延迟列 | 离线确定性路径无 wall-clock 语义（live 才有意义），spec §8 延迟披露随 live 密封件产生 | operator live 授权 | live suite 首封 |
| live/shadow/human suite、外部四件套、S4/S6/S7/S8 e2e、hidden reasoning 四测试 | 同 §11 既有登记，本轮不变 | 同 §11 | 同 §11 |
