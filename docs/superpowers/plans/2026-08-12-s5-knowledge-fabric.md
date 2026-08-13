# S5 Source-native Knowledge Fabric Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest, version, index and retrieve documents/tables, code/GitHub, PostgreSQL and API/MCP resources with source-native locators, ACL, temporal freshness and reconstructable provenance.

**Architecture:** PostgreSQL/ObjectStore Source Ledger is authoritative; OpenSearch and the PostgreSQL Context Graph are derived. The Knowledge Planner uses source-specific exact signals plus hybrid retrieval and returns Evidence candidates, not anonymous chunks.

**Tech Stack:** PostgreSQL, S3/Garage adapter, OpenSearch, SCIP, tree-sitter, GitHub App/Webhooks, SQLGlot/SQLAlchemy, BM25, pinned CPU BGE/RRF/reranker.

---

### Task 1: Model Source Ledger and synchronization

**Files:** Create `src/zhiwei/knowledge/{contracts.py,ledger.py,sync.py,watermarks.py,freshness.py}`,
`migrations/versions/0005_knowledge.py`, `tests/unit/knowledge/test_ledger.py`.

- [ ] Test immutable SourceVersion, observed/valid time, locator identity, watermark, parent/tombstone and stale transition.
- [ ] Implement tenant/ACL/classification fields and ObjectStore manifests; no raw content in index-only tables.
- [ ] Add sync intent/outbox, duplicate/out-of-order webhook handling and reconciliation contract.
- [ ] Suggested commit: `feat(knowledge): add source ledger and sync watermarks`.

### Task 2: Parse documents and tables with stable locators

**Files:** Create `src/zhiwei/knowledge/parsers/{documents.py,tables.py}`, `src/zhiwei/knowledge/connectors/files.py`,
`tests/fixtures/knowledge/documents/`, `tests/contract/knowledge/test_documents.py`.

- [ ] Test section/paragraph/table/row/cell/code-block hierarchy, Unicode spans, page/title path and deterministic rebuild.
- [ ] Implement supported Markdown/PDF text/XLSX/CSV paths with explicit unsupported/size failure.
- [ ] Verify Cell/Doc locator replay against immutable source digest.
- [ ] Suggested commit: `feat(knowledge): preserve document and table structure`.

### Task 3: Ingest code and GitHub sources

**Files:** Create `src/zhiwei/knowledge/connectors/github.py`, `src/zhiwei/knowledge/parsers/{scip.py,treesitter.py}`,
`tests/fixtures/knowledge/repos/`, `tests/contract/knowledge/test_code_github.py`.

- [ ] Create an owned/synthetic repo with commits, PRs, issues, reviews and checks; freeze expected symbols/refs/diffs.
- [ ] Test GitHub App least permission, webhook signature/duplicate/missing event/reconciliation/force-push/revoke.
- [ ] Implement Repository@Commit/File/Symbol/definition/ref/import/test and GitHub locators; SCIP first, explicit fallback.
- [ ] Verify repository permission revoke removes new-query access before freshness SLA.
- [ ] Suggested commit: `feat(knowledge): add code and github native indexing`.

### Task 4: Add PostgreSQL and API/MCP resource sources

**Files:** Create `src/zhiwei/knowledge/connectors/{postgres.py,api_resource.py}`,
`src/zhiwei/knowledge/query_sql.py`, tests under `tests/contract/knowledge/`.

- [ ] Test schema snapshot, SELECT/CTE AST, typed params, timeout/row/byte limits and read-only account enforcement.
- [ ] Test API/MCP observation is not Evidence until frozen in Source Ledger.
- [ ] Implement QueryResult canonicalization and snapshot/version boundary; semantic correctness remains scorer responsibility.
- [ ] Suggested commit: `feat(knowledge): add structured and api source snapshots`.

### Task 5: Implement OpenSearch indexes and Context Graph

**Files:** Create `src/zhiwei/knowledge/indexes/{opensearch.py,lexical.py,dense.py,fusion.py,rerank.py}`,
`src/zhiwei/knowledge/graph.py`, `deploy/compose/opensearch/`, integration tests.

- [ ] Test index determinism/version aliases, BM25/dense/RRF/rerank assembly and CPU model revision/cache.
- [ ] Test code exact symbol/path/ref signals outrank semantic-only candidates for exact queries.
- [ ] Implement typed temporal Context Graph edges with source refs; prove graph deletion/rebuild from Ledger.
- [ ] Kill/rebuild OpenSearch and alias-switch without changing Source Ledger.
- [ ] Suggested commit: `feat(knowledge): add reconstructable hybrid indexes`.

### Task 6: Implement Knowledge Planner and ACL enforcement

**Files:** Create `src/zhiwei/knowledge/{planner.py,query.py,acl.py}`, `src/zhiwei/policy/knowledge.py`,
`src/zhiwei/runtime/handlers/retrieve.py`, `src/zhiwei/workflows/activities/knowledge.py`,
`tests/security/knowledge_acl/`, `tests/integration/knowledge/test_planner.py`.

- [ ] Test typed query planning for doc/code/GitHub/DB/cross-source, score breakdown and evidence requirements.
- [ ] Test ACL pre-filter and hydration re-check with stale/unknown/revoked/cross-org states.
- [ ] Implement fail-close candidate generation and SourceVersion/Locator/freshness-rich results.
- [ ] Register Retrieve in S2 TaskHandlerRegistry. Execute planner/source/index I/O in a Temporal Activity and append
  typed candidate/artifact/failure events; cancellation and ACL revoke must produce formal task terminal state.
- [ ] Add an integration test that executes a real TaskGraph Retrieve node through AgentRuntime; direct planner-only tests
  do not satisfy the Gate.
- [ ] Suggested commit: `feat(knowledge): plan acl-safe source-native retrieval`.

### Task 7: Build Knowledge management Web journey

**Files:** Create `src/zhiwei/api/knowledge.py`, `src/zhiwei/cli/sources.py`,
`tests/contract/cli/test_source_cli.py`, `apps/web/src/features/knowledge/`, `apps/web/e2e/knowledge.spec.ts`.

- [ ] Implement add/connect/sync/status/version/ACL/disable and Builder debug query against real APIs.
- [ ] Display source version, locator, freshness, ACL and score breakdown, not anonymous chunks.
- [ ] Cover sync failure, permission loss, stale index and reconciliation states.
- [ ] Register `source sync|status`; test `--help`, unauthorized source and reference sync/reconcile smoke.
- [ ] Suggested commit: `feat(web): add source-native knowledge management`.

### Task 8: Create and seal four new Knowledge suites

**Files:** Create `evals/knowledge/`, `solution-packs/reference-knowledge/`, `tests/integration/knowledge/test_suites.py`.

- [ ] Freeze doc/table, code/GitHub, cross-source and ACL/freshness datasets with independence/source units.
- [ ] Add blind holdout and metamorphic rename/move/update/revoke variants; do not modify legacy 120 questions.
- [ ] Run the four commands in `specs/s5-knowledge-fabric.md`; record CPU latency/memory and sealed results.
- [ ] Run S5 Gate. Suggested commit: `test(knowledge): add source-native sealed suites`.
