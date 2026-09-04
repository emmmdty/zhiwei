"""S7-T6 contract tests: Memory Center API.

Covers:
- User views own + visible team/case memory
- Filter by source/type/status
- Confirm/correct/resolve/revoke/delete/export
- Team confirm only by Steward
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhiwei.api.memory import (
    _store,
    create_memory_router,
)
from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.domain import ActorContext, ActorRoleBinding, PrincipalKind
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
    SourceRef,
)

# ── Fixtures ───────────────────────────────────────────────────────────


_ORG_ID = UUID("11111111-1111-4111-8111-111111111111")
_WS_ID = UUID("22222222-2222-4222-8222-222222222222")
_USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_USER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


def _make_record(
    *,
    key: str = "editor.vim_mode",
    subject: str = "vim keybindings",
    canonical_value: str = "enabled",
    scope: MemoryScope = MemoryScope.USER,
    scope_subject_id: UUID = _USER_A,
    mem_type: MemoryType = MemoryType.PREFERENCE,
    sensitivity: SensitivityLevel = SensitivityLevel.LOW,
    status: MemoryStatus = MemoryStatus.CANDIDATE,
    author_ref: UUID = _USER_A,
    organization_id: UUID = _ORG_ID,
    workspace_id: UUID = _WS_ID,
    source_refs: tuple[SourceRef, ...] = (),
) -> MemoryRecord:
    return MemoryRecord(
        id=new_id(),
        version=1,
        organization_id=organization_id,
        workspace_id=workspace_id,
        scope=scope,
        scope_subject_id=scope_subject_id,
        type=mem_type,
        subject=subject,
        key=key,
        canonical_value=canonical_value,
        source_refs=source_refs,
        observed_at=_NOW,
        confidence=0.8,
        sensitivity=sensitivity,
        status=status,
        author_ref=author_ref,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _actor(
    user_id: UUID = _USER_A,
    org_id: UUID = _ORG_ID,
    ws_id: UUID = _WS_ID,
    is_steward: bool = False,
) -> ActorContext:
    bindings: tuple[ActorRoleBinding, ...] = ()
    if is_steward:
        bindings = (
            ActorRoleBinding(
                name="memory_steward",
                scope="workspace",
                organization_id=org_id,
                workspace_id=ws_id,
            ),
        )
    return ActorContext(
        principal_id=user_id,
        organization_id=org_id,
        workspace_id=ws_id,
        kind=PrincipalKind.USER,
        role_bindings=bindings,
    )


@pytest.fixture(autouse=True)
def _clear_store() -> Generator[None]:
    """Clear the global memory store between tests."""
    _store._records.clear()
    _store._conflict_detector._conflicts.clear()
    yield
    _store._records.clear()
    _store._conflict_detector._conflicts.clear()


def _build_app() -> FastAPI:
    app = FastAPI()

    def _dep() -> ActorContext:
        return _actor()

    app.include_router(create_memory_router(actor_dependency=_dep))
    return app


def _build_app_with_actor(actor: ActorContext) -> FastAPI:
    app = FastAPI()

    def _dep() -> ActorContext:
        return actor

    app.include_router(create_memory_router(actor_dependency=_dep))
    return app


# ── List / Get tests ───────────────────────────────────────────────────


class TestMemoryCenterList:
    def test_list_own_user_memory(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record(scope=MemoryScope.USER, scope_subject_id=_USER_A)
        _store.add(record)

        resp = client.get("/api/v1/memory/records")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == str(record.id)

    def test_list_excludes_other_user_memory(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record(
            scope=MemoryScope.USER,
            scope_subject_id=_USER_B,
            author_ref=_USER_B,
        )
        _store.add(record)

        resp = client.get("/api/v1/memory/records")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_list_team_memory_visible(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record(scope=MemoryScope.TEAM)
        _store.add(record)

        resp = client.get("/api/v1/memory/records")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_list_filter_by_scope(self) -> None:
        app = _build_app()
        client = TestClient(app)

        user_rec = _make_record(scope=MemoryScope.USER, scope_subject_id=_USER_A)
        team_rec = _make_record(scope=MemoryScope.TEAM, key="team.key")
        _store.add(user_rec)
        _store.add(team_rec)

        resp = client.get("/api/v1/memory/records?scope=team")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["scope"] == "team"

    def test_list_filter_by_type(self) -> None:
        app = _build_app()
        client = TestClient(app)

        pref = _make_record(mem_type=MemoryType.PREFERENCE, key="pref.k")
        fact = _make_record(mem_type=MemoryType.FACT, key="fact.k")
        _store.add(pref)
        _store.add(fact)

        resp = client.get("/api/v1/memory/records?type=fact")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["type"] == "fact"

    def test_list_filter_by_status(self) -> None:
        app = _build_app()
        client = TestClient(app)

        cand = _make_record(status=MemoryStatus.CANDIDATE, key="cand.k")
        conf = _make_record(status=MemoryStatus.CONFIRMED, key="conf.k")
        _store.add(cand)
        _store.add(conf)

        resp = client.get("/api/v1/memory/records?status=confirmed")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["status"] == "confirmed"

    def test_list_filter_by_source(self) -> None:
        app = _build_app()
        client = TestClient(app)

        with_source = _make_record(
            key="src.k",
            source_refs=(SourceRef(source_id="s1", source_type="run"),),
        )
        without_source = _make_record(key="nosrc.k", source_refs=())
        _store.add(with_source)
        _store.add(without_source)

        resp = client.get("/api/v1/memory/records?source=run")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_get_record(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record()
        _store.add(record)

        resp = client.get(f"/api/v1/memory/records/{record.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(record.id)

    def test_get_nonexistent(self) -> None:
        app = _build_app()
        client = TestClient(app)

        resp = client.get(f"/api/v1/memory/records/{new_id()}")
        assert resp.status_code == 404


# ── Confirm tests ──────────────────────────────────────────────────────


class TestMemoryCenterConfirm:
    def test_confirm_own_candidate(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record(scope=MemoryScope.USER, scope_subject_id=_USER_A)
        _store.add(record)

        resp = client.post(
            f"/api/v1/memory/records/{record.id}/confirm",
            json={"record_id": str(record.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"
        assert resp.json()["approver_ref"] == str(_USER_A)

    def test_confirm_team_requires_steward(self) -> None:
        regular_actor = _actor(is_steward=False)
        app = _build_app_with_actor(regular_actor)
        client = TestClient(app)

        record = _make_record(scope=MemoryScope.TEAM)
        _store.add(record)

        resp = client.post(
            f"/api/v1/memory/records/{record.id}/confirm",
            json={"record_id": str(record.id)},
        )
        assert resp.status_code == 403

    def test_steward_can_confirm_team(self) -> None:
        steward_actor = _actor(is_steward=True)
        app = _build_app_with_actor(steward_actor)
        client = TestClient(app)

        record = _make_record(scope=MemoryScope.TEAM)
        _store.add(record)

        resp = client.post(
            f"/api/v1/memory/records/{record.id}/confirm",
            json={"record_id": str(record.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

    def test_confirm_non_candidate_fails(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record(status=MemoryStatus.CONFIRMED)
        _store.add(record)

        resp = client.post(
            f"/api/v1/memory/records/{record.id}/confirm",
            json={"record_id": str(record.id)},
        )
        assert resp.status_code == 409


# ── Correct tests ──────────────────────────────────────────────────────


class TestMemoryCenterCorrect:
    def test_correct_supersedes_original(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record(canonical_value="old_value")
        _store.add(record)

        resp = client.post(
            f"/api/v1/memory/records/{record.id}/correct",
            json={
                "record_id": str(record.id),
                "canonical_value": "new_value",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["canonical_value"] == "new_value"
        assert data["status"] == "confirmed"
        assert data["version"] == 2

        # Original should be superseded
        original = _store.get(record.id)
        assert original is not None
        assert original.status == MemoryStatus.SUPERSEDED


# ── Revoke tests ───────────────────────────────────────────────────────


class TestMemoryCenterRevoke:
    def test_revoke_record(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record()
        _store.add(record)

        resp = client.post(
            f"/api/v1/memory/records/{record.id}/revoke",
            json={"record_id": str(record.id), "reason": "outdated"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"
        assert resp.json()["tombstone"] is True

    def test_revoke_terminal_fails(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record(status=MemoryStatus.REVOKED)
        _store.add(record)

        resp = client.post(
            f"/api/v1/memory/records/{record.id}/revoke",
            json={"record_id": str(record.id), "reason": "test"},
        )
        assert resp.status_code == 409


# ── Delete tests ───────────────────────────────────────────────────────


class TestMemoryCenterDelete:
    def test_delete_soft_deletes(self) -> None:
        app = _build_app()
        client = TestClient(app)

        record = _make_record()
        _store.add(record)

        resp = client.post(
            f"/api/v1/memory/records/{record.id}/delete",
            json={"record_id": str(record.id)},
        )
        assert resp.status_code == 204

        # Record should now be revoked with tombstone
        updated = _store.get(record.id)
        assert updated is not None
        assert updated.status == MemoryStatus.REVOKED
        assert updated.tombstone is True


# ── Export tests ───────────────────────────────────────────────────────


class TestMemoryCenterExport:
    def test_export_all(self) -> None:
        app = _build_app()
        client = TestClient(app)

        r1 = _make_record(key="k1")
        r2 = _make_record(key="k2")
        _store.add(r1)
        _store.add(r2)

        resp = client.post(
            "/api/v1/memory/export",
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_export_filter_by_scope(self) -> None:
        app = _build_app()
        client = TestClient(app)

        r1 = _make_record(scope=MemoryScope.USER, scope_subject_id=_USER_A)
        r2 = _make_record(scope=MemoryScope.TEAM, key="team.k")
        _store.add(r1)
        _store.add(r2)

        resp = client.post(
            "/api/v1/memory/export",
            json={"scope": "team"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 1


# ── Stats tests ────────────────────────────────────────────────────────


class TestMemoryCenterStats:
    def test_stats(self) -> None:
        app = _build_app()
        client = TestClient(app)

        r1 = _make_record(scope=MemoryScope.USER, scope_subject_id=_USER_A)
        r2 = _make_record(scope=MemoryScope.TEAM, key="team.k")
        _store.add(r1)
        _store.add(r2)

        resp = client.get("/api/v1/memory/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_records"] == 2
        assert "user" in data["by_scope"]
        assert "team" in data["by_scope"]


# ── Conflict tests ─────────────────────────────────────────────────────


class TestMemoryCenterConflicts:
    def test_list_conflicts_empty(self) -> None:
        app = _build_app()
        client = TestClient(app)

        resp = client.get("/api/v1/memory/conflicts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_resolve_conflict(self) -> None:
        from zhiwei.memory.conflicts import ConflictKind, ConflictRecord

        app = _build_app()
        client = TestClient(app)

        conflict = ConflictRecord(
            conflict_id=new_id(),
            kind=ConflictKind.VALUE,
            record_a_id=new_id(),
            record_b_id=new_id(),
            dedup_key=("a", "b", "c", "d", "e", "f", "g"),
            detected_at=_NOW,
        )
        _store._conflict_detector._conflicts.append(conflict)

        resp = client.post(
            "/api/v1/memory/conflicts/resolve",
            json={"conflict_id": str(conflict.conflict_id)},
        )
        assert resp.status_code == 200
        assert resp.json()["resolved"] is True
