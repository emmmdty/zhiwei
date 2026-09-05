# S9 Gate Report（specs/s9 §7 Gate + §8 claim boundary，Task 8 执行记录）

执行日期：2026-09-06（密封件 UTC 时间戳为 2026-09-05）　执行者：S9-T8 executor（autonomous）
执行性质：faithful execution（判分层另行处理）；本报告为逐命令 verbatim 记录

## 0. 环境

```text
HEAD:    a8ff3ea7a4a0d751bcc88023024c2483c7368b83（工作区起始干净）
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

## 3. 全量离线密封（步骤 3）

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
{"mode": "fixture", "run_status": "succeeded", "eval_run_status": "sealed", "registered_units": 0, "verified": true, "run_id": "08d6fdf7-d216-4d07-b1b6-b773bc80b10d", "eval_run_id": "2f685116-20b0-415c-8197-043e438c6be8", "manifest_id": "b6dd8e2e-38b6-4291-adbf-95c738e4e8f1", "seal_digest": "sha256:4a098ca863f08856b2dd765a44eb5d74082d7f2160a9e38c205f8e7d5fedff10"}
（exit 0）

$ uv run zhiwei eval run --suite legacy-assets --mode fixture --seal
{"mode": "fixture", "executor": "legacy", "registered_units": 26, "terminal_units": 26, "eval_run_id": "ed685157-a72d-4bf7-ba54-198159737227", "organization_id": "3cf869db-fc96-495e-8b59-7f59e0ec90f5", "workspace_id": "aec05e7d-e5f4-4fce-96e8-07ae519f2bfc", "sealed": true, "seal_digest": "sha256:9f498c31e3c85ebded22703aa090e03ce37a403404cd4424d8ff0c800be16cda"}
（exit 0）
```

### 3.2 Runtime / Knowledge / S6 / S7 / S8 suites（claim 支撑面，--mode offline）

```text
$ uv run zhiwei eval run --suite runtime-contract-v1 --mode offline --seal
{"suite": "runtime-contract-v1", "mode": "offline", "executor": "agent-runtime", "registered_units": 7, "terminal_units": 7, "status_counts": {"completed": 7}, "eval_run_id": "485d81ee-2fb7-4f48-8233-de7e2eaca4a9", "organization_id": "dfb1b4f8-1a08-4f2e-aee6-dbc8f51ca2fe", "workspace_id": "f52bf010-3d22-449a-be51-e262f0b1a6d1", "sealed": true, "seal_digest": "sha256:1bbb4768ce44a483a57e521cb703a92cd04799f83ed7650db5f4fb17e19c23aa"}

$ uv run zhiwei eval run --suite knowledge-doc-v1 --mode offline --seal
{"suite": "knowledge-doc-v1", "mode": "offline", "executor": "knowledge-retrieval", "production_path": "RetrieveTaskHandler->KnowledgePlanner", "corpus_digest": "sha256:209d480ede352b35738f0e469ea3f13daded8fee334e10caba769bced9a54da5", "registered_units": 15, "terminal_units": 15, "status_counts": {"completed": 15}, "eval_run_id": "a7ba5a86-d328-49a7-a3db-6f25de900724", "organization_id": "a72ca2b6-dfd4-4a20-aa34-8ff52c1ec440", "workspace_id": "d44ded08-e89f-44c8-9f9b-34a625bce404", "sealed": true, "seal_digest": "sha256:5533adb4289d3862408caea669a8e61f2538acbcf8cf002965c98588b2589132"}

$ uv run zhiwei eval run --suite knowledge-code-github-v1 --mode offline --seal
{"suite": "knowledge-code-github-v1", "mode": "offline", "executor": "knowledge-retrieval", "production_path": "RetrieveTaskHandler->KnowledgePlanner", "corpus_digest": "sha256:60c2fbc356dc642fdbd7ba673c9aedefe71c746dbf2db1dc0b2383881d29a3ba", "registered_units": 15, "terminal_units": 15, "status_counts": {"completed": 15}, "eval_run_id": "157fee2e-1aa6-4ae7-b986-cfd218137ee1", "organization_id": "821da952-cb1b-45c8-9b7e-cd5eb50ab084", "workspace_id": "efb4bf24-95e5-42d3-b19f-98b955127f96", "sealed": true, "seal_digest": "sha256:a83641a81f2300c03aad5f6d2326764c7783139e0a1361df57449e6f60e8f0f0"}

$ uv run zhiwei eval run --suite knowledge-cross-source-v1 --mode offline --seal
{"suite": "knowledge-cross-source-v1", "mode": "offline", "executor": "knowledge-retrieval", "production_path": "RetrieveTaskHandler->KnowledgePlanner", "corpus_digest": "sha256:a4672f97f4339f584082678ed2e45ad0f6c43fb5f89f1d26df139080c82cd1f2", "registered_units": 12, "terminal_units": 12, "status_counts": {"completed": 12}, "eval_run_id": "42eff8c7-9486-4918-830a-75de68c49bf2", "organization_id": "2fb3d2d6-b0f2-48d7-b2f6-a40b10aa8da4", "workspace_id": "5f0815de-0afb-4b08-993b-0d61ae234116", "sealed": true, "seal_digest": "sha256:0cdbb15c242f1c332913637522bc7e03db3294f138da27e284a2ebc66be056b4"}

$ uv run zhiwei eval run --suite knowledge-acl-freshness-v1 --mode offline --seal
{"suite": "knowledge-acl-freshness-v1", "mode": "offline", "executor": "knowledge-retrieval", "production_path": "RetrieveTaskHandler->KnowledgePlanner", "corpus_digest": "sha256:956f3eecba1a64da1eed068d56e6843d3939bf42c0d4af3e5abe4282353cbb71", "registered_units": 11, "terminal_units": 11, "status_counts": {"completed": 11}, "eval_run_id": "b81b9d16-526c-47b7-ab6d-f0aee07e4230", "organization_id": "af016e95-874d-43fb-8897-b9b1cd9fccc2", "workspace_id": "dce56d2f-f3dc-4f34-b260-5d18dcea09e1", "sealed": true, "seal_digest": "sha256:b235134bca330d2ff3a253ba9f123aaf32debd0420d0b8645085bd7257856cc1"}

$ uv run zhiwei eval run --suite enterprise-memory-v1 --mode offline --seal
{"suite": "enterprise-memory-v1", "mode": "offline", "executor": "memory-lifecycle", "production_path": "WriteMemoryCandidateHandler->MemoryPolicy->CandidateQueue-ConfirmationWorkflow-ConflictManager-ForgetManager", "registered_units": 12, "terminal_units": 12, "status_counts": {"completed": 12}, "eval_run_id": "6b323179-4cac-4a49-baa7-b9a32e0662b8", "organization_id": "19e0c7db-2f1e-41b5-af98-5ab6fb65ab61", "workspace_id": "7a629fc7-7678-4746-9f03-cacee830c882", "sealed": true, "seal_digest": "sha256:998bfa4c4fc8381bc137beeb807bf2439b0850cb87a22003293d28bbb6d4d0a3"}

$ uv run zhiwei eval run --suite factqa-v1 --mode offline --seal
{"suite": "factqa-v1", "mode": "offline", "executor": "evidence-sql-replay", "production_path": "FrozenSnapshotReplay->QueryReplayRef->EvidenceVerifier", "registered_units": 120, "terminal_units": 120, "status_counts": {"completed": 120}, "eval_run_id": "06de7f99-d50f-45ed-857f-c559587caa27", "organization_id": "4ecb9d0b-44b3-4147-a657-356fed382c20", "workspace_id": "c6d35c33-cc30-4690-a74b-2643b79e5574", "sealed": true, "seal_digest": "sha256:ad6cf7ce0d2a34d639d7561068dd9ba36fb50e34e1a73a1c614f3d3e70d44ed7"}

$ uv run zhiwei eval run --suite numeric-risk-v1 --mode offline --seal
{"suite": "numeric-risk-v1", "mode": "offline", "executor": "numeric-detector-pack", "production_path": "FrozenRiskSnapshot->NumericPatternDetector->Signal->RiskHypothesis->NegativeProbe(deterministic)->FalsificationResult", "registered_units": 22, "terminal_units": 22, "status_counts": {"completed": 22}, "eval_run_id": "f9f6db88-17cc-4966-9c4e-cb045006c299", "organization_id": "93fedd1a-0301-4c8e-a2cd-400b9ba34ea7", "workspace_id": "9f83f126-8ab2-4d8a-b45b-4046497b699d", "sealed": true, "seal_digest": "sha256:063339752ba57efd430cc8567e995821dc60b77dbf5c237b74adc974c7182b64"}

$ uv run zhiwei eval run --suite discover-blind-v1 --mode offline --seal
{"suite": "discover-blind-v1", "mode": "offline", "executor": "numeric-detector-pack", "production_path": "FrozenRiskSnapshot->NumericPatternDetector->Signal->RiskHypothesis->NegativeProbe(deterministic)->FalsificationResult", "registered_units": 5, "terminal_units": 5, "status_counts": {"completed": 5}, "eval_run_id": "1e4c809d-6f3a-4389-a78e-f167fc6e3078", "organization_id": "005364d0-ae17-4f11-ba0b-9f525a7e3f4a", "workspace_id": "77945db5-9631-4173-b331-184511ea0a4d", "sealed": true, "seal_digest": "sha256:c8781078cef006e36c4056274f32209b4f46fdc3f0bc76216a1b7152946792a0"}

$ uv run zhiwei eval run --suite ask-v1 --mode offline --seal
{"suite": "ask-v1", "mode": "offline", "executor": "agent-runtime", "production_path": "RunCommandService->AgentRunWorkflow->AskTaskGraph", "registered_units": 6, "terminal_units": 6, "status_counts": {"completed": 6}, "eval_run_id": "b0d13826-41b1-4e79-b012-3016b3fda1b9", "organization_id": "b1c671dc-7256-47f7-8927-5a80d956b293", "workspace_id": "8c63cc5c-2922-4b70-b8d9-81980e9f97b9", "sealed": true, "seal_digest": "sha256:2fb752aea584cc8f050552946fd10286ede0a8907bc8c84e8afe4f8efd9474ba"}
```

### 3.3 外部基准可用性（fail closed 如实）

```text
$ uv run zhiwei eval external-status --suite longmemeval-adapter --seal
{"suite": "longmemeval-adapter", "benchmark": "longmemeval", "external_status": "unavailable", "reasons": [{"code": "missing_file", "path": "evals/external/longmemeval/LICENSE", "detail": "数据许可文件缺失"}, {"code": "missing_file", "path": "evals/external/longmemeval/VERSION", "detail": "数据版本文件缺失"}, {"code": "missing_data_dir", "path": "evals/external/longmemeval/data", "detail": "数据目录不存在"}], "run_kind": "none", "claim": {"benchmark": "longmemeval", "claim_status": "planned/unavailable"}, "eval_run_id": "dd956df2-b728-4996-aa44-d4064adfa51d", "organization_id": "a8161569-3def-4a20-b24b-eaead58984e8", "workspace_id": "882ac737-e502-4217-9162-df40b465698c", "sealed": true, "seal_digest": "sha256:e70c36ac9b46f438fb33e5ca17bbba547b92a404f986c9f5424bd06819feb96b"}
（exit 0；unavailable → planned/unavailable sealed，机器可读原因在案）
```

密封汇总：13 个 sealed EvalRun（seal-empty / legacy-assets / runtime-contract-v1 /
knowledge×4 / enterprise-memory-v1 / factqa-v1 / numeric-risk-v1 / discover-blind-v1 /
ask-v1 / longmemeval-adapter external-status）。verbatim JSON 数组已固化在
`artifacts/gates/s9/sealed-runs.json`。

## 4. 系统级密封复核（步骤 4）

```text
$ ZHIWEI_DATABASE_URL=…zhiwei_app… uv run zhiwei eval verify --all-sealed
{"checked": 0, "verified": 0, "failures": []}
（exit 0——但 checked=0 是 app DSN 在 FORCE RLS 下看不到租户行的 vacuous 成功；
命令本身要求系统级 maintenance DSN，见 _verify_all_sealed_flow docstring。如实记录后换 DSN 复跑。）

$ ZHIWEI_DATABASE_URL=…zhiwei_migrator… uv run zhiwei eval verify --all-sealed
{"checked": 13, "verified": 13, "failures": []}
（exit 0）
```

**Gate 口径取第二次执行：checked=13 / verified=13 / failures=[]**。第一次执行未计入
Gate 结论（空集不构成证据），但如实留档。

## 5. Claim Registry seeding（步骤 5）

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
[seed] ✓ factqa-v1.accuracy -> offline_verified sha256:ad6cf7ce0d2a34d639d7561068dd9ba36fb50e34e1a73a1c614f3d3e70d44ed7
[seed] ✓ knowledge-doc-v1.retrieval -> offline_verified sha256:5533adb4289d3862408caea669a8e61f2538acbcf8cf002965c98588b2589132
[seed] ✓ knowledge-code-github-v1.retrieval -> offline_verified sha256:a83641a81f2300c03aad5f6d2326764c7783139e0a1361df57449e6f60e8f0f0
[seed] ✓ knowledge-cross-source-v1.retrieval -> offline_verified sha256:0cdbb15c242f1c332913637522bc7e03db3294f138da27e284a2ebc66be056b4
[seed] ✓ knowledge-acl-freshness-v1.retrieval -> offline_verified sha256:b235134bca330d2ff3a253ba9f123aaf32debd0420d0b8645085bd7257856cc1
[seed] ✓ enterprise-memory-v1.pass -> offline_verified sha256:998bfa4c4fc8381bc137beeb807bf2439b0850cb87a22003293d28bbb6d4d0a3
[seed] ✓ numeric-risk-v1.recall-d0 -> offline_verified sha256:063339752ba57efd430cc8567e995821dc60b77dbf5c237b74adc974c7182b64
[seed] ✓ discover-blind-v1.blind-pass -> offline_verified sha256:c8781078cef006e36c4056274f32209b4f46fdc3f0bc76216a1b7152946792a0
[seed] ✓ runtime-contract-v1.contract-pass -> offline_verified sha256:1bbb4768ce44a483a57e521cb703a92cd04799f83ed7650db5f4fb17e19c23aa
[seed] ✓ ask-v1.contract-pass -> offline_verified sha256:2fb752aea584cc8f050552946fd10286ede0a8907bc8c84e8afe4f8efd9474ba
[seed] ✓ longmemeval.external-diagnostic -> planned（外部基准不可用，不解锁质量 claim）
[seed] ✓ 11 条 claim 就绪
（exit 0；复跑 exit 0，幂等跳过——"[seed] • longmemeval.external-diagnostic 已存在（保持 planned）"）
```

绑定值明细（bound_value，均由密封 sample 聚合得出）：

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
`<!-- claims:end -->` 块内数字只以 `{{claim:ID}}` marker 出现（上表 10 条
offline_verified claim；planned 的 longmemeval 不入块——checker 对非 verified marker
一律出 finding）。块内不出现任何裸数字（suite 名与版本号含数字，一律不写入块内文本；
ISO 口径日期被 checker 剔除）。历史资产数字（120/112/57、Risk planted、`$43.0231552`）
未加入 README，维持 docs/BENCHMARK.md、docs/RISK_EVAL.md 既有窄口径标注原样（语料
内部边界，未扩写未删除）。无任何生产 SLO 表述。

## 7. strict release check 与 attestation dry-run（步骤 7）

```text
$ ZHIWEI_DATABASE_URL=…zhiwei_migrator… uv run zhiwei release check --strict
{"checked_files": 50, "findings": []}
（exit 0；registry 经 maintenance DSN 系统级读取——checker 必须看到全部租户的 claim）

$ ZHIWEI_DATABASE_URL=…zhiwei_migrator… uv run zhiwei release attest --dry-run
{"signed": false, "provenance": {"commit": "a8ff3ea7a4a0d751bcc88023024c2483c7368b83", "generated_at": "2026-09-05T18:36:11.947235+00:00", "generator": "zhiwei-release-check"}, "content_digests": {"README.md": "sha256:a3ca4a07…", "docs/API.md": …, "artifacts/gates/s9/sealed-runs.json": "sha256:0cb5b534863597c1dcc425fde6d242e0582047d0d934b423867c13f6bac783c6", …}}
（exit 0；完整 JSON 含 50 个表面文件 digest。dry-run 后 `git status --short` 仅含
本任务预期改动 M README.md / ?? deploy/seed_s9_gate_claims.py / ?? artifacts/gates/s9/
——未写任何 attestation 文件，验证通过）
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

## 10. Claim Registry 表（claim_id → status → seal_digest → scope）

| claim_id | status | seal_digest | mode | model | version | date | corpus | environment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| factqa-v1.accuracy | offline_verified | sha256:ad6cf7ce0d2a34d639d7561068dd9ba36fb50e34e1a73a1c614f3d3e70d44ed7 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | factqa-v1 | offline-fixture |
| knowledge-doc-v1.retrieval | offline_verified | sha256:5533adb4289d3862408caea669a8e61f2538acbcf8cf002965c98588b2589132 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | knowledge-doc-v1 | offline-fixture |
| knowledge-code-github-v1.retrieval | offline_verified | sha256:a83641a81f2300c03aad5f6d2326764c7783139e0a1361df57449e6f60e8f0f0 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | knowledge-code-github-v1 | offline-fixture |
| knowledge-cross-source-v1.retrieval | offline_verified | sha256:0cdbb15c242f1c332913637522bc7e03db3294f138da27e284a2ebc66be056b4 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | knowledge-cross-source-v1 | offline-fixture |
| knowledge-acl-freshness-v1.retrieval | offline_verified | sha256:b235134bca330d2ff3a253ba9f123aaf32debd0420d0b8645085bd7257856cc1 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | knowledge-acl-freshness-v1 | offline-fixture |
| enterprise-memory-v1.pass | offline_verified | sha256:998bfa4c4fc8381bc137beeb807bf2439b0850cb87a22003293d28bbb6d4d0a3 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | enterprise-memory-v1 | offline-fixture |
| numeric-risk-v1.recall-d0 | offline_verified | sha256:063339752ba57efd430cc8567e995821dc60b77dbf5c237b74adc974c7182b64 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | numeric-risk-v1 | offline-fixture |
| discover-blind-v1.blind-pass | offline_verified | sha256:c8781078cef006e36c4056274f32209b4f46fdc3f0bc76216a1b7152946792a0 | offline | reference-fixture | 0015_release_claims | 2026-09-05 | discover-blind-v1 | offline-fixture |
| runtime-contract-v1.contract-pass | offline_verified | sha256:1bbb4768ce44a483a57e521cb703a92cd04799f83ed7650db5f4fb17e19c23aa | offline | reference-fixture | 0015_release_claims | 2026-09-05 | runtime-contract-v1 | offline-fixture |
| ask-v1.contract-pass | offline_verified | sha256:2fb752aea584cc8f050552946fd10286ede0a8907bc8c84e8afe4f8efd9474ba | offline | reference-fixture | 0015_release_claims | 2026-09-05 | ask-v1 | offline-fixture |
| longmemeval.external-diagnostic | **planned** | （无——不可用性密封件 sha256:e70c36ac9b46f438fb33e5ca17bbba547b92a404f986c9f5424bd06819feb96b 是不可用证据，不解锁质量 claim） | — | — | — | — | longmemeval-adapter | — |

## 11. 例外/未执行项（ADR-012 登记）

| 阻塞项 | 根因 | 解锁条件 | 复执行时点 |
| --- | --- | --- | --- |
| live / shadow / human 模式 suite 全部未执行 | 不调用 live 模型是全仓纪律；live 只由 operator 显式触发（release_mode=fixture_only） | operator 显式 live 授权 + production_reference 档 | operator 触发后；live_verified claim 状态机已就绪 |
| BIRD / LoCoMo / Promptfoo / Inspect 外部诊断未执行、claim 保持 planned | 外部数据/许可未就绪（longmemeval 探测输出即同类机器可读原因）；本 Gate 按 runbook 仅探测 longmemeval-adapter | operator 放置许可/version/data 后 `eval external-status --seal` 走 available 分支 | 数据就位后 |
| LongMemEval 质量诊断 | 同上 + 质量诊断需 live 模型 | 同上 | 同上 |
| S4/S6/S7/S8 e2e 例外条目 | 各阶段既有登记（ADR-012 例外，条件四要素在案） | 既有登记的解锁条件 | 最迟并入 S10 Studio Gate 清单（operator 已确认的既有口径，本轮不变） |
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

## 13. 提交

```text
<commit a> feat(release): seal evidence-backed claim registry            — deploy/seed_s9_gate_claims.py
<commit b> docs(claims): bind README claim table to sealed artifacts (S9) — README.md
<commit c> test(release): S9 gate evidence and report                    — artifacts/gates/s9/**
```

（commit sha 见交付说明；报告与 sealed-runs.json 属 artifacts/ gitignore 范围，按
既有 gate artifact 惯例 `git add -f` 纳入。）
