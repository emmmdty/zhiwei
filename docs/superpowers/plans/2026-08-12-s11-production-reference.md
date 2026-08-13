# S11 Production Reference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package a clean-machine local product and a production deployment reference, then generate real upgrade, recovery, fault, load and security evidence before making operational claims.

**Architecture:** Stateless API/workers scale by workload; enterprise stateful services stay behind tested ports. Compose is the complete CPU-only local product, while Kubernetes overlays document/test replaceable production dependencies without pretending to operate them.

**Tech Stack:** Docker Compose, Kubernetes/Kustomize or Helm (choose one via a documented spike), PostgreSQL, Temporal, OpenSearch, Garage/S3, Redis, Keycloak/OIDC, OPA, Vault/KMS port, OpenTelemetry.

---

### Task 1: Build production images and complete local-product Compose

**Files:** Create `Dockerfile`, `apps/web/Dockerfile`, `deploy/compose/{compose.yaml,profiles/,configs/,secrets.example/}`,
`.dockerignore`, `tests/e2e/local_product/`; modify `src/zhiwei/cli/dev.py`; create
`tests/contract/cli/test_dev_product_cli.py`.

- [ ] Add image tests for non-root, read-only rootfs, no source secrets, health/readiness and pinned base/service digests.
- [ ] Compose reverse proxy/Web/API/Agent/Integration/Index/Eval/Dispatcher/Capability Runner plus PG/Temporal dev/
  OpenSearch/Garage/Redis/Keycloak/OPA/OTel/reference sources/tools.
- [ ] Ensure startup never invokes live providers and CPU-only fixture journey completes.
- [ ] Run `docker compose ... up -d --wait` on clean state and strict doctor.
- [ ] Register `dev up|down` and add `dev doctor --strict`; test `--help`, config-only dry-run, safe project scoping and
  fixture product health. The wrapper must not trigger live providers or delete unrelated Compose resources.
- [ ] Suggested commit: `feat(deploy): package complete local product`.

### Task 2: Choose and implement the Kubernetes reference format

**Files:** Create `docs/adr/ADR-001-kubernetes-packaging.md`, `deploy/kubernetes/{base,overlays/}`, validation tests.

- [ ] Spike Kustomize vs Helm against required overlays/version pin/secret externalization; record decision, not both systems.
- [ ] Add stateless deployments/services/jobs, queues, probes, resources, PDB, NetworkPolicy, Pod security and ingress/TLS assumptions.
- [ ] Reference external PG/Temporal/Search/S3/OIDC/KMS/Redis/OTLP; do not deploy fake HA stateful operators.
- [ ] Render/validate manifests in CI and scan policies/images.
- [ ] Suggested commit: `feat(deploy): add validated production reference`.

### Task 3: Implement version and upgrade procedures

**Files:** Create `docs/operations/upgrade.md`, `src/zhiwei/operations/upgrade.py`,
`tests/e2e/local_product/test_upgrade.py`.

- [ ] Build previous→current fixture for Alembic expand/migrate/contract, event/manifest reader and Temporal version markers.
- [ ] Test OpenSearch new index/rebuild/alias switch and Agent/Tool/Skill version pins during upgrade.
- [ ] Implement preflight/abort/rollback rules; destructive contract migration requires explicit checkpoint.
- [ ] Suggested commit: `feat(ops): verify compatible upgrades`.

### Task 4: Implement backup and isolated restore verification

**Files:** Create `src/zhiwei/operations/{backup.py,restore.py}`, `src/zhiwei/cli/operations.py`,
`docs/operations/backup-restore.md`, `tests/contract/cli/test_operations_cli.py`,
`tests/e2e/local_product/test_restore.py`.

- [ ] Define backup manifest for PG/Object/secret recovery procedure/Temporal scope/version claims; exclude Redis/search as truth.
- [ ] Restore into isolated namespaces, rebuild projections/search and reconcile workflows.
- [ ] Verify RLS, artifact digests, secret rotation and Claim Registry; corrupt one component and require failure.
- [ ] Register `backup create|verify` and `restore verify`; test `--help`, invalid profile, corrupt manifest and isolated smoke.
- [ ] Suggested commit: `feat(ops): add verified backup restore`.

### Task 5: Build deterministic fault runner

**Files:** Create `src/zhiwei/operations/faults.py`; modify `src/zhiwei/cli/operations.py`; create `tests/fault/`,
`docs/operations/incident-response.md`.

- [ ] Register kill/restart/partition/slow/duplicate/corrupt scenarios for every dependency from S11 spec.
- [ ] Run each against fixed fixture workloads and assert expected fail-close/degrade/recover terminal state.
- [ ] Include OPA down, Redis loss, search loss, object corruption and external effect_unknown distinctions.
- [ ] Seal raw events, environment/image digests and recovery times.
- [ ] Register `ops fault-run`; test `--help`, unknown scenario/profile and fixture dry-run before destructive execution.
- [ ] Suggested commit: `test(ops): seal dependency fault matrix`.

### Task 6: Build fixed load/capacity runner

**Files:** Create `src/zhiwei/operations/load.py`; modify `src/zhiwei/cli/operations.py`; create `tests/load/`,
`docs/operations/capacity.md`.

- [ ] Define concurrent Ask, scheduled Discover, sync/index and eval workloads with fixed data/fixture providers.
- [ ] Measure queue wait, p50/p95, error/terminal/recovery, CPU/memory/I/O and cost-mode fields.
- [ ] Vary concurrency until bottleneck; report environment and uncertainty before proposing SLO.
- [ ] Do not publish unmeasured HA/SLO. Suggested commit: `test(ops): add reproducible capacity report`.
- [ ] Register `ops load-run`; test `--help`, invalid workload/profile and bounded fixture smoke.

### Task 7: Re-run production-topology security and no-secret scans

**Files:** Create `tests/security/production_topology/`, `docs/operations/security.md`.

- [ ] Re-run tenant/OIDC/OPA/ACL/MCP/sandbox/injection/action/artifact/SSE cases through Compose and rendered K8s policy.
- [ ] Scan image layers, configs, backups, ObjectStore, PG samples, trace/log/export and frontend bundle for sentinels.
- [ ] Record residual risks and exact tested versions; do not replace failures with allowlists unless design permits.
- [ ] Suggested commit: `test(security): verify production reference topology`.

### Task 8: Seal release and demo evidence

**Files:** Create/update `docs/operations/install.md`, generated Claim Registry/report links, `demo/` script/manifest.

- [ ] Reproduce clean Ubuntu/WSL2 CPU-only install and full fixture journey from one documented command.
- [ ] Record three-minute flow: cross-source Ask Evidence; context actual-wire/tamper; Discover→Case→approval→Receipt;
  Studio/Capability binding; ChangeBrief third App.
- [ ] Only with explicit operator run the approved OpenCode Go live preflight/eval; otherwise release remains offline-verified.
- [ ] Run every S11 Gate command, strict release check and provenance attestation; publish only artifacts allowed by policy/license.
- [ ] Suggested commit: `release: seal verified local product and production reference`.
