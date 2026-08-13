# S10 Agent Studio and Third App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the Web product journeys and prove Core extensibility by shipping ChangeBrief entirely as a third SolutionPack/ViewManifest.

**Architecture:** Studio edits versioned resources through existing APIs and a constrained typed Task Graph. Generic Workbench panels consume Run contracts; each App contributes only schemas, pack runtime extensions and view renderers.

**Tech Stack:** React/TypeScript/Vite, query cache, accessible component primitives, FastAPI REST/SSE, Playwright, existing Agent Core.

---

### Task 1: Freeze frontend architecture and generic registries

**Files:** Create/modify `apps/web/src/{app,routes,api,state,components,renderers}/`,
`tests/architecture/test_app_boundaries.py`, `apps/web/src/renderers/registry.test.ts`.

- [ ] Test Core Python imports no concrete Pack and generic Web panels contain no App-name conditionals.
- [ ] Define data-driven App/ViewManifest registry, typed API client, server/live/draft state boundaries and SSE resync.
- [ ] Migrate existing Ask/Discover renderers to registry without changing behavior.
- [ ] Suggested commit: `refactor(web): enforce generic app renderer boundaries`.

### Task 2: Implement Agent Studio draft editing

**Files:** Create `apps/web/src/features/studio/`, extend `src/zhiwei/api/agents.py`,
`apps/web/e2e/studio-draft.spec.ts`.

- [ ] Write journey covering Overview/Instructions/Knowledge/Memory/Tools/Task/Triggers/Model/Budget/Evidence/Evals/Access.
- [ ] Implement ETag/CAS draft revisions, validation summaries and permission/loading/error/conflict states.
- [ ] Build constrained Task Graph editor for known primitives/typed ports; reject cycles/schema/capability/obligation errors.
- [ ] Suggested commit: `feat(studio): build versioned constrained agent editor`.

### Task 3: Integrate the S9 evaluate/review/stage/publish flow

**Files:** Create `apps/web/src/features/studio/release/`; modify only S9 API/application adapters as required;
create `apps/web/e2e/studio-release.spec.ts`.

- [ ] Test dependency/permission/budget/schema diff, missing Eval/Connection, reviewer separation and rollback pointer.
- [ ] Wire buttons to S9 explicit validate/evaluate/stage/publish commands; no PATCH lifecycle shortcut or duplicate
  frontend release state machine.
- [ ] Display immutable release manifest and failed Gate artifacts.
- [ ] Suggested commit: `feat(studio): add evidence-backed publish flow`.

### Task 4: Complete Knowledge, Capability, Memory and Admin journeys

**Files:** Modify `apps/web/src/features/{knowledge,capabilities,memory,organizations,workspaces}/`; create
`apps/web/src/features/{admin,audit,costs}/`, `apps/web/e2e/full-product.spec.ts`.

- [ ] Inventory every route/action against an implemented API command; remove/withhold controls without backend behavior.
- [ ] Add shared loading/empty/error/403/stale/CAS/reconnect patterns and accessible keyboard/focus states.
- [ ] Run five-role journey from login through build/use/approve/audit.
- [ ] Suggested commit: `feat(web): complete organization product journeys`.

### Task 5: Define ChangeBrief SolutionPack

**Files:** Create `solution-packs/change-brief/{pack.yaml,agent.yaml,task_graph.yaml,skills,schemas,views,evals}/`,
`tests/contract/solution_packs/test_change_brief.py`.

- [ ] Define GitHub commit/PR trigger and VerifiedBrief schema for symbols/dependencies/tests/issues/reviews/checks/risks/unknowns.
- [ ] Bind existing code Knowledge, Skill, Core primitives and CodeRef/GitHubRef Evidence only.
- [ ] Make the contract test fail if Pack requests unknown primitive or private Core import.
- [ ] Suggested commit: `feat(change-brief): define third solution pack`.

### Task 6: Implement ChangeBrief via public extension points

**Files:** Create `solution-packs/change-brief/runtime/{planner.py,synthesis.py}`, renderer under
`apps/web/src/renderers/changeBrief/`, `tests/integration/change_brief/`.

- [ ] Test commit/PR fixture through the same Trigger→Run→TaskGraph→Evidence→Artifact path.
- [ ] Implement planner/synthesis/renderer without DB/model/provider direct imports.
- [ ] Review diff: any Core change must be generic and exercised by Ask/Discover, otherwise revert/rework.
- [ ] Suggested commit: `feat(change-brief): ship app through core contracts`.

### Task 7: Seal architecture and product journey evidence

**Files:** Create `evals/change-brief/`, `tests/architecture/test_no_app_conditionals.py`, S10 report config.

- [ ] Run import/AST checks, Pack conformance, full role Playwright, accessibility/responsive smoke.
- [ ] Seal change-brief-v1 and record touched-file/import graph proving no app-specific Core branch.
- [ ] Run S10 Gate and link journey screenshots/video only as supplemental evidence.
- [ ] Suggested commit: `test(platform): prove third app extensibility`.
