# S0 Platform Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared package, PostgreSQL transaction/outbox, artifact protocol, minimal Eval core, configuration and test foundation while preserving all frozen eval assets.

**Architecture:** PostgreSQL owns business state, a content-addressed object port owns large artifacts, and application services write state plus outbox atomically. No model, identity provider or Temporal behavior is introduced in S0.

**Tech Stack:** Python 3.11+, uv, Pydantic v2, SQLAlchemy 2 async, Alembic, PostgreSQL, Typer, Pytest/Hypothesis, Docker Compose.

---

## Preconditions

- Read `specs/s0-foundation.md`, `docs/DATA_MODEL.md`, `docs/CONVENTIONS.md`.
- Run `git status --short`; preserve all existing untracked assets. Do not commit until the owner establishes a baseline.
- Run `make evals && make determinism`; expected: 110 validations and byte-identical assets.

### Task 1: Normalize the package and quality configuration

**Files:** Modify `pyproject.toml`, `Makefile`; create `src/zhiwei/__init__.py`, `src/zhiwei/contracts/__init__.py`,
`src/zhiwei/config/settings.py`, `src/zhiwei/cli/{main.py,dev.py,db.py}`, `tests/unit/config/test_settings.py`,
`tests/contract/cli/{test_dev_cli.py,test_db_cli.py}`.

- [x] Write failing tests for environment profile parsing, secret-safe repr and “no live provider by default”.
- [x] Run `uv run pytest tests/unit/config/test_settings.py -q`; expect failure because modules do not exist.
- [x] Add runtime/dev dependencies and the `zhiwei = "zhiwei.cli.main:app"` entry point; remove the obsolete gateway target.
- [x] Update package description/keywords from the old FactQA/Risk demo to Enterprise Agent Core; keep version and
  Apache-2.0 metadata unless a release decision explicitly changes them.
- [x] Implement typed settings and register `dev doctor --format json`; test `--help`, invalid profile and fixture smoke.
  Do not load arbitrary `.env` in tests.
- [x] Register `db migrate|check` as application wrappers over Alembic/schema inspection; test `--help` and no-DB failure.
- [x] Run the focused test, `uv run ruff check src tests`, and `uv run pyright`; expect all green.
- [x] Suggested commit after baseline: `chore(foundation): establish package and settings`.

### Task 2: Implement canonical contracts

**Files:** Create `src/zhiwei/contracts/{canonical.py,envelope.py,identifiers.py,time.py}`, `tests/unit/contracts/`.

- [x] Add examples/property tests for field order, Unicode NFC, Decimal/float bits, UTC time, schema version and digest.
- [x] Run `uv run pytest tests/unit/contracts -q`; expect import/behavior failures.
- [x] Implement canonical JSON using `rfc8785`, typed envelopes and opaque UUID/ULID identifiers without global state.
- [x] Run `uv run pytest tests/unit/contracts -q`; expect green including Hypothesis cases.
- [x] Suggested commit: `feat(contracts): add canonical envelopes and digests`.

### Task 3: Add PostgreSQL schema, roles and tenant-scoped transactions

**Files:** Create `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_foundation.py`,
`src/zhiwei/persistence/{database.py,models.py,tenant.py,repositories.py}`, `tests/integration/foundation/test_database.py`.

- [x] Write tests for fresh migration, app-role ownership, missing tenant context, transaction rollback and idempotency conflict.
- [x] Add PostgreSQL to `deploy/compose/compose.test.yaml` with pinned image/healthcheck and test-only credentials.
- [x] Run `docker compose -f deploy/compose/compose.test.yaml up -d postgres` then the focused test; expect RED.
- [x] Implement Organization/Workspace/AgentVersion/Run/Event/Projection/DatasetVersion/EvalSuiteVersion/EvalRun/
  EvalSample/Idempotency/Audit/Outbox tables and default-deny RLS skeleton.
- [x] Run `uv run alembic upgrade head` and focused tests; verify app role is not owner/BYPASSRLS.
- [x] Suggested commit: `feat(persistence): add transactional tenant foundation`.

### Task 4: Implement canonical event and transactional outbox

**Files:** Create `src/zhiwei/persistence/{events.py,outbox.py,unit_of_work.py}`, `tests/unit/persistence/test_events.py`,
`tests/integration/foundation/test_outbox.py`.

- [x] Test sequence CAS/advisory locking, digest chain, duplicate idempotency, projection rebuild, outbox retry/dead-letter.
- [x] Confirm RED with `uv run pytest tests/unit/persistence tests/integration/foundation/test_outbox.py -q`.
- [x] Implement one transaction that appends event, updates projection and emits outbox/audit rows.
- [x] Run focused tests, including concurrent append workers; expect stable ordering and no missing outbox.
- [x] Suggested commit: `feat(runtime): add canonical event and outbox transaction`.

### Task 5: Implement the artifact manifest protocol

**Files:** Create `src/zhiwei/object_store/{ports.py,posix.py,manifests.py,service.py}`,
`tests/contract/object_store/test_posix.py`, `tests/integration/foundation/test_artifacts.py`.

- [x] Test temporary upload, read-back digest, immutable collision, missing/corrupt object, DB failure orphan and tenant namespace.
- [x] Run focused tests and confirm RED.
- [x] Implement POSIX adapter and `temporary → verify → immutable key → PG manifest` service; add safe orphan reconciliation.
- [x] Run tests and verify a committed manifest can be sealed while corrupt/missing artifacts cannot.
- [x] Suggested commit: `feat(artifacts): add content-addressed manifest protocol`.

### Task 6: Implement minimal Eval core and preserve legacy assets

**Files:** Create `src/zhiwei/evals/{domain.py,datasets.py,suites.py,runs.py,legacy_assets.py,sealing.py}`,
`src/zhiwei/evals/executors/{__init__.py,base.py,empty.py,legacy.py}`, `src/zhiwei/cli/{assets.py,evals.py}`,
`tests/contract/cli/{test_assets_cli.py,test_eval_cli.py}`, `tests/unit/evals/test_domain.py`,
`tests/integration/foundation/test_empty_run.py`; modify `Makefile` additively.

- [x] Snapshot `git diff -- evals` (or checksums if no baseline), then test immutable Dataset/Suite versions, registered
  sample/unit terminal states, partial/resume and seal refusal.
- [x] Implement executor port and empty/legacy executors plus a sealed empty Run/EvalRun containing code/config/schema
  digests; S2 will bind this same port to Agent Runtime.
- [x] Register `eval seal-empty|run|resume|seal` commands and test `--help`, invalid mode and empty/legacy smoke.
- [x] Register `assets lock --check|--write`; `--check` is the normal Gate and `--write` requires explicit operator intent.
- [x] Run `uv run zhiwei eval seal-empty --check` and focused tests; expect independently verified manifests.
- [x] Run `make evals && make determinism`; assert no unintended asset drift.
- [x] Run full S0 Gate from `specs/s0-foundation.md` and save report under `artifacts/gates/s0/` (gitignored unless release-safe).
- [x] Suggested commit: `feat(foundation): add reproducible eval core`.
