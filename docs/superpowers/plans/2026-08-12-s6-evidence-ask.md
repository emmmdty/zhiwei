# S6 Evidence Contract and Ask App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver deterministic claim verification and the first complete cross-source Ask Agent App through public Agent Core contracts.

**Architecture:** Evidence refs point to immutable Source Ledger snapshots and canonical values. Ask is a SolutionPack using typed runtime primitives; its renderer is registered by ViewManifest and does not create a parallel runtime.

**Tech Stack:** Pydantic, PostgreSQL/ObjectStore, SQLGlot/SQLAlchemy, FastAPI/Typer, React, Pytest/Hypothesis/Playwright.

---

### Task 1: Implement canonical values and Evidence tagged union

**Files:** Create `src/zhiwei/evidence/{canonical_values.py,refs.py,locators.py}`,
`tests/unit/evidence/{test_values.py,test_refs.py}`.

- [ ] Add property/golden tests for integer/decimal/float bits/text NFC/bytes/datetime/null and ordered/multiset results.
- [ ] Add schema tests for QueryReplay/Cell/Doc/Code/GitHub/Api/Agent/Pattern refs and source/version ownership.
- [ ] Confirm RED, then implement immutable ref models and canonical result digests.
- [ ] Suggested commit: `feat(evidence): add canonical values and typed refs`.

### Task 2: Implement Claim binding and verifier

**Files:** Create `src/zhiwei/evidence/{claims.py,bundles.py,verifier.py,errors.py}`,
`src/zhiwei/runtime/handlers/verify.py`,
`src/zhiwei/cli/verify.py`, `tests/contract/cli/test_verify_cli.py`, `tests/contract/evidence/`.

- [ ] Test answer digest, Unicode code-point span, Fact/Quote value/ref and Inference non-deterministic status.
- [ ] Build valid fixtures, then independently tamper schema/source/snapshot/query/result/locator/value/span/digest.
- [ ] Implement layered verification and stable exit codes 0/2/3/4/5/6/7 from S6 spec.
- [ ] Register `verify evidence`; test `--help` plus every stable exit-code fixture.
- [ ] Register Verify in S2 TaskHandlerRegistry and prove a TaskGraph Verify node commits typed verification events.
- [ ] Assert hash does not mark semantic correctness/publisher identity verified.
- [ ] Suggested commit: `feat(evidence): verify claim-level bundles`.

### Task 3: Add Case aggregate and cross-App sharing

**Files:** Create `src/zhiwei/cases/{domain.py,commands.py,repositories.py}`, `migrations/versions/0006_cases_evidence.py`,
`tests/unit/cases/`, `tests/security/cases/test_sharing.py`.

- [ ] Test Case membership, selected Evidence/Artifact/Decision sharing, resolution and no transcript implicit sharing.
- [ ] Implement append-only Case timeline and tenant/ACL policies.
- [ ] Verify source access is rechecked when opening shared Evidence.
- [ ] Suggested commit: `feat(cases): add explicit cross-app collaboration`.

### Task 4: Define Ask SolutionPack contracts

**Files:** Create `solution-packs/ask/{pack.yaml,agent.yaml,task_graph.yaml}`, directories `skills/`, `schemas/`, `views/`,
`evals/`; create `tests/contract/solution_packs/test_ask.py`.

- [ ] Write failing Pack conformance for AskTaskSpec, Finding and Answer schemas.
- [ ] Define Task Graph template using only Core primitives and required doc/code/GitHub/DB capabilities.
- [ ] Encode Fact/Quote Evidence Gate, conflict, clarification, partial/abstain and completion obligations.
- [ ] Verify Core source contains no `ask` condition with architecture tests.
- [ ] Suggested commit: `feat(ask): define cross-source solution pack`.

### Task 5: Implement Ask planner and renderer extension points

**Files:** Create `solution-packs/ask/runtime/{planner.py,synthesis.py}`, `src/zhiwei/agents/views.py`,
`tests/integration/ask/test_runtime.py`.

- [ ] Test simple, multi-hop, conflict, clarify, unanswerable and partial tasks against fixture model responses.
- [ ] Implement Planner/Synthesizer through public Pack plugin ports; no direct DB/provider access.
- [ ] Ensure Finding and final answer include source refs and unresolved obligations.
- [ ] Suggested commit: `feat(ask): execute evidence-bearing research tasks`.

### Task 6: Build Ask Workbench and Evidence explorer

**Files:** Create `src/zhiwei/api/{cases.py,evidence.py}`, `apps/web/src/features/{ask,evidence,cases}/`,
`apps/web/e2e/ask-evidence.spec.ts`.

- [ ] Write Playwright task: ask cross-source question, inspect Claim locator, view Context/Tool/Cost, tamper local bundle, create Case.
- [ ] Implement generic panels plus Ask renderer registered by manifest; label Fact/Quote/Inference/Recommendation.
- [ ] Cover stale/unauthorized source, partial, abstain, fixture/replay/live and reconnect.
- [ ] Suggested commit: `feat(web): deliver ask evidence workbench`.

### Task 7: Integrate legacy FactQA and new Ask evaluation

**Files:** Create `src/zhiwei/evals/adapters/factqa.py`, `evals/ask/`, `tests/integration/ask/test_eval.py`.

- [ ] Preserve legacy asset checksums; adapt 120 questions only through public Dataset loader.
- [ ] Freeze ask-v1 author-visible/blind cross-source tasks and deterministic Evidence/abstain scorers.
- [ ] Calibrate human rubric for inference/utility separately; save order/blinding/agreement metadata.
- [ ] Run factqa fixture and ask offline suites, tamper matrix, browser journey and full S6 Gate.
- [ ] Suggested commit: `test(ask): seal factqa and cross-source ask evaluations`.
