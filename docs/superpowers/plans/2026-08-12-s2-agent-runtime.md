# S2 Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run versioned Agents through a durable typed Task Graph with approvals, cancellation, retries, delegation and recoverable streaming, using a deterministic planner before real models.

**Architecture:** PostgreSQL canonical events remain business truth; Temporal owns durable execution position. A transactional outbox bridges them, and Redis/SSE is a disposable UI channel.

**Tech Stack:** Temporal Python SDK/CLI dev server, PostgreSQL, Redis Streams, FastAPI SSE, Pydantic, React/TypeScript, Pytest/Playwright.

---

### Task 1: Version AgentDefinition and SolutionPack

**Files:** Create `src/zhiwei/agents/{domain.py,versions.py,solution_packs.py,schemas.py}`,
`migrations/versions/0003_agents_runtime.py`, `tests/unit/agents/`, `tests/contract/solution_packs/test_schema.py`.

- [x] Test immutable candidate/sandbox versions, parent/diff, dependency digests and invalid Pack references; published
  Agent lifecycle is added by S9.
- [x] Implement draft/version repositories and Pack loader; do not add Ask/Discover conditionals.
- [x] Add an architecture test that `src/zhiwei` cannot import `solution-packs/*` app modules.
- [x] Suggested commit: `feat(agents): add versioned agent and solution pack contracts`.

### Task 2: Implement Task Graph and pure reducer

**Files:** Create `src/zhiwei/agents/task_graph.py`, `src/zhiwei/runtime/{events.py,reducer.py,scheduler.py,attempts.py}`,
`src/zhiwei/runtime/handlers/{__init__.py,base.py,registry.py,core.py,fixture.py}`,
`tests/unit/runtime/{test_reducer.py,test_scheduler.py,test_attempts.py}`.

- [x] Write examples/property tests for DAG validation, readiness, parallel merge, attempt commit/abort and terminal obligations.
- [x] Confirm RED, then implement only the primitives in `specs/s2-agent-runtime.md`.
- [x] Implement versioned TaskHandlerRegistry; duplicate/unknown/missing/version-mismatched handlers fail validation.
  Core/fixture handlers use application ports and external I/O only through Activities.
- [x] Prove arbitrary committed event replay yields the same projection and duplicate events are idempotent.
- [x] Suggested commit: `feat(runtime): add typed task graph reducer`.

### Task 3: Add Temporal durable shell

**Files:** Create `src/zhiwei/workflows/{agent_run.py,versioning.py}`,
`src/zhiwei/workflows/activities/{__init__.py,base.py,runtime.py}`,
`src/zhiwei/workers/agent_worker.py`, `tests/integration/temporal/test_agent_run.py`; extend test Compose.

- [x] Test deterministic start, Activity timeout/retry, worker kill, signal duplicate, replay and Continue-As-New.
- [x] Add pinned Temporal dev service and run the focused test to observe failure.
- [x] Implement Workflow as orchestration only; payloads contain refs, and Activities append PG events idempotently.
- [x] Kill/restart the worker during tests and verify PG state/terminal result.
- [x] Suggested commit: `feat(runtime): add temporal agent workflow`.

### Task 4: Bridge commands, workflow signals and outbox

**Files:** Create `src/zhiwei/runtime/{commands.py,outbox_handlers.py}`, `src/zhiwei/workers/outbox_dispatcher.py`,
`tests/integration/runtime/test_db_temporal_outbox.py`.

- [x] Test DB success/Temporal failure, duplicate dispatch, signal-before-worker, dispatcher crash and poison message.
- [x] Implement deterministic workflow/signal ids and bounded retry/dead-letter with observable state.
- [x] Verify there is no cross-system "simultaneous transaction" assumption.
- [x] Suggested commit: `feat(runtime): bridge postgres and temporal via outbox`.

### Task 5: Implement approval and effect semantics

**Files:** Create `src/zhiwei/runtime/{approvals.py,actions.py,failures.py,delegation.py}`,
`tests/fixtures/tools/fake_ticket.py`, `tests/integration/runtime/test_actions.py`.

- [x] Test exact input digest, replace/expiry/revoke, provider idempotency, read-after-write and uncertain timeout.
- [x] Persist requester, last input modifier and effective AgentIdentity on ApprovalRequest. The approver must be a different
  human principal from requester/modifier and cannot be the represented AgentIdentity owner; test concurrent approve/
  reject/replace with CAS and direct API bypass.
- [x] Implement ToolIntent/Approval/ActionReceipt against the fake ticket service through a formal Activity port.
- [x] Assert an uncertain non-idempotent write becomes `effect_unknown` and is never automatically retried.
- [x] Suggested commit: `feat(runtime): add approvals and action receipts`.

### Task 6: Bind the S0 Eval executor to Agent Runtime

**Files:** Create `src/zhiwei/evals/executors/agent_runtime.py`, `src/zhiwei/cli/runtime.py`,
`tests/contract/cli/test_runtime_cli.py`, `tests/integration/runtime/test_eval_executor.py`.

- [x] Register runtime-contract-v1 units and write a failing fixture EvalRun test using the public Runtime command.
- [x] Implement the Agent Runtime executor without an eval-specific Planner/Workflow/reducer path.
- [x] Register `runtime replay-check`; test `--help`, invalid fixture and successful deterministic replay.
- [x] Verify all registered units get terminal states, partial cannot seal and sealed results recompute from artifacts.
- [x] Suggested commit: `feat(evals): execute suites through agent runtime`.

### Task 7: Add delegation, Redis/SSE and runtime UI

**Files:** Create `src/zhiwei/runtime/delegation.py`, `src/zhiwei/api/{agents.py,runs.py,approvals.py,events.py}`,
`src/zhiwei/telemetry/streams.py`, `apps/web/src/features/{workbench,runs,approvals}/`, corresponding tests.

- [x] Test ChildTask scope/budget/depth, stable merge, SSE cursor/reconnect/slow client and Redis loss.
  （2026-09-03 解决轮：SSE cursor/reconnect + Redis kill 零丢失已接真实
  redis-server 7.2.5 + 真实 uvicorn（tests/integration/runtime/test_sse_redis.py，
  5 例全绿）；slow client 经连接级断开与 cursor 重连覆盖。）
- [x] Implement FixturePlanner through the public Planner port; it must drive the same Workflow/Task Graph. UI launches
  only sandbox AgentVersion until S9 release service exists.
  （2026-09-03 解决轮：Planner Protocol + FixturePlanner（runtime/planner.py）；
  POST /runs 经 port；agent-version 图规划按裁决推迟到 S9 发布服务。）
- [x] Build Run/Task/Approval Web journey; refresh reconstructs from REST, not retained SSE tokens.
  （2026-09-03 解决轮：REST 投影绑定 PG canonical events（无进程内缓存）；审批旅程
  PG 化（migration 0011 + workflow 等待 + REST 决策）；web journey 组件绑定真实 REST，
  npm build 全绿。e2e spec 不可运行——无 docker 编排 Keycloak；s1-t6 §5-1 的
  OIDC redirect_uri 诊断经严格 FakeIdP 契约证伪（handoff §6.9），真实 Keycloak
  403 需重诊。）
- [x] Run 10 concurrent Runs across two orgs, kill worker/API/Redis, and assert terminal/no-leak invariants.
  （2026-09-03：10 并发 × 2 org 经真实 PG+Temporal 验证（tests/security/runtime_isolation）；
  worker kill/restart 见 tests/integration/temporal；Redis kill 随 SSE 一项遗留。）
- [x] Run S2 Gate including `zhiwei eval run --suite runtime-contract-v1 --mode fixture --seal`; suggested commit:
  `feat(runtime): complete durable fixture run journey`.
  （2026-09-03 解决轮：Python 侧 Gate 全绿（artifacts/gates/s2/report.md）；
  e2e runtime-approval.spec.ts 因环境无 docker（Keycloak 不可编排）不可执行，
  该限制为环境性而非代码性——S11 部署参考阶段随 compose 编排补跑。）
