# Phase 1 Spec Document Audit Report

**Date:** 2026-09-04
**Audit Scope:** S0–S8 spec documents, ADRs, master design
**Method:** 3 rounds independent + 3 rounds cross-validation (9 sub-agents total)

---

## Executive Summary

The S0–S8 spec architecture is **structurally sound** — dependency chains are correct, ADRs are properly cross-referenced, and core invariants are declared. One **critical dependency issue** (httpx→httpx2 migration) blocks S3 implementation. Several spec gaps exist around undefined interfaces and missing type schemas.

| Category | Count | Critical | High | Medium | Low |
|----------|-------|----------|------|--------|-----|
| Confirmed Issues | 12 | 2 | 5 | 4 | 1 |
| False Positives (Retracted) | 3 | — | — | — | — |
| Newly Discovered | 5 | 0 | 2 | 2 | 1 |

---

## Critical Issues (P0)

### C-1: httpx→httpx2 Migration Required
- **Status:** CONFIRMED by 3 auditors (1.2C, 1.3A, 1.3B) + web search verification
- **Evidence:** openai-python v3.0.0 (2026-08-12) and anthropic-python v1.0.0 (2026-08-20) both migrated from `httpx` to `httpx2`. Current `pyproject.toml:24` pins `httpx>=0.28.1` (legacy). All transport code (`presend.py`, `base.py`, `openai_chat.py`, `anthropic_messages.py`, `policy/client.py`) imports from `httpx`.
- **Impact:** S3 cannot begin implementation. Wire capture, OPA client, and all provider transports are broken.
- **Fix:** Update `pyproject.toml`: `httpx>=0.28.1` → `httpx2>=2.5.0`. Update all imports: `import httpx` → `import httpx2 as httpx`. Update `pytest-httpx` → httpx2-compatible mock. Update `anthropic>=0.70.0,<1.0.0` → `>=1.0.0,<2.0.0`.

### C-2: anthropic Version Upper Bound Blocks v1.0
- **Status:** CONFIRMED by 2 auditors (1.2C, 1.3A)
- **Evidence:** `pyproject.toml:26` pins `anthropic>=0.70.0,<1.0.0`. anthropic v1.0.0 (2026-08-20) is the httpx2-compatible version but is blocked by the `<1.0.0` upper bound.
- **Impact:** Cannot upgrade to httpx2-compatible anthropic SDK.
- **Fix:** Update to `anthropic>=1.0.0,<2.0.0`.

---

## High Issues (P1)

### H-1: Missing Dependencies
- **Status:** CONFIRMED by 2 auditors (1.2C, 1.3A)
- **Evidence:** `pyproject.toml` lacks: `tiktoken` (ADR-002 token counting), `mcp` (S4 MCP client/server), `datasketch` (ADR-009 MinHash/LSH).
- **Fix:** Add to `[project.optional-dependencies]` or main dependencies.

### H-2: S3↔S7 MemoryPort Circular Interface
- **Status:** CONFIRMED by 1.3B implementer audit
- **Evidence:** S3 Context Compiler needs to allocate memory budget; S7 Memory needs to know what the compiler expects. Neither spec defines the `MemoryPort` interface.
- **Fix:** Define `MemoryPort` Protocol in S3 spec before S7 implementation.

### H-3: Case State Machine Not Formalized
- **Status:** CONFIRMED by 1.3A cross-audit
- **Evidence:** Unlike Run (S2 with explicit states), Case has no formal state machine. S6 and S8 reference Case but lifecycle is implicit.
- **Fix:** Add explicit Case state machine to S6 or a shared contracts section.

### H-4: Missing Spec Type Definitions
- **Status:** CONFIRMED by 1.3B
- **Evidence:** Multiple types referenced but undefined: `TaskGraphPatch` (S2), `ContextSlice` (S2/S3), `NegativeProbe` structure (S8), Evidence bundle serialization format (S6).
- **Fix:** Add formal type definitions to relevant specs.

### H-5: ADR-002/004/007 Missing Integration Tests
- **Status:** CONFIRMED by 1.1B
- **Evidence:** ADR-002 (context_refusal mapping), ADR-004 (full falsification pipeline), ADR-007 (refusal→recovery path) have unit tests but no integration-level tests.
- **Fix:** Add integration test directories and test files.

---

## Medium Issues (M-1 to M-4)

### M-1: Missing Test Directories (Spec-Required)
- `tests/security/model_egress/` (S3), `tests/security/evidence_access/` (S6), `tests/security/memory/` (S7), `tests/security/discover_identity/` (S8), `tests/integration/ask/` (S6), `tests/integration/discover/` (S8), `tests/integration/memory/` (S7)

### M-2: Missing E2E Playwright Specs
- `apps/web/e2e/runtime-approval.spec.ts` (S2 — **explicitly blocks Gate per ADR-012**), plus 4 others for S4/S6/S7/S8.

### M-3: hypothesis Property Tests Not Used
- `hypothesis` is in dev dependencies but zero property-based tests exist. S2 §6 explicitly requires reducer property tests.

### M-4: pytest-asyncio Version ancient
- `pyproject.toml:57` pins `pytest-asyncio>=1.2.0` (current is 0.24+). May cause compatibility issues.

---

## Low Issues (L-1)

### L-1: OAuth 2.1 Is Internet-Draft
- OAuth 2.1 is referenced as a released standard but is actually draft-ietf-oauth-v2-1-16. Consistent with MCP's own reference — not a blocker.

---

## False Positives (Retracted)

| Finding | Auditor | Reason for Retraction |
|---------|---------|----------------------|
| Session refresh not wired | 1.2A | Implementation exists in `identity/sessions.py:622-734` with lease fencing, CAS, IdP refresh. |
| Redis event_stream not wired | 1.2A | Wiring exists in `app.py:228-256`, conditional on `settings.redis_url`. |
| ContextEpoch undefined | 1.2A | Well-specified in S3 §3, S4 §4, ADR-007. |

---

## Newly Discovered Issues

| Issue | Priority | Description |
|-------|----------|-------------|
| Case state machine gap | P3 | Cross-App Case collaboration needs unified lifecycle spec |
| pytest-httpx conflict with httpx2 | P2 | `pytest-httpx>=0.36.2` may not support httpx2 |
| ContextCompiler↔Memory circular dependency | P1 | Port interface must be defined before either stage |
| SecretBackend Vault adapter contract undefined | P2 | S4 references vault.py but no formal adapter spec |
| DiscoveryProgram trigger→Runtime integration undefined | P1 | S8 triggers must go through S2 Runtime per architecture |

---

## Fix Plan

### Code Fixes (pyproject.toml)

| Fix | Files | Priority |
|-----|-------|----------|
| httpx→httpx2 migration | `pyproject.toml`, all `import httpx` in src/ | P0 |
| anthropic>=1.0.0 | `pyproject.toml` | P0 |
| Add tiktoken, mcp, datasketch | `pyproject.toml` | P1 |
| Update pytest-asyncio | `pyproject.toml` | P2 |
| Replace pytest-httpx | `pyproject.toml` | P2 |

### Spec Fixes

| Fix | Files | Priority |
|-----|-------|----------|
| Define MemoryPort Protocol | `specs/s3-models-context.md` | P1 |
| Add Case state machine | `specs/s6-evidence-ask.md` | P3 |
| Define ContextSlice schema | `specs/s2-agent-runtime.md` | P1 |
| Define TaskGraphPatch type | `specs/s2-agent-runtime.md` | P2 |
| Define NegativeProbe model | `specs/s8-discover-actions.md` | P2 |
| Define Evidence bundle format | `specs/s6-evidence-ask.md` | P2 |
| Document trigger→Runtime integration | `specs/s8-discover-actions.md` | P1 |

---

## Verification Status

| Round | Independence | Sub-agents | Status |
|-------|-------------|------------|--------|
| 1.1 Independent | ✅ No cross-reading | 3 (1.1A, 1.1B, 1.1C) | COMPLETE |
| 1.2 Independent | ✅ No 1.1 access | 3 (1.2A, 1.2B, 1.2C) | COMPLETE |
| 1.3 Cross-validation | ✅ Reads 1.1+1.2 | 3 (1.3A, 1.3B, 1.3C) | COMPLETE |
| 1.4 Fix & Verify | Pending | — | PENDING |

**Cross-validation confirmations:**
- httpx→httpx2: confirmed by 1.2C, 1.3A, 1.3B (3 auditors)
- anthropic bound: confirmed by 1.2C, 1.3A (2 auditors)
- Missing deps: confirmed by 1.2C, 1.3A (2 auditors)
- Session refresh false positive: retracted by 1.3A after code verification
- Redis event_stream false positive: retracted by 1.3A after code verification
