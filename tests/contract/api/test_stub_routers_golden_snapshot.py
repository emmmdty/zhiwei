"""F-R3-01 remediation RED phase — golden JSON snapshots of the in-memory
stub routers (capabilities), mounted in app.py:240-245.

WHY THIS FILE EXISTS
These routers pre-date the tenancy hardening and currently leak cross-tenant
data. This file is a *behavior-change ledger*: it freezes the exact JSON each
representative endpoint returns today, with a fully deterministic seed (fixed
UUIDs, fixed timestamps, digests computed by zhiwei.contracts.canonical).

Entries marked KNOWN-LEAK deliberately capture data the actor must NOT see
after the F-R3-01 fix (org/workspace ownership predicates). Entries marked
GUARD capture deny shapes that are already correct today and must be preserved.

UPDATE PROTOCOL (part of the fix's GREEN commit, not an afterthought):
when F-R3-01 lands, the KNOWN-LEAK snapshots stop matching and this file fails.
That failure is the ledger doing its job: regenerate those goldens in the same
commit and let the diff document exactly which fields stopped leaking. GUARD
entries must not change.

P2b note: the memory router left this ledger when it was rewired onto PG +
PEP (F-R6-02/03) — its GUARD shapes (list visibility, cross-org 404) moved to
tests/integration/memory/test_memory_center_api.py as regression anchors. The
connections and knowledge routers followed (PG + PEP, F-R6-03): their GUARD
shapes moved to tests/integration/capabilities/test_connections_api.py and
tests/integration/knowledge/test_knowledge_api_pg.py respectively.

No snapshot framework is used (none exists in the repo; no new dependencies
are allowed): goldens are inline literals compared with plain dict equality,
so failures print via _assert_snapshot's sorted-JSON diff.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from uuid import UUID

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
from zhiwei.identity.domain import ActorContext

# ── Fixed world (repo convention: guessable UUIDs, tests/security/tenancy/test_idor.py) ──

_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)

ORG_A = UUID("11111111-1111-4111-8111-111111111111")
WS_A = UUID("22222222-2222-4222-8222-222222222222")
WS_A2 = UUID("33333333-3333-4333-8333-333333333333")  # second workspace, same org
ORG_B = UUID("44444444-4444-4444-8444-444444444444")
WS_B = UUID("55555555-5555-4555-8555-555555555555")
USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

PROV_A1 = UUID("c0a00000-0000-4000-8000-000000000001")
PROV_A1_PID = UUID("c0a00000-0000-4000-8000-000000000002")
PROV_B1 = UUID("c0b00000-0000-4000-8000-000000000001")
PROV_B1_PID = UUID("c0b00000-0000-4000-8000-000000000002")
CAP_A1 = UUID("d0a00000-0000-4000-8000-000000000001")
CAP_B1 = UUID("d0b00000-0000-4000-8000-000000000001")
KS_WS_A = UUID("f0a00000-0000-4000-8000-000000000001")
KS_WS_A2 = UUID("f0a00000-0000-4000-8000-000000000002")
KS_ORG_B = UUID("f0b00000-0000-4000-8000-000000000001")
CONN_A = UUID("8a000000-0000-4000-8000-000000000001")
CONN_A2 = UUID("8a000000-0000-4000-8000-000000000002")
CONN_B = UUID("8b000000-0000-4000-8000-000000000001")
CONN_PROV_REF = UUID("9a000000-0000-4000-8000-000000000001")

# sha256(canonical_json({"tools": [{"name": "echo"}]})) — verified against
# zhiwei.contracts.canonical.digest_bytes; stable for this fixed content.
_PROVIDER_DIGEST = "sha256:3549b5ea394c1662cd72ee050cf420710dafd91c8fb2edee525909fc36a8ef6a"
# Connection.compute_fingerprint() for the fixed seeds below (verified).
_FP_CONN_A = "sha256:7bc17e302f33b39c361c08c4dc9e77b3c0bf0aa3b977d8f3449518de99cd3060"


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


# ── Seed helpers (domain objects with fixed ids/timestamps; same shapes the
#    routers themselves persist via their POST endpoints) ──────────────────


def _seed_provider(
    pid: UUID,
    ppid: UUID,
    name: str,
    org: UUID,
    ws: UUID,
) -> ProviderVersion:
    provider = ProviderVersion(
        id=pid,
        provider_id=ppid,
        name=name,
        version=1,
        description=f"golden {name}",
        status=CapabilityStatus.DISCOVERED,
        classification="PUBLIC",
        source_url=None,
        content={"tools": [{"name": "echo"}]},
        metadata={},
        risk_level=RiskLevel.LOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    cap_store.store_provider(provider, organization_id=org, workspace_id=ws)
    return provider


def _seed_cap_version(
    vid: UUID,
    name: str,
    provider_version_id: UUID,
    org: UUID,
    ws: UUID,
) -> None:
    cap_store.store_cap_version(
        CapabilityVersion(
            id=vid,
            capability_type="provider",
            name=name,
            version=1,
            status=CapabilityStatus.DISCOVERED,
            risk_level=RiskLevel.LOW,
            content_digest=_PROVIDER_DIGEST,
            test_digest="",
            metadata={"provider_version_id": str(provider_version_id)},
            created_at=_NOW,
            updated_at=_NOW,
        ),
        organization_id=org,
        workspace_id=ws,
    )




def _seed_world() -> None:
    """Seed all stores; org-A rows are actor A's, org-B rows must be invisible."""
    _seed_provider(PROV_A1, PROV_A1_PID, "golden-provider-a", ORG_A, WS_A)
    _seed_provider(PROV_B1, PROV_B1_PID, "golden-provider-b", ORG_B, WS_B)
    _seed_cap_version(CAP_A1, "golden-provider-a", PROV_A1, ORG_A, WS_A)
    _seed_cap_version(CAP_B1, "golden-provider-b", PROV_B1, ORG_B, WS_B)


def _actor_a_app() -> TestClient:
    """Fixed actor: USER_A in ORG_A / WS_A — the victim's own context."""
    actor = ActorContext(principal_id=USER_A, organization_id=ORG_A, workspace_id=WS_A)
    app = FastAPI()
    app.include_router(
        create_capabilities_router(
            actor_dependency=lambda: actor, policy_enforcer=FakePolicyEnforcer(allow=True)
        )
    )
    return TestClient(app)


def _assert_snapshot(label: str, actual: object, expected: object) -> None:
    import json

    assert actual == expected, (
        f"golden snapshot mismatch: {label}\n"
        f"--- actual ---\n{json.dumps(actual, indent=1, sort_keys=True)}\n"
        f"--- expected golden ---\n{json.dumps(expected, indent=1, sort_keys=True)}"
    )


# ═════════════════════════════ Capabilities ═════════════════════════════


class TestGoldenCapabilities:
    def test_list_providers_actor_a(self) -> None:
        """F-R3-01 GREEN 再生：org B 的 provider 不再泄漏进 actor A 的列表
        （RED 期此快照捕获泄漏行为，GREEN 提交的 diff 即泄漏消除审计线）。"""
        _seed_world()
        resp = _actor_a_app().get("/api/v1/capabilities/providers")
        assert resp.status_code == 200
        _assert_snapshot(
            "GET /api/v1/capabilities/providers (actor A)",
            resp.json(),
            [
                {
                    "id": str(PROV_A1),
                    "provider_id": str(PROV_A1_PID),
                    "name": "golden-provider-a",
                    "version": 1,
                    "description": "golden golden-provider-a",
                    "status": "discovered",
                    "classification": "PUBLIC",
                    "source_url": None,
                    "risk_level": "low",
                    "content_digest": _PROVIDER_DIGEST,
                },
            ],
        )

    def test_get_provider_cross_org(self) -> None:
        """F-R3-01 GREEN 再生：跨 org provider 读为 404（与不存在同形，
        不泄漏存在性）。"""
        _seed_world()
        resp = _actor_a_app().get(f"/api/v1/capabilities/providers/{PROV_B1}")
        assert resp.status_code == 404
        _assert_snapshot(
            f"GET /api/v1/capabilities/providers/{PROV_B1} (actor A, cross-org)",
            resp.json(),
            {"detail": "provider not found"},
        )

    def test_list_versions_actor_a(self) -> None:
        """F-R3-01 GREEN 再生：org B 的 capability version 不再泄漏。"""
        _seed_world()
        resp = _actor_a_app().get("/api/v1/capabilities/versions")
        assert resp.status_code == 200
        _assert_snapshot(
            "GET /api/v1/capabilities/versions (actor A)",
            resp.json(),
            [
                {
                    "id": str(CAP_A1),
                    "capability_type": "provider",
                    "name": "golden-provider-a",
                    "version": 1,
                    "status": "discovered",
                    "risk_level": "low",
                    "content_digest": _PROVIDER_DIGEST,
                    "test_digest": "",
                    "parent_id": None,
                    "metadata": {"provider_version_id": str(PROV_A1)},
                },
            ],
        )

    def test_get_version_cross_org(self) -> None:
        """F-R3-01 GREEN 再生：跨 org version 读为 404。"""
        _seed_world()
        resp = _actor_a_app().get(f"/api/v1/capabilities/versions/{CAP_B1}")
        assert resp.status_code == 404
        _assert_snapshot(
            f"GET /api/v1/capabilities/versions/{CAP_B1} (actor A, cross-org)",
            resp.json(),
            {"detail": "capability version not found"},
        )

    def test_unknown_provider_404_shape(self) -> None:
        """GUARD: deny shape for unknown provider — must not change."""
        _seed_world()
        resp = _actor_a_app().get(
            "/api/v1/capabilities/providers/00000000-0000-4000-8000-000000000000"
        )
        assert resp.status_code == 404
        _assert_snapshot("GET unknown provider", resp.json(), {"detail": "provider not found"})

    def test_unknown_version_404_shape(self) -> None:
        """GUARD: deny shape for unknown capability version — must not change."""
        _seed_world()
        resp = _actor_a_app().get(
            "/api/v1/capabilities/versions/00000000-0000-4000-8000-000000000000"
        )
        assert resp.status_code == 404
        _assert_snapshot(
            "GET unknown version", resp.json(), {"detail": "capability version not found"}
        )
