# S3 Models and Canonical Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three model wire protocols and a verifiable Context Compiler that projects canonical state, compresses only allowed content and binds the actual serialized request.

**Architecture:** Provider-neutral model contracts sit behind thin transports. Canonical state is reduced from PostgreSQL events; each Attempt gets a ContextManifest, and explicit model changes use a TransitionManifest.

**Tech Stack:** Pydantic, OpenAI/Anthropic SDKs or HTTPX at adapter edges, token estimators, PostgreSQL/ObjectStore, Pytest/Hypothesis.

---

### Task 1: Freeze Model/Endpoint/Profile/Attestation schemas

**Files:** Create `src/zhiwei/models/{contracts.py,profiles.py,attestations.py}`, `config/models/`,
`tests/unit/models/test_profiles.py`, `tests/contract/models/test_attestations.py`.

- [ ] Test unknown capability, source/profile digest, attestation expiry and no profile mutation.
- [ ] Implement profile loaders that migrate current OpenCode Go YAML without claiming runtime support.
- [ ] Cross-check `required_capabilities_by_suite`; missing required fields fail before scheduling.
- [ ] Suggested commit: `feat(models): add versioned profiles and attestations`.

### Task 2: Implement provider-neutral contracts and three transports

**Files:** Create `src/zhiwei/models/transports/{base.py,openai_chat.py,openai_responses.py,anthropic_messages.py}`,
`tests/fixtures/models/{chat,responses,anthropic}/`, `tests/contract/models/test_transports.py`.

- [ ] Add golden fixtures for request, stream, tool call/result, usage and terminal response for every transport.
- [ ] Add malformed stream/JSON args, schema refusal, 429/5xx, timeout and cancellation cases; confirm RED.
- [ ] Implement the smallest adapters that normalize the fixtures; keep provider-specific types inside the package.
- [ ] Run contract tests without network. Suggested commit: `feat(models): add three wire transports`.

### Task 3: Implement canonical context types and reducer

**Files:** Create `src/zhiwei/context/{types.py,state.py,reducer.py,inventory.py}`,
`tests/unit/context/{test_reducer.py,test_inventory.py}`.

- [ ] Test authoritative/conversational/recoverable/opaque classification and complete authoritative inventory.
- [ ] Add property tests for event replay, conflict/entity/approval/evidence updates and terminal opaque deletion.
- [ ] Implement pure reducers and source refs; never persist hidden reasoning body.
- [ ] Scan PG/Object/Temporal/log test captures for sentinel reasoning text.
- [ ] Suggested commit: `feat(context): reduce canonical state inventory`.

### Task 4: Implement budget, compression and Context IR

**Files:** Create `src/zhiwei/context/{budget.py,compression.py,compiler.py,ir.py}`,
`tests/unit/context/{test_budget.py,test_compiler.py}`.

- [ ] Test fixed priority: artifactize result → remove recoverable → summarize conversation → task split/model choice → refusal.
- [ ] Test policy/data classification and completion obligation reservation before model choice.
- [ ] Implement deterministic ContextIR with source/transform map and token estimate confidence.
- [ ] Prove authoritative omission always returns `context_refusal`, never a request.
- [ ] Suggested commit: `feat(context): compile budgeted provider-neutral context`.

### Task 5: Bind the actual wire body

**Files:** Create `src/zhiwei/context/manifests.py`, `src/zhiwei/models/presend.py`,
`src/zhiwei/evidence/context_verify.py`, `src/zhiwei/cli/context.py`,
`tests/contract/cli/test_context_cli.py`, `tests/integration/context/test_wire_binding.py`.

- [ ] Write tamper tests for IR, serialized body, source inventory, redaction, target profile and send-after-capture mutation.
- [ ] Implement transport serialization callback/pre-send hook that captures normalized semantic bytes immediately before send.
- [ ] Persist scrubbed body privately in ObjectStore and manifest digest in PG; never persist auth headers.
- [ ] Implement `zhiwei verify context`; run valid/tampered fixtures and assert stable errors.
- [ ] Register `verify context` in `cli/main.py`; test `--help`, missing/tampered/valid manifests.
- [ ] Suggested commit: `feat(context): bind manifests to actual model wire`.

### Task 6: Implement transitions and Router

**Files:** Create `src/zhiwei/context/transition.py`, `src/zhiwei/models/{router.py,usage.py}`,
`src/zhiwei/runtime/handlers/model_actions.py`, `src/zhiwei/workflows/activities/model.py`,
`tests/integration/context/test_handoff.py`, `tests/unit/models/test_router.py`.

- [ ] Test two-phase epoch transition, old epoch preservation, A-prefix exactly once and direction-specific identity.
- [ ] Test Router order compliance→capability→context→quality→budget→latency and no silent fallback.
- [ ] Implement transition/route decisions as canonical events with reason and cost reservation.
- [ ] Register formal Plan/Analyze/Synthesize handlers in the S2 registry; execute model I/O through Activity and commit
  typed results/failures through Attempt events.
- [ ] Run fixture handoff pilot; label it pilot and keep structure vs quality metrics separate.
- [ ] Suggested commit: `feat(context): add explicit handoff and governed routing`.

### Task 7: Qualify fixtures and optionally one live endpoint

**Files:** Create `src/zhiwei/models/probes.py`, `src/zhiwei/cli/models.py`,
`tests/contract/cli/test_models_cli.py`, `tests/integration/models/test_fixture_attestation.py`;
modify capability status UI in `apps/web/src/features/runs/`.

- [ ] Run all fixture attestations and S3 offline Gate; seal profile/transport/context artifacts.
- [ ] Register `models attest`; test `--help`, fixture mode and live preflight refusal with no network.
- [ ] Verify UI distinguishes declared/fixture/transport/task qualification and shows fallback/transition.
- [ ] Only after operator preflight, run the exact live command in `specs/s3-models-context.md`; never run from CI.
- [ ] Do not generalize one probe to provider/package support. Suggested commit: `feat(models): expose evidence-backed qualification`.
