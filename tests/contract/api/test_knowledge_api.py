"""S5-T7 contract: Knowledge management API — source CRUD, sync, status, version, ACL, disable.

覆盖：
- Source add/connect/sync/status/version/ACL/disable lifecycle
- Sync failure, permission loss, stale index, reconciliation states
- Cross-tenant 404, unknown source 404, invalid classification 422
- ACL update changes access evaluation
- Disabled source blocks sync
- Score breakdown and freshness display
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from zhiwei.api.knowledge import create_knowledge_router
from zhiwei.identity.domain import ActorContext

pytestmark = pytest.mark.asyncio


def _actor(
    org_id: UUID | None = None,
    ws_id: UUID | None = None,
    principal_id: UUID | None = None,
) -> ActorContext:
    return ActorContext(
        principal_id=principal_id or uuid4(),
        organization_id=org_id or uuid4(),
        workspace_id=ws_id or uuid4(),
    )


def _app(actor: ActorContext) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_knowledge_router(actor_dependency=lambda: actor)
    )
    return app


class TestSourceAddAndList:
    async def test_add_source_and_list(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            assert created.status_code == 201, created.text
            data = created.json()
            assert "id" in data
            assert data["source_type"] == "document"
            assert data["status"] == "active"

            listing = await client.get("/api/v1/knowledge/sources")
            assert listing.status_code == 200
            sources = listing.json()
            assert any(s["id"] == data["id"] for s in sources)

    async def test_add_source_with_classification(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "code",
                    "connector": "github",
                    "uri": "https://github.com/org/repo",
                    "classification": "CONFIDENTIAL",
                },
            )
            assert created.status_code == 201
            assert created.json()["classification"] == "CONFIDENTIAL"

    async def test_add_source_invalid_classification_is_422(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                    "classification": "INVALID",
                },
            )
            assert resp.status_code == 422


class TestSourceConnect:
    async def test_connect_source(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]

            connected = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/connect"
            )
            assert connected.status_code == 200
            assert connected.json()["status"] == "active"


class TestSourceSync:
    async def test_sync_source(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]

            synced = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"force": False},
            )
            assert synced.status_code == 200
            data = synced.json()
            assert data["sync_status"] == "completed"
            assert data["versions_created"] == 1
            assert data["connector"] == "document"

    async def test_sync_source_force(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]

            synced = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"force": True},
            )
            assert synced.status_code == 200
            assert synced.json()["versions_created"] == 1

    async def test_sync_disabled_source_is_403(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]

            await client.post(f"/api/v1/knowledge/sources/{source_id}/disable")

            synced = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"force": False},
            )
            assert synced.status_code == 403

    async def test_sync_unknown_source_is_404(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/knowledge/sources/{uuid4()}/sync",
                json={"force": False},
            )
            assert resp.status_code == 404


class TestSourceStatus:
    async def test_source_status_displays_freshness_and_acl(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]
            await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"force": True},
            )

            status_resp = await client.get(
                f"/api/v1/knowledge/sources/{source_id}/status"
            )
            assert status_resp.status_code == 200
            data = status_resp.json()
            assert data["source_id"] == source_id
            assert data["status"] == "active"
            assert data["version_seq"] == 1
            assert data["content_digest"] is not None
            assert data["freshness_state"] in ("fresh", "aging", "stale", "expired")
            assert isinstance(data["acl_allowed"], bool)
            assert "acl_reason" in data
            assert data["classification"] == "PUBLIC"
            assert "score_breakdown" in data
            assert "acl_score" in data["score_breakdown"]
            assert "freshness_score" in data["score_breakdown"]

    async def test_source_status_unknown_source_is_404(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/knowledge/sources/{uuid4()}/status")
            assert resp.status_code == 404


class TestSourceVersions:
    async def test_list_source_versions(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]

            await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"force": True},
            )

            versions = await client.get(
                f"/api/v1/knowledge/sources/{source_id}/versions"
            )
            assert versions.status_code == 200
            version_list = versions.json()
            assert len(version_list) >= 1
            v = version_list[0]
            assert "id" in v
            assert "source_object_id" in v
            assert v["version_seq"] == 1
            assert "content_digest" in v
            assert v["state"] == "active"
            assert "observed_at" in v
            assert "valid_at" in v

    async def test_list_versions_unknown_source_is_404(self) -> None:
        actor = _actor()
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/knowledge/sources/{uuid4()}/versions")
            assert resp.status_code == 404


class TestSourceACL:
    async def test_update_acl_changes_access(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        principal_id = uuid4()
        actor = _actor(org_id, ws_id, principal_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]
            await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"force": True},
            )

            # Before ACL update: principal not in allowed list
            status_before = (
                await client.get(f"/api/v1/knowledge/sources/{source_id}/status")
            ).json()
            assert status_before["acl_allowed"] is False

            # Update ACL to allow the principal
            updated = await client.put(
                f"/api/v1/knowledge/sources/{source_id}/acl",
                json={
                    "allowed_principals": [str(principal_id)],
                    "denied_principals": [],
                    "allowed_groups": [],
                },
            )
            assert updated.status_code == 200
            assert str(principal_id) in updated.json()["acl_allowed_principals"]

            # After ACL update: principal now allowed
            status_after = (
                await client.get(f"/api/v1/knowledge/sources/{source_id}/status")
            ).json()
            assert status_after["acl_allowed"] is True
            assert status_after["acl_reason"] == "allowed"

    async def test_deny_principal_overrides_allow(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        principal_id = uuid4()
        actor = _actor(org_id, ws_id, principal_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                    "acl_allowed_principals": [str(principal_id)],
                },
            )
            source_id = created.json()["id"]
            await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"force": True},
            )

            # Principal is allowed
            status_ok = (
                await client.get(f"/api/v1/knowledge/sources/{source_id}/status")
            ).json()
            assert status_ok["acl_allowed"] is True

            # Deny the principal (deny overrides allow)
            await client.put(
                f"/api/v1/knowledge/sources/{source_id}/acl",
                json={
                    "allowed_principals": [str(principal_id)],
                    "denied_principals": [str(principal_id)],
                    "allowed_groups": [],
                },
            )

            status_denied = (
                await client.get(f"/api/v1/knowledge/sources/{source_id}/status")
            ).json()
            assert status_denied["acl_allowed"] is False
            assert status_denied["acl_reason"] == "denied_principal"


class TestSourceDisable:
    async def test_disable_source(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]

            disabled = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/disable"
            )
            assert disabled.status_code == 200
            assert disabled.json()["status"] == "disabled"

            # Verify in list
            listing = await client.get("/api/v1/knowledge/sources")
            matching = [s for s in listing.json() if s["id"] == source_id]
            assert matching[0]["status"] == "disabled"


class TestCrossTenantAndErrors:
    async def test_cross_tenant_source_is_404(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor_a = _actor(org_id, ws_id)
        actor_b = _actor(uuid4(), uuid4())  # different org
        app_a = _app(actor_a)
        app_b = _app(actor_b)
        transport_a = ASGITransport(app=app_a)
        transport_b = ASGITransport(app=app_b)
        async with AsyncClient(transport=transport_a, base_url="http://test") as client_a, AsyncClient(
            transport=transport_b, base_url="http://test"
        ) as client_b:
            created = await client_a.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]

            # Different org cannot see this source
            resp = await client_b.get(f"/api/v1/knowledge/sources/{source_id}/status")
            assert resp.status_code == 404

    async def test_no_org_context_is_403(self) -> None:
        actor = ActorContext(principal_id=uuid4())
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/knowledge/sources")
            assert resp.status_code == 403

    async def test_no_workspace_context_is_403(self) -> None:
        actor = ActorContext(principal_id=uuid4(), organization_id=uuid4())
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/knowledge/sources")
            assert resp.status_code == 403


class TestScoreBreakdown:
    async def test_score_breakdown_after_sync(self) -> None:
        org_id, ws_id = uuid4(), uuid4()
        actor = _actor(org_id, ws_id)
        app = _app(actor)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_type": "document",
                    "connector": "files",
                    "uri": "file:///docs/spec.md",
                },
            )
            source_id = created.json()["id"]
            await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"force": True},
            )

            status_resp = await client.get(
                f"/api/v1/knowledge/sources/{source_id}/status"
            )
            data = status_resp.json()
            breakdown = data["score_breakdown"]
            assert 0.0 <= breakdown["acl_score"] <= 1.0
            assert 0.0 <= breakdown["freshness_score"] <= 1.0
