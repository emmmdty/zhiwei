# S4 Capability Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let organization publishers discover/import, inspect, admit, connect, bind, run, update and revoke MCP/OpenAPI/Skill/SDK/Agent capabilities through real UI and governed execution.

**Architecture:** Capability metadata and versions live in PostgreSQL; credentials reuse the S1 SecretBackend; providers normalize to ToolDefinition or ResourceDefinition/SourceObservationProvider, and S5 alone turns authorized observations into DataSource/SourceVersion. Execution crosses policy, approval and an independent runner boundary.

**Tech Stack:** MCP 2025-11-25, OAuth 2.1/OIDC libraries, HTTPX, JSON Schema/OpenAPI 3.1, Agent Skills, OCI runners, cryptography AES-GCM, Vault/KMS port, React.

---

### Task 1: Implement capability resources and lifecycle

**Files:** Create `src/zhiwei/capabilities/{domain.py,versions.py,admission.py,repositories.py}`,
`migrations/versions/0004_capabilities.py`, `tests/unit/capabilities/test_lifecycle.py`.

- [ ] Test immutable versions and every lifecycle transition, publisher roles, update diff and immediate suspend/revoke.
- [ ] Model AdmissionRecord decisions with actor/role/test+content digest. For high/critical versions require distinct
  Capability Publisher and Security Admin approvals; reject same actor, stale digest and concurrent publish via CAS.
- [ ] Implement Provider/Tool/Skill/Workflow/Admission/Binding schemas and tenant repositories with RLS.
- [ ] Ensure remote self-reported risk/effect stays separate from administrator-confirmed admission values.
- [ ] Suggested commit: `feat(capabilities): add governed version lifecycle`.

### Task 2: Implement catalog discovery and quarantine

**Files:** Create `src/zhiwei/capabilities/catalog/{base.py,mcp_registry.py,git.py,imports.py}`,
`tests/contract/capabilities/test_catalog.py`.

- [ ] Test official registry pagination/identity, Git/URL digest, size/license metadata and catalog outage.
- [ ] Implement discover/import to immutable quarantine without credentials or execution.
- [ ] Record source URL, publisher, fetched_at and digest; no “install and run” shortcut.
- [ ] Suggested commit: `feat(capabilities): import catalogs into quarantine`.

### Task 3: Implement SecretBackend and Connections

**Files:** Create `src/zhiwei/capabilities/{connections.py,credential_bindings.py}`; reuse S1
`src/zhiwei/secrets/{base.py,local.py}` and create `src/zhiwei/secrets/vault.py`,
`tests/security/capabilities/test_secrets.py`, `tests/contract/capabilities/test_connections.py`.

- [ ] Re-run S1 AES-GCM/rotation/revoke contract for Connection credentials and test write-only API serialization.
- [ ] Implement user_delegated/workspace_service/service_account Connections and opaque credential refs.
- [ ] Test that execution after approval re-reads membership, active policy, capability/Connection/credential status and
  denies any revoke/expiry instead of honoring stale approval.
- [ ] Add Vault Transit implementation behind the same S1 SecretBackend port and a contract fake, not a fake
  “production secret store”.
- [ ] Scan DB/API/log/trace/artifact/Temporal payload for sentinel secrets.
- [ ] Suggested commit: `feat(capabilities): add scoped connections and secret backends`.

### Task 4: Implement MCP client and OAuth

**Files:** Create `src/zhiwei/capabilities/mcp/{client.py,transport.py,oauth.py,mapping.py,capabilities.py}`,
`tests/fixtures/mcp/`, `tests/contract/capabilities/test_mcp.py`, `tests/security/capabilities/test_mcp_oauth.py`.

- [ ] Build reference stdio/Streamable HTTP fixtures for tools/resources/prompts/roots/elicitation/sampling/tasks.
- [ ] Test OAuth metadata, PKCE, Resource Indicator, audience/scope/refresh/revoke and token passthrough rejection.
- [ ] Map tools to ToolDefinition and resources to ResourceDefinition/SourceObservationProvider; prompts remain Skill
  candidates. S5 alone persists observations as DataSource/SourceVersion under Knowledge policy.
- [ ] Disable sampling by default and always for Discover service identity; reject secret elicitation.
- [ ] Key MCP process/session by org/workspace/provider version/connection subject/run and reject cross-key reuse.
- [ ] Suggested commit: `feat(mcp): add full governed protocol mapping`.

### Task 5: Implement OpenAPI, Agent Skills and SDK providers

**Files:** Create `src/zhiwei/capabilities/openapi/{importer.py,operations.py,auth.py}`,
`src/zhiwei/capabilities/skills/{package.py,validator.py,projection.py,script_tool.py}`,
`src/zhiwei/capabilities/sdk.py`, corresponding contract/security tests.

- [ ] Test OpenAPI `$ref` limits/cycles, selected operations, immutable host, typed params and write idempotency.
- [ ] Test Skills metadata/reference/assets, HTML sanitization, allowed-tools narrowing and script classification.
- [ ] Define SDK discovery/invoke/health/auth port and one reference provider.
- [ ] Ensure executable Skill becomes a versioned Tool, never a host subprocess.
- [ ] Suggested commit: `feat(capabilities): add openapi skills and sdk providers`.

### Task 6: Build admission inspection and malicious corpus

**Files:** Create `src/zhiwei/capabilities/inspection/{schema.py,supply_chain.py,network.py,contracts.py}`,
`src/zhiwei/capabilities/admission_commands.py`,
`tests/fixtures/capabilities/malicious/`, `tests/security/capabilities/test_admission.py`.

- [ ] Add schema bomb, prompt injection, SSRF/redirect/DNS, secret exfiltration, capability drift, oversized output and license/SBOM cases.
- [ ] Implement deterministic inspection report and approval requirements; failed tests cannot be overridden by Builder.
- [ ] Pin every admitted source/image digest; update creates a new candidate.
- [ ] Implement publisher/security approval commands and API PEPs; test direct repository/API bypass cannot publish a
  high/critical version without two distinct current decisions.
- [ ] Suggested commit: `feat(capabilities): enforce admission security gates`.

### Task 7: Implement Tool Gateway and isolated runners

**Files:** Create `src/zhiwei/capabilities/{tool_gateway.py,invocations.py}`,
`src/zhiwei/capabilities/runners/{contracts.py,client.py,remote_http.py,prebuilt.py,kubernetes.py}`,
`src/zhiwei/runtime/handlers/invoke_tool.py`, `src/zhiwei/workflows/activities/tools.py`,
`src/zhiwei/workers/capability_runner.py`, tests under `tests/integration/capabilities/`.

- [ ] Test full intent→policy→approval→credential→execute→validate/redact→receipt sequence.
- [ ] Repeat the complete authorization/Connection/input-digest check after approval and immediately before execution.
- [ ] Test fixed OCI, non-root/read-only/no socket/default no-network/resource/time/process limits; if unavailable, fail closed.
- [ ] Implement authenticated internal Runner IPC. local test calls a dedicated prebuilt reference-provider service with
  pinned signed image; it never mounts the host Docker socket and never accepts uploaded code at runtime.
- [ ] Implement the Kubernetes Job backend contract: least-privilege ServiceAccount, per-invocation Pod, digest pin,
  seccomp/AppArmor, read-only rootfs, resource/NetworkPolicy/projected short secret, status/watch and cleanup. S11 runs
  this against the production-reference topology; S4 contract-tests its generated manifests and fake API.
- [ ] Verify API/Agent Worker has no runtime credential. A newly admitted local executable without a deployed prebuilt
  runner returns `execution_backend_unavailable`; remote MCP/OpenAPI remains usable.
- [ ] Test remote exact origin, redirects, DNS rebinding, response limits and model host override.
- [ ] Verify duplicate/effect_unknown semantics reuse S2 implementation.
- [ ] Register InvokeTool in S2 TaskHandlerRegistry; only the Tool Activity crosses runner IPC and all results commit
  through canonical events.
- [ ] Suggested commit: `feat(tools): add governed isolated invocation gateway`.

### Task 8: Build Capability Hub Web journey

**Files:** Create `src/zhiwei/api/{capabilities.py,connections.py}`, `src/zhiwei/cli/providers.py`,
`tests/contract/cli/test_provider_cli.py`, `apps/web/src/features/capabilities/`, `apps/web/e2e/capability-hub.spec.ts`.

- [ ] Write Publisher/Security/Builder journeys for import→inspect→test→admit→connect→bind→suspend.
- [ ] Implement real API actions, version diff, connection OAuth/status and permission/error states.
- [ ] Register `provider inspect|test|admit`; test `--help`, invalid/quarantined provider and reference fixture smoke.
- [ ] Run malicious suite, reference provider sealed tests, Playwright and S4 Gate.
- [ ] Assert `provider test --all-reference --sealed` executes stdio through the independent prebuilt runner service;
  monkeypatching/in-process provider execution must make the Gate fail.
- [ ] Suggested commit: `feat(web): complete capability hub journey`.
