# S8 Discover and Governed Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the Discover Agent App from governed triggers and source watermarks through evidence/falsification, human triage, Case, approved action and resolution.

**Architecture:** DiscoveryProgram is a versioned SolutionPack configuration. Temporal schedules the pipeline, the Source Ledger freezes inputs, typed Signal/Hypothesis/Resolution records prevent semantic collapse, and existing ActionReceipt closes side effects.

**Tech Stack:** Temporal schedules/signals, PostgreSQL, existing Knowledge/Evidence/Memory/Capability Core, NumPy/SciPy deterministic scorers, React/Playwright.

---

### Task 1: Model DiscoveryProgram and triggers

**Files:** Create `solution-packs/discover/{pack.yaml,agent.yaml,task_graph.yaml,schemas/}`, `src/zhiwei/runtime/triggers/{schedule.py,webhook.py,source_delta.py}`,
`migrations/versions/0008_discover.py`, tests.

- [ ] Test immutable charter/source/entity/exclusion/detector/evidence/recipient/budget/action configuration.
- [ ] Test schedule/webhook/delta duplicate/out-of-order/watermark and activate/deactivate/version change.
- [ ] Implement ServiceAccount trigger identity; assert no creator session/token/personal memory propagation.
- [ ] Suggested commit: `feat(discover): add governed programs and triggers`.

### Task 2: Separate Signal, Hypothesis and Resolution

**Files:** Create `src/zhiwei/cases/{signals.py,hypotheses.py,resolutions.py,risk_fingerprint.py}`,
`tests/unit/discover/test_lifecycle.py`.

- [ ] Test immutable linked records, supporting/contradicting/missing evidence, ownership/status and source watermark.
- [ ] Test deterministic fingerprint/dedupe/reopen/version and semantic merge as proposal only.
- [ ] Implement human triage states without overwriting detector output; heuristic score is not probability.
- [ ] Suggested commit: `feat(discover): model auditable hypothesis lifecycle`.

### Task 3: Migrate Numeric Risk Detector Pack

**Files:** Create `solution-packs/discover/detectors/numeric/`, `src/zhiwei/evidence/patterns/`,
`src/zhiwei/cli/risk.py`, `evals/risk/v2/`, `tests/contract/cli/test_risk_cli.py`,
`tests/unit/discover/test_numeric_detectors.py`.

- [ ] First capture current single-seed assets as legacy input tests; do not edit them.
- [ ] Implement versioned formulas and independent realized-SNR scorer for six kinds.
- [ ] Generate planned 10-seed clean/planted pairs; test plantability/ghost/counterfactual/distractor/dirtiness.
- [ ] Implement benchmark-only deterministic one-to-one matching and per-kind PatternRef verification.
- [ ] Register `risk generate|verify`; test `--help`, legacy migration input, deterministic generation and tampered suite.
- [ ] Suggested commit: `feat(discover): add independently scored numeric detector pack`.

### Task 4: Implement change-driven and controlled exploration paths

**Files:** Create `solution-packs/discover/runtime/{change_detector.py,exploration.py,falsification.py}`,
`tests/integration/discover/test_detection_paths.py`.

- [ ] Freeze typed AnalysisSpec and allowed analysis tool; test invalid/free-form DB/script attempts fail.
- [ ] Test source diff→Signal and mandatory falsification/contradicting evidence before publish.
- [ ] Implement both paths through public Core tasks; no direct source/model/tool access.
- [ ] Suggested commit: `feat(discover): add change and controlled exploration paths`.

### Task 5: Connect triage, Case, action and memory candidate

**Files:** Create `src/zhiwei/cases/actions.py`, `solution-packs/discover/runtime/workflow.py`,
`tests/integration/discover/test_case_action.py`.

- [ ] Test Hypothesis→Case, Ask child task evidence, approval digest, duplicate action, effect_unknown and Resolution.
- [ ] Implement selected Evidence sharing, not transcript sharing; use S2/S4 ActionReceipt unchanged.
- [ ] Write lesson only as team/case Memory candidate; require Steward confirmation.
- [ ] Suggested commit: `feat(discover): close case and governed action loop`.

### Task 6: Build Discover and Case UI

**Files:** Create `src/zhiwei/api/discover.py`, `apps/web/src/features/{discover,cases,actions}/`,
`apps/web/e2e/discover-case-action.spec.ts`.

- [ ] Write end-to-end source-delta→hypothesis→triage→Ask evidence→approval→receipt→resolution journey.
- [ ] Display support/contradiction/freshness/dedupe/owner/status and separate detector vs human resolution.
- [ ] Cover insufficient data, duplicate, stale/ACL denial, retry/reconcile and effect_unknown.
- [ ] Suggested commit: `feat(web): deliver discover case action workflow`.

### Task 7: Evaluate detector, discovery and utility separately

**Files:** Create `evals/discover/`, `src/zhiwei/evals/scorers/{__init__.py,base.py,discover.py}`, human review
protocol/docs.

- [ ] Seal numeric-risk-v1 with recall/precision/distractor/Evidence per seed/difficulty.
- [ ] Freeze author-hidden change-driven holdout and metamorphic/fault cases.
- [ ] Run blinded human relevance/actionability/false-positive-burden protocol; do not invent probability/ECE at small n.
- [ ] Report D0-D6 separately and run S8 Gate. Suggested commit: `test(discover): seal layered discovery evaluations`.
