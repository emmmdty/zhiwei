# S0 - Platform Foundation

> Status: frozen implementation specification  
> Depends on: existing repository assets only  
> Unlocks: S1

## 1. Goal

建立后续所有阶段共用的 Python package、配置、PostgreSQL transaction/outbox、ObjectStore manifest、
版本/digest 与最小 Eval core，同时保持现有冻结 eval 资产不变。阶段出口是一个真实持久化的 sealed
empty Run/EvalRun，不是假 Agent 对话。

## 2. User-visible result

- 开发者可用 `uv sync` 初始化，启动最小 test dependencies，执行 migration/health/asset checks。
- `zhiwei dev doctor` 显示 DB、object store、schema revision、release mode，且不探测/调用真实模型。
- 可创建 test Organization/Workspace、AgentDefinition draft 和空 Run/EvalRun，并从 artifact manifest 复算。

## 3. Required modules

```text
src/zhiwei/{contracts,config,persistence,object_store,telemetry,evals,api,cli}/
tests/{unit,contract,integration}/
deploy/compose/compose.test.yaml
```

必须提供：

- RFC 8785/JCS 语义的 canonical JSON、SHA-256、schema/version envelope、opaque id 与 UTC time helpers。
- SQLAlchemy async session、Alembic、application/migration roles、tenant context API。
- 基础表：organizations、workspaces、agent_definitions/versions、runs、canonical_events/projections、
  artifact_manifests、dataset_versions、eval_suite_versions、eval_runs/samples、outbox、audit_events、
  idempotency_records。
- POSIX test ObjectStore 与 S3 port；temporary→digest verify→immutable→manifest 协议。
- transactional outbox claim/retry/dead-letter contract；只实现 test sink，不提前接 Temporal/Redis。
- Settings 分 test/local-product/production-reference；secret 类型禁止 repr/log。
- 最小 Dataset/Suite/EvalRun/version、sample/unit registry、mode、partial/resume/seal 与 `evals/executors/`
  port；S0 只接 empty/legacy executor，S2 把同一 port 绑定真实 Agent Runtime。
- 现有 `evals/` validator/determinism 作为 legacy asset adapter 继续运行，不迁移/改写冻结 JSONL。

## 4. Invariants

- event/projection/outbox 同事务；projection 可从 event 重建。
- tenant repository 无 org context 时拒绝；RLS policy skeleton 默认 deny，S1 再接真实 principal/policy。
- artifact digest mismatch、missing object、manifest before object 全部不能 seal。
- schema/version 未知时 fail closed；migration 可从空库向前并完成一次 downgrade/upgrade smoke。
- CI/Compose 启动不读取 `.env` 内容、不调用模型、不下载 GPU asset。

## 5. Required tests

- canonical JSON/property：字段顺序、Unicode、decimal/float/time、schema mismatch。
- PG：transaction rollback、idempotency same/different payload、event sequence/CAS、projection rebuild、outbox retry。
- Object：partial upload、digest tamper、orphan、manifest missing、authorized namespace。
- migration：fresh upgrade、downgrade/upgrade、application role cannot own/bypass tenant tables。
- Eval：immutable Dataset/Suite、registered unit terminal completeness、partial cannot seal、sealed recompute。
- legacy assets：`make evals`、`make determinism`；运行前后 `git diff -- evals` 为空。

## 6. Gate

```bash
uv sync --extra dev --extra evals
make evals
make determinism
uv run ruff check .
uv run pyright
uv run pytest tests/unit tests/contract tests/integration/foundation -q
uv run alembic upgrade head
uv run zhiwei dev doctor --format json
uv run zhiwei eval seal-empty --check
```

Gate artifact 必含 code/schema/config digest、migration revision、Run/EvalRun/event/artifact manifest 和 test report。

## 7. Explicit non-goals

OIDC、真实用户、Temporal、LLM、Tools、Knowledge 和 Web UI 属后续阶段。不得用 test Organization 冒充
多租户已实现，不得将 JSONL 恢复为生产 canonical truth。
