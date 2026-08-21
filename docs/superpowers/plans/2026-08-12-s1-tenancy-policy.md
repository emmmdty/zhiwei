# S1 Tenancy, Identity and Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a real multi-organization identity and authorization vertical slice with OIDC BFF, RBAC/OPA, PostgreSQL RLS, audit and role-aware Web flows.

**Architecture:** The IdP authenticates; ZhiWei owns Organization/Workspace authorization. API PEPs, OPA decisions and RLS form independent layers, and every mutation writes an audit outbox in the same transaction.

**Tech Stack:** FastAPI, Authlib/OIDC client, cryptography AES-GCM, Keycloak, SCIM 2.0 endpoints, OPA/Rego, PostgreSQL RLS, Node.js 22 LTS, npm, React/Vite, Playwright.

---

### Task 1: Model principals, organizations and memberships

**Files:** Create `src/zhiwei/identity/{domain.py,commands.py,repositories.py}`,
`src/zhiwei/api/{organizations.py,workspaces.py,memberships.py}`,
`migrations/versions/0002_identity.py`, `tests/unit/identity/test_domain.py`, `tests/integration/identity/test_memberships.py`.

- [x] Test User/ServiceAccount/AgentIdentity, `(issuer, subject)` uniqueness, Group membership and disable lifecycle.
- [x] Run focused tests; expect RED. Implement Principal/ExternalIdentity/AuthSession/Organization/Workspace/Group/
  Membership domain, commands and tenant-scoped repositories.
- [x] Add RLS policies for every new table and test raw cross-org identifiers return no rows.
- [x] Suggested commit: `feat(identity): add organization principal model`.

### Task 2: Implement SecretBackend and OIDC BFF sessions

**Files:** Create `src/zhiwei/identity/{oidc.py,sessions.py}`,
`src/zhiwei/secrets/{__init__.py,base.py,local.py}`, `src/zhiwei/api/auth.py`,
`tests/security/identity/{test_oidc.py,test_session_secrets.py}`; add Keycloak config under `deploy/compose/keycloak/`.

- [x] Test state/nonce/PKCE, callback replay, Secure/HttpOnly/SameSite cookie, fixation, CSRF, logout and disabled user.
- [x] Add `cryptography`/OIDC dependencies with `uv lock`, pinned Keycloak service/realm and Docker-secret master key.
- [x] Implement generic SecretBackend port/local AES-GCM envelope store and principal/session-level encrypted AuthSession;
  AAD binds session/issuer/subject/version without org so first login and multi-org membership work. Active org is selected
  after authentication. S4 must reuse this port with per-org AAD for Connection secrets.
- [x] Implement login/callback/logout/me, refresh/revoke and opaque server session with CAS for multiple replicas.
- [x] Verify restart/rotation/expiry/revoke and that no token appears in API/browser/log/trace/Temporal or PG plaintext.
- [x] Suggested commit: `feat(auth): add oidc bff sessions`.

### Task 3: Add RBAC and OPA policy decisions

**Files:** Create `src/zhiwei/policy/{roles.py,input.py,client.py,enforcement.py}`, `deploy/compose/opa/`,
`policies/zhiwei/{authz.rego,authz_test.rego}`, `tests/unit/policy/`, `tests/integration/policy/`.

- [x] Generate role/resource/action/scope and separation-of-duty cases from the frozen matrix in `docs/PERMISSIONS.md`,
  including Agent Builder, no self-publish, no self-approval and dual control for high-risk CapabilityVersion.
- [x] Run `opa test policies/zhiwei -v`; expect RED before rules exist.
- [x] Implement versioned bundle loading, decision id/revision, bounded cache and fail-close behavior.
- [x] Run OPA tests plus Python contract tests for unavailable/stale bundle.
- [x] Suggested commit: `feat(policy): enforce rbac and opa decisions`.

### Task 4: Complete FORCE RLS and audit enforcement

**Files:** Modify `migrations/versions/0002_identity.py`, `src/zhiwei/persistence/tenant.py`; create
`src/zhiwei/identity/audit.py`, `tests/security/tenancy/{test_rls.py,test_idor.py,test_pool.py}`.

- [x] Test missing `SET LOCAL`, pool reuse, app-role owner/BYPASSRLS, list/count/cursor and guessed IDs.
- [x] Implement transaction-scoped tenant context and FORCE RLS; keep repository tenant predicates.
- [x] Ensure allow/deny/mutation audit records contain actor/effective identity, resource version and OPA revision.
- [x] Run the security suite with two orgs and overlapping resource names.
- [x] Suggested commit: `feat(security): enforce tenant rls and audit`.

### Task 5: Add SCIM and membership lifecycle

**Files:** Create `src/zhiwei/identity/scim.py`, `src/zhiwei/api/scim.py`,
`tests/contract/identity/test_scim.py`, `tests/integration/identity/test_scim_lifecycle.py`.

- [x] Test create/update/disable, duplicate external identity, group reconciliation and idempotent retries.
- [x] Implement the required SCIM User/Group subset with explicit unsupported operations.
- [x] Verify disable blocks new sessions/commands and emits audit; do not delete historical actor refs.
- [x] Suggested commit: `feat(identity): add scim lifecycle`.

### Task 6: Build the role-aware organization Web shell

**Files:** Create `apps/web/{package.json,package-lock.json,.nvmrc,playwright.config.ts,tsconfig.json,vite.config.ts}` and
`apps/web/src/features/{auth,organizations,workspaces,members}/`;
create `apps/web/e2e/tenancy.spec.ts`.

- [x] Pin Node.js 22 in `.nvmrc`/CI, use npm exclusively, add `test:e2e` script and run `npm --prefix apps/web ci`.
- [x] Write Playwright journeys for Owner, Agent Builder, Member, Approver and Auditor before implementing views.
- [x] Implement same-origin API client, server-state cache, protected routes and real create/invite/role/remove actions.
- [x] Cover loading/empty/error/403/revoked states; hidden navigation must not replace server enforcement.
- [x] Run `npm --prefix apps/web run test:e2e -- tenancy.spec.ts` and S1 Gate; save sealed report.
- [x] Suggested commit: `feat(web): add multi-role organization shell`.
