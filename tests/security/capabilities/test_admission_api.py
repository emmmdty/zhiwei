"""F-R9-01 remediation RED phase — admission/lifecycle contract on the API
surface: POST /api/v1/capabilities/providers/{id}/actions currently
direct-writes lifecycle status (capabilities.py:352-373), bypassing
CapabilityVersionManager.transition (src/zhiwei/capabilities/versions.py:99-131,
_VALID_TRANSITIONS at :21-35) and ApprovalPEP
(src/zhiwei/capabilities/admission_commands.py:149-236).

Contract under test (remediation F-R9-01, aligned with S4 §7 and the domain
admission semantics verified in tests/security/capabilities/test_admission.py):
- skip-step transition (e.g. discovered → admit / publish) → 409
  (versions.py InvalidTransitionError);
- publish without any valid approval → 403 (admission.py
  InsufficientApprovalError surfaced by ApprovalPEP);
- the same actor holding both publisher and security approvals → 403
  (admission.py SameActorError, admission.py:271-275);
- second/concurrent publish writer → 409 (CAS conflict; versions.py
  VersionConflictError / invalid published→published transition).

Seeding approvals
-----------------
The API surface has no approval endpoint today, so approvals are seeded through
the domain AdmissionManager the fix is expected to wire per tenant — exactly
how tests/security/capabilities/test_admission.py seeds them via
PublisherApprovalCommand / SecurityApprovalCommand. Pre-fix, zhiwei.api.capabilities
._RepoStore keeps no admission manager at all; _admission_manager_for() below
is then a documented no-op and every publish assertion fails with the plain
200-vs-403/409 mismatch, which IS the defect (no admission PEP on the API at
all). The helper probes the most plausible seams the fix could expose;
if the fix lands with a different shape, adjust _ADMISSION_SEAMS only — the
assertions encode the contract, not the seam.

Design assumption (from the frozen S4 admission design, not invented here):
"admit" is a lifecycle transition only and records no approval; approvals exist
exclusively as AdmissionRecords created by the admission commands. If the fix
instead auto-records an approval inside "admit", this file's
publish-without-approval test is deliberately expected to stay red — that
design would contradict the remediation contract.

"Second writer" CAS formulation: the request model forbids extra fields
(capabilities.py:125-130, verified 422 for expected_version), so concurrency
is simulated deterministically as a lost update — writer 2 publishes from a
state that writer 1 already consumed. Today both attempts return 200; post-fix
writer 2 must get 409. When the fix adds an explicit CAS field to
LifecycleActionRequest, extend this test with an expected_version variant.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fixtures.policy_fake import FakePolicyEnforcer

from zhiwei.api.capabilities import _store, _TenantContext, create_capabilities_router
from zhiwei.capabilities.admission import AdmissionManager
from zhiwei.capabilities.admission_commands import (
    PublisherApprovalCommand,
    SecurityApprovalCommand,
)
from zhiwei.capabilities.domain import RiskLevel
from zhiwei.identity.domain import ActorContext, ActorRoleBinding

ORG = UUID("11111111-1111-4111-8111-111111111111")
WS = UUID("22222222-2222-4222-8222-222222222222")
PUBLISHER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SECURITY = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


def _clear_store() -> None:
    _store._provider_versions.clear()
    _store._cap_versions.clear()
    _store._bindings.clear()
    _store._repos.clear()
    _store._binding_repos.clear()
    _store._version_managers.clear()


@pytest.fixture(autouse=True)
def _isolated_store() -> Generator[None]:
    _clear_store()
    yield
    _clear_store()


def _actor(principal: UUID) -> ActorContext:
    """Capability-publisher actor (org-scoped role per policy/roles.py)."""
    return ActorContext(
        principal_id=principal,
        organization_id=ORG,
        workspace_id=WS,
        role_bindings=(
            ActorRoleBinding(
                name="capability_publisher", scope="org", organization_id=ORG
            ),
        ),
    )


def _client(principal: UUID = PUBLISHER) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_capabilities_router(
            actor_dependency=lambda: _actor(principal),
            policy_enforcer=FakePolicyEnforcer(allow=True),
        )
    )
    return TestClient(app)


# ── Lifecycle helpers (valid chain per versions.py:21-35) ─────────────


def _register(client: TestClient, *, risk: str = "low", name: str = "admission-target") -> UUID:
    resp = client.post(
        "/api/v1/capabilities/providers",
        json={"name": name, "risk_level": risk},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


def _walk(client: TestClient, provider_id: UUID, actions: tuple[str, ...]) -> None:
    for action in actions:
        resp = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": action},
        )
        assert resp.status_code == 200, f"{action} failed: {resp.text}"


def _cap_version_of(client: TestClient, provider_id: UUID) -> dict:
    resp = client.get("/api/v1/capabilities/versions")
    assert resp.status_code == 200
    matching = [
        v
        for v in resp.json()
        if v.get("metadata", {}).get("provider_version_id") == str(provider_id)
    ]
    assert matching, f"no capability version for provider {provider_id}"
    return matching[0]


# ── Approval seeding seam (see module docstring) ───────────────────────

_ADMISSION_SEAMS = (
    "get_admission_manager",  # fix exposes an accessor taking the API _TenantContext
    "_admission_managers",    # fix keeps a per-(org, ws) dict, mirroring get_repo
    "_admission_manager",     # fix keeps one process-wide manager
)


def _admission_manager_for(actor: ActorContext) -> AdmissionManager | None:
    """Locate the admission manager the F-R9-01 fix is expected to wire.

    Returns None pre-fix (no seam exists — that absence is part of the defect)
    and raises post-fix only through the caller's seam-effectiveness assertion,
    never inside a RED run.
    """
    for seam in _ADMISSION_SEAMS:
        candidate: object = getattr(_store, seam, None)
        if candidate is None:
            continue
        if callable(candidate):
            try:
                manager = candidate(_TenantContext(actor))
            except TypeError:
                continue
            if isinstance(manager, AdmissionManager):
                return manager
        if isinstance(candidate, dict):
            manager = candidate.get((actor.organization_id, actor.workspace_id))
            if isinstance(manager, AdmissionManager):
                return manager
        if isinstance(candidate, AdmissionManager):
            return candidate
    return None


def _seed_approvals(
    manager: AdmissionManager,
    *,
    version_id: UUID,
    publisher_actor: UUID,
    security_actor: UUID | None,
    risk_level: RiskLevel,
    test_digest: str,
    content_digest: str,
) -> None:
    """Record approvals exactly like the domain tests do
    (tests/security/capabilities/test_admission.py::TestApprovalPEP).

    security_actor=None seeds the single publisher approval required for
    low/medium risk; a distinct security_actor seeds the dual-actor pair.
    """
    PublisherApprovalCommand(manager).approve(
        version_id=version_id,
        actor_id=publisher_actor,
        risk_level=risk_level,
        test_digest=test_digest,
        content_digest=content_digest,
    )
    if security_actor is not None:
        SecurityApprovalCommand(manager).approve(
            version_id=version_id,
            actor_id=security_actor,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
        )


# ══════════════════════════ Transition validity ════════════════════════


class TestTransitionValidity:
    def test_skip_step_admit_from_discovered_is_409(self) -> None:
        """CURRENT: admit from discovered returns 200 and writes APPROVED
        (capabilities.py:352-368 maps actions to statuses with no transition
        check; write at :370-373). POST-FIX: 409 — discovered→approved is not
        in _VALID_TRANSITIONS (versions.py:22-26), InvalidTransitionError → 409.
        """
        client = _client()
        provider_id = _register(client, risk="low")
        resp = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": "admit"},
        )
        assert resp.status_code == 409, (
            f"skip-step admit must be 409; got {resp.status_code}: {resp.text}"
        )
        stored = _store.get_provider(
            provider_id, organization_id=ORG, workspace_id=WS
        )
        assert stored is not None and stored.status.value == "discovered", (
            "denied transition must not mutate provider status"
        )

    def test_skip_step_publish_from_discovered_is_409(self) -> None:
        """CURRENT: publish from discovered returns 200 (same direct-write path).
        POST-FIX: 409 — structural transition validity is checked before the
        approval gate, so an invalid publish is 409 even with zero approvals."""
        client = _client()
        provider_id = _register(client, risk="low")
        resp = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": "publish"},
        )
        assert resp.status_code == 409, (
            f"skip-step publish must be 409; got {resp.status_code}: {resp.text}"
        )

    def test_full_chain_transitions_remain_allowed(self) -> None:
        """GUARD (passes today, must keep passing): the full valid chain
        discovered→quarantined→inspected→tested→approved is accepted; the fix
        must route these through CapabilityVersionManager.transition without
        breaking the happy path."""
        client = _client()
        provider_id = _register(client, risk="low")
        _walk(client, provider_id, ("quarantine", "inspect", "test", "admit"))
        detail = client.get(f"/api/v1/capabilities/providers/{provider_id}")
        assert detail.json()["status"] == "approved"


# ══════════════════════════ Approval enforcement ═══════════════════════


class TestPublishApprovalEnforcement:
    def test_publish_without_approval_is_403(self) -> None:
        """CURRENT: the full chain plus publish returns 200 with zero approval
        records anywhere — the API never consults ApprovalPEP
        (capabilities.py:336-391). POST-FIX: 403 (no valid approvals for the
        low-risk single-actor requirement, admission.py:236-243)."""
        client = _client()
        provider_id = _register(client, risk="low")
        _walk(client, provider_id, ("quarantine", "inspect", "test", "admit"))
        resp = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": "publish"},
        )
        assert resp.status_code == 403, (
            f"publish without approvals must be 403; got {resp.status_code}: {resp.text}"
        )
        stored = _store.get_provider(provider_id, organization_id=ORG, workspace_id=WS)
        assert stored is not None and stored.status.value == "approved", (
            "denied publish must leave the provider approved"
        )

    def test_publish_with_same_actor_dual_approval_is_403(self) -> None:
        """CURRENT: publish returns 200 regardless of who approved what — no
        admission wiring exists. POST-FIX: 403 — high/critical requires two
        DISTINCT actors (admission.py:269-275 SameActorError).

        The publisher seeds BOTH approvals with their own principal, then
        publishes; the assertion order is deliberate: the 403 check runs first
        so RED fails on the API behavior, and the seam-effectiveness check runs
        afterwards to prevent a post-fix pass for the wrong reason (missing
        approvals instead of the same-actor violation)."""
        client = _client(PUBLISHER)
        provider_id = _register(client, risk="high")
        _walk(client, provider_id, ("quarantine", "inspect", "test", "admit"))
        version = _cap_version_of(client, provider_id)

        manager = _admission_manager_for(_actor(PUBLISHER))
        if manager is not None:
            _seed_approvals(
                manager,
                version_id=UUID(version["id"]),
                publisher_actor=PUBLISHER,
                security_actor=PUBLISHER,  # same actor on both roles — must be rejected
                risk_level=RiskLevel.HIGH,
                test_digest=version["test_digest"],
                content_digest=version["content_digest"],
            )

        resp = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": "publish"},
        )
        assert resp.status_code == 403, (
            "publish must be 403 when one actor holds both approvals; "
            f"got {resp.status_code}: {resp.text}"
        )
        assert manager is not None, (
            "admission approvals could not be seeded: no admission-manager seam on "
            "zhiwei.api.capabilities._store — see _ADMISSION_SEAMS in this module; "
            "this 403 may be 'no approvals at all' rather than the same-actor violation"
        )

    def test_publish_with_distinct_dual_approval_succeeds(self) -> None:
        """GUARD (passes today, must keep passing): high-risk publish with two
        distinct actors (publisher + security admin, seeded via the domain
        commands) is allowed — the fix must not over-block the legitimate path.
        Depends on the seeding seam post-fix; see module docstring."""
        client = _client(PUBLISHER)
        provider_id = _register(client, risk="high")
        _walk(client, provider_id, ("quarantine", "inspect", "test", "admit"))
        version = _cap_version_of(client, provider_id)

        manager = _admission_manager_for(_actor(PUBLISHER))
        if manager is not None:
            _seed_approvals(
                manager,
                version_id=UUID(version["id"]),
                publisher_actor=PUBLISHER,
                security_actor=SECURITY,
                risk_level=RiskLevel.HIGH,
                test_digest=version["test_digest"],
                content_digest=version["content_digest"],
            )

        resp = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": "publish"},
        )
        assert resp.status_code == 200, (
            f"distinct dual-actor publish must be allowed; got {resp.status_code}: {resp.text}"
        )
        assert client.get(f"/api/v1/capabilities/providers/{provider_id}").json()[
            "status"
        ] == "published"


# ══════════════════════════ CAS / concurrent publish ═══════════════════


class TestConcurrentPublish:
    def test_second_publish_writer_conflicts_409(self) -> None:
        """CURRENT: publishing twice returns 200 both times — writer 2 silently
        rewrites status (capabilities.py:368-373; no CAS, no transition check).
        POST-FIX: writer 2 gets 409 — published→published is not a valid
        transition (versions.py:27-31) and/or expected-version CAS raises
        VersionConflictError (versions.py:118-126). Deterministic lost-update
        formulation; see module docstring for the explicit-CAS extension.

        A publisher approval is seeded pre-publish (via the domain seam) so the
        FIRST publish passes the approval gate post-fix; the conflict under test
        is the second writer, not a missing approval."""
        client = _client()
        provider_id = _register(client, risk="low")
        _walk(client, provider_id, ("quarantine", "inspect", "test", "admit"))
        version = _cap_version_of(client, provider_id)

        manager = _admission_manager_for(_actor(PUBLISHER))
        if manager is not None:
            _seed_approvals(
                manager,
                version_id=UUID(version["id"]),
                publisher_actor=PUBLISHER,
                security_actor=None,
                risk_level=RiskLevel.LOW,
                test_digest=version["test_digest"],
                content_digest=version["content_digest"],
            )

        first = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": "publish"},
        )
        assert first.status_code == 200, first.text

        second = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": "publish"},
        )
        assert second.status_code == 409, (
            f"second publish writer must conflict with 409; got {second.status_code}: "
            f"{second.text}"
        )


# ══════════════════════════ Harness sanity ═════════════════════════════


class TestHarnessSanity:
    def test_register_generates_unique_ids(self) -> None:
        """GUARD: two registers never collide (uuid4-based store keys); keeps
        the shared module-level store safe for the whole module run."""
        client = _client()
        first = _register(client, name="unique-a")
        second = _register(client, name="unique-b")
        assert first != second
