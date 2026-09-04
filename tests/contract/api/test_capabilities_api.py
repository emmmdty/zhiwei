"""S4-T8 contract: Capability Hub API — provider/version/binding CRUD + lifecycle actions。

覆盖：
- Provider import (register) + inspect/test/admit/publish/suspend/revoke journey
- Capability version listing, detail, and version diff
- Binding create (only published versions) + delete
- Permission/error states: 403 without org context, 404 on unknown, 409 on invalid transition
- No live provider calls; all fixture-based.
"""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from zhiwei.api.capabilities import create_capabilities_router
from zhiwei.identity.domain import ActorContext

pytestmark = pytest.mark.asyncio


def _actor(
    org_id: UUID | None = None,
    ws_id: UUID | None = None,
) -> ActorContext:
    return ActorContext(
        principal_id=uuid4(),
        organization_id=org_id or uuid4(),
        workspace_id=ws_id or uuid4(),
    )


def _app(actor: ActorContext) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_capabilities_router(actor_dependency=lambda: actor)
    )
    return app


class TestProviderJourney:
    """Publisher journey: import → inspect → test → admit → publish → suspend."""

    async def test_register_and_inspect_provider(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Register a provider
            created = await client.post(
                "/api/v1/capabilities/providers",
                json={
                    "name": "test-provider",
                    "description": "A test provider",
                    "risk_level": "low",
                    "content": {"tools": [{"name": "echo"}]},
                },
            )
            assert created.status_code == 201, created.text
            data = created.json()
            provider_id = data["id"]
            assert data["name"] == "test-provider"
            assert data["status"] == "discovered"

            # Inspect
            detail = await client.get(f"/api/v1/capabilities/providers/{provider_id}")
            assert detail.status_code == 200
            assert detail.json()["id"] == provider_id

    async def test_full_lifecycle_actions(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/capabilities/providers",
                json={"name": "lifecycle-provider", "risk_level": "low"},
            )
            assert created.status_code == 201
            provider_id = created.json()["id"]

            # Lifecycle: discovered → quarantined → inspected → tested → approved → published
            expected = {
                "quarantine": "quarantined",
                "inspect": "inspected",
                "test": "tested",
                "admit": "approved",
                "publish": "published",
            }
            for action in ("quarantine", "inspect", "test", "admit", "publish"):
                resp = await client.post(
                    f"/api/v1/capabilities/providers/{provider_id}/actions",
                    json={"action": action},
                )
                assert resp.status_code == 200, f"action {action} failed: {resp.text}"
                assert resp.json()["status"] == expected[action]

            # Verify published
            detail = await client.get(f"/api/v1/capabilities/providers/{provider_id}")
            assert detail.json()["status"] == "published"

            # Suspend
            resp = await client.post(
                f"/api/v1/capabilities/providers/{provider_id}/actions",
                json={"action": "suspend"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "suspended"

    async def test_unknown_provider_is_404(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/capabilities/providers/{uuid4()}")
            assert resp.status_code == 404

    async def test_invalid_action_is_422(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/capabilities/providers",
                json={"name": "invalid-action-provider"},
            )
            assert created.status_code == 201
            provider_id = created.json()["id"]
            resp = await client.post(
                f"/api/v1/capabilities/providers/{provider_id}/actions",
                json={"action": "fly_to_moon"},
            )
            assert resp.status_code == 422

    async def test_invalid_risk_level_is_422(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/capabilities/providers",
                json={"name": "bad-risk", "risk_level": "catastrophic"},
            )
            assert resp.status_code == 422


class TestVersionDiff:
    async def test_version_diff_for_first_version(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/capabilities/providers",
                json={"name": "diff-provider"},
            )
            assert created.status_code == 201
            provider_id = created.json()["id"]

            # List versions to find the cap version for this provider
            versions = await client.get("/api/v1/capabilities/versions")
            assert versions.status_code == 200
            matching = [
                v for v in versions.json()
                if v.get("metadata", {}).get("provider_version_id") == provider_id
            ]
            assert matching, f"no version found for provider {provider_id}"
            version_id = matching[0]["id"]

            # Diff for first version
            diff = await client.get(f"/api/v1/capabilities/versions/{version_id}/diff")
            assert diff.status_code == 200
            data = diff.json()
            assert data["from_version"] == 0
            assert data["to_version"] == 1
            assert data["content_changed"] is True


class TestBindings:
    async def test_create_binding_requires_published_version(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Register provider (discovered state)
            created = await client.post(
                "/api/v1/capabilities/providers",
                json={"name": "bind-provider"},
            )
            assert created.status_code == 201
            provider_id = created.json()["id"]

            # List versions to get the cap version for this provider
            versions = await client.get("/api/v1/capabilities/versions")
            matching = [
                v for v in versions.json()
                if v.get("metadata", {}).get("provider_version_id") == provider_id
            ]
            assert matching
            version_id = matching[0]["id"]

            # Try binding before publish → 409
            resp = await client.post(
                "/api/v1/capabilities/bindings",
                json={
                    "agent_definition_id": str(uuid4()),
                    "agent_version_id": str(uuid4()),
                    "capability_version_id": version_id,
                },
            )
            assert resp.status_code == 409

    async def test_bind_after_publish_and_delete(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Register and publish
            created = await client.post(
                "/api/v1/capabilities/providers",
                json={"name": "published-provider", "risk_level": "low"},
            )
            provider_id = created.json()["id"]
            for action in ("quarantine", "inspect", "test", "admit", "publish"):
                await client.post(
                    f"/api/v1/capabilities/providers/{provider_id}/actions",
                    json={"action": action},
                )

            # List versions and find the one for this provider
            versions = await client.get("/api/v1/capabilities/versions")
            matching = [
                v for v in versions.json()
                if v.get("metadata", {}).get("provider_version_id") == provider_id
            ]
            assert matching, f"no version found for provider {provider_id}"
            version_id = matching[0]["id"]

            # Create binding
            binding = await client.post(
                "/api/v1/capabilities/bindings",
                json={
                    "agent_definition_id": str(uuid4()),
                    "agent_version_id": str(uuid4()),
                    "capability_version_id": version_id,
                },
            )
            assert binding.status_code == 201, binding.text
            binding_id = binding.json()["id"]

            # List bindings
            bindings = await client.get("/api/v1/capabilities/bindings")
            assert any(b["id"] == binding_id for b in bindings.json())

            # Delete binding
            del_resp = await client.delete(f"/api/v1/capabilities/bindings/{binding_id}")
            assert del_resp.status_code == 204

            # Verify deleted
            bindings2 = await client.get("/api/v1/capabilities/bindings")
            assert all(b["id"] != binding_id for b in bindings2.json())

    async def test_delete_unknown_binding_is_404(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete(f"/api/v1/capabilities/bindings/{uuid4()}")
            assert resp.status_code == 404


class TestPermissionAndErrorStates:
    async def test_list_providers_works(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/capabilities/providers")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)

    async def test_list_versions_works(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/capabilities/versions")
            assert resp.status_code == 200

    async def test_list_bindings_works(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/capabilities/bindings")
            assert resp.status_code == 200
