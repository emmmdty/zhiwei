"""F-R3-01 remediation RED phase — IDOR on the in-memory stub router
(capabilities), mounted in app.py:240-245 with actor_dependency only.

Contract under test (docs/PERMISSIONS.md §3.1, §15; mirrors the frozen S1-T4
IDOR semantics in tests/security/tenancy/test_idor.py):
- cross-org read AND write of another org's resource: uniform 403/404, no
  existence leak, zero write side effects;
- cross-workspace (same org) access: denied — workspace is a collaboration
  boundary, not just a display filter;
- (connections left this file in P2b: PG + PEP wiring moved its IDOR/role
  cases to tests/integration/capabilities/test_connections_api.py.)

Each test's docstring states: CURRENT failure mode (what happens today and at
which line) and the POST-FIX expectation. RED tests fail on the HTTP status
assertion first; zero-write assertions run afterwards so a fix that denies but
still mutates also stays red.

The in-memory routers keep module-level singleton stores; the autouse fixture
wipes them around every test (same pattern as tests/contract/api/
test_stub_routers_golden_snapshot.py) so tests are order-independent.

P2b note: the connections and knowledge routers left this file when they
were rewired onto PG + PEP (F-R6-03) — their IDOR cases moved to
tests/integration/capabilities/test_connections_api.py and
tests/integration/knowledge/test_knowledge_api_pg.py.

P2b note: the memory router left this file when it was rewired onto PG + PEP
(F-R6-02/03) — its IDOR cases moved to
tests/integration/memory/test_memory_center_api.py (tenant-scoped queries
replace the in-memory predicates).
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fixtures.policy_fake import FakePolicyEnforcer

from zhiwei.api.capabilities import _store as cap_store
from zhiwei.api.capabilities import create_capabilities_router
from zhiwei.capabilities.domain import (
    CapabilityStatus,
    CapabilityVersion,
    ProviderVersion,
    RiskLevel,
)
from zhiwei.identity.domain import ActorContext, ActorRoleBinding

_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)

ORG_A = UUID("11111111-1111-4111-8111-111111111111")
WS_A = UUID("22222222-2222-4222-8222-222222222222")
WS_A2 = UUID("33333333-3333-4333-8333-333333333333")  # same org, different workspace
ORG_B = UUID("44444444-4444-4444-8444-444444444444")
WS_B = UUID("55555555-5555-4555-8555-555555555555")
USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _clear_all_stores() -> None:
    cap_store._repos.clear()
    cap_store._binding_repos.clear()
    cap_store._version_managers.clear()
    cap_store._provider_versions.clear()
    cap_store._cap_versions.clear()
    cap_store._bindings.clear()
    cap_store._provider_tenants.clear()
    cap_store._cap_version_tenants.clear()


@pytest.fixture(autouse=True)
def _isolated_stores() -> Generator[None]:
    _clear_all_stores()
    yield
    _clear_all_stores()


# ── Actors ─────────────────────────────────────────────────────────────


def _actor_a() -> ActorContext:
    """Owner of the seeded resources: ORG_A / WS_A."""
    return ActorContext(principal_id=USER_A, organization_id=ORG_A, workspace_id=WS_A)


def _actor_a_ws_a2() -> ActorContext:
    """Same org as the resources, different workspace (cross-workspace attacker)."""
    return ActorContext(principal_id=USER_B, organization_id=ORG_A, workspace_id=WS_A2)


def _actor_b() -> ActorContext:
    """Different org entirely (cross-org attacker)."""
    return ActorContext(principal_id=USER_B, organization_id=ORG_B, workspace_id=WS_B)


def _role_actor(name: str, org: UUID = ORG_A, ws: UUID = WS_A) -> ActorContext:
    """Actor with a single role binding; scope follows the frozen matrix
    (src/zhiwei/policy/roles.py: security_admin is org-scoped, workspace_admin
    and agent_builder are workspace-scoped, member is tested workspace-scoped)."""
    if name == "security_admin":
        binding = ActorRoleBinding(
            name=name, scope="org", organization_id=org, workspace_id=None
        )
    else:
        binding = ActorRoleBinding(
            name=name, scope="workspace", organization_id=org, workspace_id=ws
        )
    return ActorContext(
        principal_id=USER_B,
        organization_id=org,
        workspace_id=ws,
        role_bindings=(binding,),
    )


# ── App / client helpers ───────────────────────────────────────────────


def _client_for(
    actor: ActorContext, factory: Callable[..., object]
) -> TestClient:
    app = FastAPI()
    if factory is create_capabilities_router:
        # F-R9-01：生命周期动作为信任根，工厂强制 PEP（FakeOPA 只钉 wiring，
        # 角色语义由 rego 套件钉死）
        app.include_router(
            factory(
                actor_dependency=lambda: actor,  # type: ignore[arg-type]
                policy_enforcer=FakePolicyEnforcer(allow=True),
            )
        )
    else:
        app.include_router(factory(actor_dependency=lambda: actor))  # type: ignore[arg-type]
    # raise_server_exceptions=False: knowledge.py currently escapes an unhandled
    # ObjectNotFoundError for cross-workspace reads (500). We must observe that
    # as a status code and fail the tenancy assertion, not error the test.
    return TestClient(app, raise_server_exceptions=False)


# ── Seed helpers ───────────────────────────────────────────────────────


def _seed_provider_with_version(
    name: str = "idor-victim",
    org: UUID = ORG_A,
    ws: UUID = WS_A,
) -> tuple[UUID, UUID]:
    provider = ProviderVersion(
        id=uuid4(),
        provider_id=uuid4(),
        name=name,
        version=1,
        status=CapabilityStatus.DISCOVERED,
        risk_level=RiskLevel.LOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    cap_store.store_provider(provider, organization_id=org, workspace_id=ws)
    cap_version = CapabilityVersion(
        id=uuid4(),
        capability_type="provider",
        name=name,
        version=1,
        status=CapabilityStatus.DISCOVERED,
        risk_level=RiskLevel.LOW,
        content_digest=provider.content_digest,
        test_digest="",
        metadata={"provider_version_id": str(provider.id)},
        created_at=_NOW,
        updated_at=_NOW,
    )
    cap_store.store_cap_version(cap_version, organization_id=org, workspace_id=ws)
    return provider.id, cap_version.id




# ══════════════════════════════ Capabilities ═══════════════════════════


class TestCapabilitiesIdor:
    """capabilities.py has no tenant predicate on any provider/version path."""

    def test_get_provider_cross_org_is_denied(self) -> None:
        """CURRENT: GET returns 200 with the full org B record
        (capabilities.py:312-334 — 404 only when the id is absent).
        POST-FIX: 403/404, no existence leak."""
        provider_id, _ = _seed_provider_with_version()
        client = _client_for(_actor_b(), create_capabilities_router)
        resp = client.get(f"/api/v1/capabilities/providers/{provider_id}")
        assert resp.status_code in (403, 404), (
            f"cross-org provider read must be denied; got {resp.status_code}: {resp.text}"
        )

    def test_provider_action_cross_org_is_denied(self) -> None:
        """CURRENT: POST .../actions as org B returns 200 and flips the victim's
        provider status (capabilities.py:336-391 — no tenant predicate; direct
        write at :370-373). POST-FIX: 403/404, status unchanged."""
        provider_id, _ = _seed_provider_with_version()
        client = _client_for(_actor_b(), create_capabilities_router)
        resp = client.post(
            f"/api/v1/capabilities/providers/{provider_id}/actions",
            json={"action": "admit"},
        )
        assert resp.status_code in (403, 404), (
            f"cross-org lifecycle action must be denied; got {resp.status_code}: {resp.text}"
        )
        stored = cap_store.get_provider(
            provider_id, organization_id=ORG_A, workspace_id=WS_A
        )
        assert stored is not None and stored.status == CapabilityStatus.DISCOVERED

    def test_list_providers_does_not_leak_other_org(self) -> None:
        """CURRENT: GET /providers returns org B's provider inside actor A's list
        (capabilities.py:192-193 returns the global dict values, ignoring ctx).
        POST-FIX: only the actor's own tenant rows; org B ids absent."""
        provider_a, _ = _seed_provider_with_version(name="own-provider")
        provider_b, _ = _seed_provider_with_version(
            name="foreign-provider", org=ORG_B, ws=WS_B
        )
        client = _client_for(_actor_a(), create_capabilities_router)
        resp = client.get("/api/v1/capabilities/providers")
        assert resp.status_code == 200
        listed = {row["id"] for row in resp.json()}
        assert str(provider_a) in listed
        assert str(provider_b) not in listed, (
            f"list leaks cross-org providers: {sorted(listed)}"
        )

    def test_list_versions_does_not_leak_other_org(self) -> None:
        """CURRENT: GET /versions returns every capability version in the process
        (capabilities.py:393-412, line 397 iterates the global dict).
        POST-FIX: org B's version absent."""
        _, version_a = _seed_provider_with_version(name="own-versions")
        _, version_b = _seed_provider_with_version(
            name="foreign-versions", org=ORG_B, ws=WS_B
        )
        client = _client_for(_actor_a(), create_capabilities_router)
        resp = client.get("/api/v1/capabilities/versions")
        assert resp.status_code == 200
        listed = {row["id"] for row in resp.json()}
        assert str(version_a) in listed
        assert str(version_b) not in listed, (
            f"version list leaks cross-org rows: {sorted(listed)}"
        )

    def test_get_version_cross_org_is_denied(self) -> None:
        """CURRENT: GET returns 200 with the full org B version
        (capabilities.py:414-436 — no tenant predicate). POST-FIX: 403/404."""
        _, version_id = _seed_provider_with_version()
        client = _client_for(_actor_b(), create_capabilities_router)
        resp = client.get(f"/api/v1/capabilities/versions/{version_id}")
        assert resp.status_code in (403, 404), (
            f"cross-org version read must be denied; got {resp.status_code}: {resp.text}"
        )
