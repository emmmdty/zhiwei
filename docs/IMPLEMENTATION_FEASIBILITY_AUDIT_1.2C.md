# Sub-agent 1.2C — Implementation Feasibility Audit

> Audit date: 2026-09-04
> Scope: S0–S8 spec files, pyproject.toml, existing source code
> Method: Fresh independent audit; no prior audit results read

## Executive Summary

**One critical issue** found that blocks S3 (and all subsequent stages) and affects S4's MCP integration. Two additional significant issues and several minor risks identified.

---

## 1. Dependency Check: All Required vs Available

| Spec Reference | Required Technology | Status in pyproject.toml | Risk |
|---|---|---|---|
| S0 | `rfc8785` | ✅ `rfc8785>=0.1.4,<1.0.0` | None |
| S0 | `sqlalchemy` | ✅ `sqlalchemy>=2.0.0` | None |
| S0 | `alembic` | ✅ `alembic>=1.14.0,<2.0.0` | None |
| S0 | `asyncpg` | ✅ `asyncpg>=0.30.0,<1.0.0` | None |
| S1 | `authlib` | ✅ `authlib>=1.3.0,<2.0.0` | None |
| S1 | `cryptography` | ✅ `cryptography>=42.0.0` | None |
| S2 | `temporalio` | ✅ `temporalio>=1.14,<2` | None |
| S2 | `redis` | ✅ `redis>=6,<7` | None |
| S3 | `openai` | ✅ `openai>=2.0.0,<3.0.0` | **CRITICAL — see §2** |
| S3 | `anthropic` | ✅ `anthropic>=0.70.0,<1.0.0` | **CRITICAL — see §2** |
| S3 | `httpx` | ✅ `httpx>=0.28.1` | **CRITICAL — see §2** |
| S3 | `tiktoken` | ❌ Not listed | See §3 |
| S4 | `mcp` | ❌ Not listed | See §3 |
| S5 | `rank-bm25` | ✅ Optional in `retrieval` extra | None |
| S5 | `sentence-transformers` | ✅ Optional in `retrieval` extra | None |
| S7 | `datasketch` | ❌ Not listed | See §3 |

**Missing dependencies that must be added to pyproject.toml:**
- `tiktoken` (for ADR-002 token counting level 2)
- `mcp` (for S4 MCP client/server)
- `datasketch` (for ADR-009 MinHash/LSH)

---

## 2. API Assumption Verification

### 2A. ADR-001: httpx Transport Capture — **CRITICAL ISSUE**

**Claim in spec:** "openai-python and anthropic-python are both based on httpx" (ADR-006, line 64-66). The spec prescribes custom `httpx.AsyncBaseTransport` subclass for wire capture.

**Verified Reality (as of 2026-08-20):**

Both SDKs have **migrated from httpx to httpx2**:

1. **openai-python**: Migrated to `httpx2`. The GitHub migration guide (`httpx2.md`) explicitly states:
   > "Replace httpx-specific objects with the corresponding httpx2 objects: `httpx.Client` → `httpx2.Client`, `httpx.AsyncClient` → `httpx2.AsyncClient`, `httpx.HTTPTransport` → `httpx2.HTTPTransport`, `httpx.AsyncHTTPTransport` → `httpx2.AsyncHTTPTransport`, `httpx.MockTransport` → `httpx2.MockTransport`"
   
   Custom transport subclasses must target HTTPX2's transport interfaces.

2. **anthropic-python v1.0.0** (released 2026-08-20): The MIGRATION.md states:
   > "The top-level `anthropic.Transport` and `anthropic.ProxiesTypes` exports were unused aliases of httpx types and are gone. Use `httpx2.BaseTransport`, `httpx2.AsyncBaseTransport` and `httpx2.Proxy` directly."

3. **MCP Python SDK v2.0.0**: Also migrated to httpx2.

**Impact on existing code:**

The following files use `httpx.AsyncBaseTransport` / `httpx.AsyncClient` and **will not work** with the current versions of openai/anthropic SDKs:

- `src/zhiwei/models/presend.py:35` — `PinnedBody(httpx.AsyncByteStream, httpx.SyncByteStream)`
- `src/zhiwei/models/presend.py:89` — `CaptureTransport(httpx.AsyncBaseTransport)`
- `src/zhiwei/models/transports/base.py:132-144` — `send()` and `send_stream()` accept `httpx.AsyncClient`
- `src/zhiwei/models/transports/openai_chat.py:156-166` — uses `httpx.AsyncClient`
- `src/zhiwei/models/transports/anthropic_messages.py:175-187` — uses `httpx.AsyncClient`
- `src/zhiwei/policy/client.py:121` — `httpx.AsyncClient(timeout=..., trust_env=False)`

**Status: DISPUTED** — The httpx transport capture approach is **technically correct** but the **library has changed**. The codebase must migrate from `httpx` to `httpx2` before S3 can be implemented. The existing `presend.py` is a valid proof-of-concept but uses the wrong library version.

**Mitigation:** The httpx2 migration is straightforward (API-compatible fork). The project should:
1. Add `httpx2` to pyproject.toml dependencies
2. Pin `openai>=2.x` and `anthropic>=1.0.0` (both require httpx2)
3. Update all transport code to use httpx2 types
4. The `CaptureTransport` pattern remains valid — only the import changes

### 2B. ADR-002: Token Counting — **CONFIRMED with caveats**

**Claim:** Three-level token counting: (1) provider API, (2) local tokenizer, (3) calibrated estimate.

**Verification:**
- **tiktoken** (level 2): Available, actively maintained (v0.14.0, Aug 2026). Supports OpenAI models via `o200k_base`, `cl100k_base`, `p50k_base`, `r50k_base` encodings. **Does NOT support Anthropic or other non-OpenAI models.** The spec correctly handles this with the three-level fallback.
- **Calibrated estimate** (level 3): The spike-02 evidence confirms linear calibration works for third-party endpoints.
- **Anthropic `messages.count_tokens`** (level 1): Available but Anthropic-only.
- **Known limitation:** tiktoken has superlinear performance on very long inputs (documented issue). For production use, this may need chunking.

**Status: CONFIRMED** — Three-level approach is implementable. tiktoken covers OpenAI models; level 3 handles the rest.

### 2C. ADR-005: Temporal Workflow Determinism — **CONFIRMED**

**Claim:** Temporal Python SDK handles durable workflows, signals, activities, Continue-As-New.

**Verification:** The Temporal Python SDK (v1.14+) supports:
- `@workflow.defn` class-based workflows with `@workflow.run`
- `@activity.defn` decorated activities (sync and async)
- Signals, queries, updates
- Continue-As-New (`workflow.continue_as_new()`)
- Workflow sandbox for determinism enforcement
- Time-skipping test environment
- Activity heartbeating and cancellation

The SDK uses its own event loop model that integrates with asyncio. The workflow sandbox enforces determinism by detecting non-deterministic calls.

**Status: CONFIRMED** — All claimed workflow patterns are supported by the SDK.

### 2D. ADR-009: MinHash/LSH — **CONFIRMED**

**Claim:** Use MinHash/LSH for deterministic similarity fast-path in memory dedup.

**Verification:** `datasketch` (v2.0.0, Jul 2026) provides:
- `MinHash` — Jaccard similarity estimation
- `MinHashLSH` — sub-linear nearest neighbor search
- `MinHashLSHTopK` — top-K retrieval
- Redis and Cassandra storage backends
- Python 3.9+ compatible

**Status: CONFIRMED** — datasketch is the standard Python library for this purpose.

### 2E. MCP OAuth 2.1 — **CONFIRMED**

**Claim:** S4 implements MCP OAuth 2.1 with PKCE, Resource Indicator, etc.

**Verification:** The MCP Python SDK (v2.1.1, Aug 2026) includes:
- `mcp.client.auth.oauth2` — Full OAuth 2.1 client implementation
- Protected resource metadata support
- PKCE flow
- Resource Indicator support
- Token refresh/revoke

**Status: CONFIRMED** — MCP SDK has built-in OAuth 2.1 support.

---

## 3. Critical Feasibility Issues

### 3.1. httpx → httpx2 Migration Required (BLOCKING for S3)

**Severity: CRITICAL**

The entire wire capture architecture (ADR-001) is built on `httpx.AsyncBaseTransport`. Since both openai-python and anthropic-python have migrated to httpx2, the CaptureTransport must be rewritten to subclass `httpx2.AsyncBaseTransport`.

**Affected files:**
- `src/zhiwei/models/presend.py` (CaptureTransport, PinnedBody)
- `src/zhiwei/models/transports/base.py` (BaseTransport.send/send_stream signatures)
- `src/zhiwei/models/transports/openai_chat.py`
- `src/zhiwei/models/transports/anthropic_messages.py`
- `src/zhiwei/policy/client.py` (OPAClient uses httpx)

**Required action:**
1. Add `httpx2` to pyproject.toml
2. Replace all `httpx` imports with `httpx2` in transport code
3. Update type annotations from `httpx.AsyncClient` to `httpx2.AsyncClient`
4. Re-run spike-01 wire capture validation with httpx2
5. Ensure `httpx2.MockTransport` works for tests (not just `httpx.MockTransport`)

**Note:** httpx2 is API-compatible with httpx, so the migration is mechanical but mandatory.

### 3.2. Missing Dependencies in pyproject.toml

**Severity: HIGH**

| Package | Required by | Purpose |
|---|---|---|
| `tiktoken` | S3 (ADR-002) | Level 2 token counting |
| `mcp` | S4 | MCP protocol client/server |
| `datasketch` | S7 (ADR-009) | MinHash/LSH similarity |

These must be added before the respective stages begin.

### 3.3. httpx Version Pinning Conflict

**Severity: MEDIUM**

`pyproject.toml` currently pins `httpx>=0.28.1`. However:
- openai-python now requires httpx2 (not httpx)
- anthropic-python v1.0 requires httpx2
- httpx 1.0 is still in pre-release (1.0.dev5, Aug 2026)

**Recommendation:** Remove `httpx>=0.28.1` from dependencies, add `httpx2>=2.5.0` instead. The project's own code (OPAClient, transports) should use httpx2 directly. httpx may still be pulled in transitively but should not be a direct dependency.

---

## 4. Version Compatibility

| Package | Pinned Version | Current Latest | Compatibility |
|---|---|---|---|
| `fastapi` | `>=0.115.0,<1.0.0` | 0.115+ | ✅ OK |
| `sqlalchemy` | `>=2.0.0` | 2.0.x | ✅ OK |
| `openai` | `>=2.0.0,<3.0.0` | 2.54+ | ⚠️ Requires httpx2 |
| `anthropic` | `>=0.70.0,<1.0.0` | 1.0.0+ | ⚠️ Upper bound blocks v1.0 |
| `temporalio` | `>=1.14,<2` | 1.24+ | ✅ OK |
| `httpx` | `>=0.28.1` | 0.28.1 (pre-1.0) | ❌ Should be replaced with httpx2 |

**anthropic upper bound issue:** The current pin `anthropic>=0.70.0,<1.0.0` blocks anthropic v1.0 which is required for httpx2 support. This must be updated to `anthropic>=1.0.0`.

---

## 5. Integration Risks

### 5.1. PostgreSQL JSONB Operations

The specs reference `jsonb_set`, `jsonb_path_query`, and JSONB round-trip semantics. These are standard PostgreSQL 14+ features available via asyncpg. **No risk identified** — asyncpg supports JSONB natively.

### 5.2. Temporal + PostgreSQL Coexistence

The specs describe Temporal handling workflow/timer/retry/signal while PostgreSQL serves as the canonical event store. This is a standard pattern. The `outbox` pattern (S0 §3) bridges the two systems transactionally. **No risk identified.**

### 5.3. OPA Python Client

The codebase already has a production-quality OPA client in `src/zhiwei/policy/client.py` that uses httpx for HTTP transport. After the httpx→httpx2 migration, this will continue to work. The client implements bounded caching, revision fencing, and fail-closed semantics. **Low risk** — only needs httpx2 update.

### 5.4. sentence-transformers for Dense Retrieval

S5 requires dense vector retrieval. `sentence-transformers>=5.0.0` is listed in the optional `retrieval` extra. The current `DenseIndex` implementation (`src/zhiwei/knowledge/indexes/dense.py`) uses brute-force cosine similarity, which is correct for small corpora. For production scale, the spec should explicitly address the need for HNSW/IVF (e.g., via FAISS or pgvector). **Medium risk** — current implementation is a placeholder.

---

## 6. Performance Assessment

### 6.1. Token Counting Performance

- tiktoken encode is O(n) for short texts but has superlinear behavior for very long inputs (>100K chars). The three-level fallback mitigates this.
- Calibrated estimate (level 3) is O(1) after calibration.

### 6.2. MinHash/LSH Convergence

The ADR-009 claim that MinHash/LSH provides a deterministic fast-path is correct. datasketch's MinHashLSH provides sub-linear query time. The "convergence" claim (queue doesn't grow linearly with Run count) is a property of the deduplication logic, not the similarity algorithm itself. **Realistic.**

### 6.3. Dense Index Performance

The current `DenseIndex` implementation is O(n) per query (brute-force). For production with millions of documents, this will be too slow. The spec should require a production-grade vector index (pgvector, FAISS, or similar) by S5. **Performance gap between current code and spec requirement.**

### 6.4. Wire Capture Performance

The CaptureTransport reads the entire body into memory before sending (`aread()`). For large bodies (multi-MB), this is a memory concern. The spec addresses this with `max_wire_body_bytes` limits. **Acceptable with the proposed limit.**

---

## 7. Recommendations

### 7.1. MANDATORY: httpx → httpx2 Migration

**Priority: P0 (blocks S3)**

Before S3 implementation begins:
1. Add `httpx2>=2.5.0` to pyproject.toml
2. Update `anthropic>=1.0.0` (remove `<1.0.0` upper bound)
3. Migrate all `httpx` imports in transport code to `httpx2`
4. Re-validate spike-01 wire capture with httpx2
5. Update OPAClient to use httpx2

### 7.2. Add Missing Dependencies

**Priority: P1 (blocks S4, S7)**

Add to pyproject.toml:
```toml
dependencies = [
    # ... existing ...
    "tiktoken>=0.14.0",      # S3: level 2 token counting
    "mcp>=2.1.0",            # S4: MCP protocol
    "datasketch>=2.0.0",     # S7: MinHash/LSH
]
```

### 7.3. Update anthropic Version Bound

**Priority: P1**

Change `anthropic>=0.70.0,<1.0.0` to `anthropic>=1.0.0` to allow v1.0 which uses httpx2.

### 7.4. Clarify Vector Index Strategy

**Priority: P2**

S5 spec should explicitly state whether the production dense index will use:
- pgvector (PostgreSQL extension)
- FAISS (Facebook AI Similarity Search)
- Or another solution

The current brute-force implementation is a development placeholder only.

### 7.5. Re-run Spikes with httpx2

**Priority: P1**

- spike-01 (wire capture) must be re-validated with httpx2 transport types
- spike-02 (token calibration) is unaffected (uses hashlib, not httpx)

---

## Appendix: Verified Technical Claims

| Claim | Source | Status |
|---|---|---|
| openai-python uses httpx internally | GitHub README | ✅ Confirmed (but now httpx2) |
| anthropic-python uses httpx internally | GitHub source | ✅ Confirmed (but now httpx2) |
| httpx supports custom AsyncBaseTransport | httpx docs | ✅ Confirmed |
| httpx2 is API-compatible with httpx | httpx2 migration guide | ✅ Confirmed |
| tiktoken supports OpenAI model tokenizers | PyPI, GitHub | ✅ Confirmed |
| tiktoken does NOT support Anthropic tokenizers | tiktoken docs | ✅ Confirmed |
| Temporal Python SDK supports async workflows | GitHub, docs | ✅ Confirmed |
| Temporal supports signals and Continue-As-New | SDK source | ✅ Confirmed |
| datasketch provides MinHash + LSH | PyPI, GitHub | ✅ Confirmed |
| MCP SDK supports OAuth 2.1 | SDK source | ✅ Confirmed |
| authlib supports OIDC/PKCE | pyproject.toml present | ✅ Confirmed |

---

**Audit complete. One critical blocking issue (httpx→httpx2) identified that must be resolved before S3 implementation.**
