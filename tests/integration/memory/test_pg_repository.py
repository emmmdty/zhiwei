"""S7 GREEN 实现级验证：memory PG 生命周期与同事务落账（真实 PG）。

test_pg_lifecycle_events.py 冻结契约（repositories/events 模块存在）的实现级验证，
覆盖 plan Task 1/2 的生产行为：

- ADR-009 写入去重：同键 candidate 合并证据不新建记录；
- 状态机 candidate → confirm → supersede / expire（tombstone），域层状态机复用；
- lifecycle ledger + audit chain 与记录转移同事务；
- Memory Activity 持久化路径：candidate/refusal canonical event 与记录同事务，
  失败整体回滚（无 Run 不落记录——同事务契约的直接证据）；
- tenant 隔离：跨 org 上下文不可见、跨租户记录拒绝。

事实源：specs/s7-memory.md §3/§4/§7、ADR-009、plan Task 1/2。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.canonical import digest
from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.domain import PrincipalKind
from zhiwei.memory.candidates import DedupKey
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    RetentionPolicy,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.events import (
    ACTION_CANDIDATE_CONFIRMED,
    ACTION_CANDIDATE_EXPIRED,
    ACTION_CANDIDATE_MERGED,
    ACTION_CANDIDATE_RECORDED,
    MemoryLifecycleLedger,
)
from zhiwei.memory.repositories import PgMemoryRepository
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import (
    AuditEvent,
    CanonicalEvent,
    MemoryLifecycleEventRow,
    MemoryRecordRow,
    OutboxMessage,
)
from zhiwei.persistence.tenant import TenantContext, TenantScopeError, tenant_session
from zhiwei.workflows.activities.memory import (
    MemoryActivityInput,
    build_persistent_memory_activity,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_SQLALCHEMY_URL = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)

_USER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_STEWARD = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_SUBJECT = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_T0 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Tenant:
    organization_id: UUID
    workspace_id: UUID

    @property
    def context(self) -> TenantContext:
        return TenantContext(
            organization_id=self.organization_id, workspace_id=self.workspace_id
        )


async def _insert_tenant_rows(organization_id: UUID, workspace_id: UUID) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) "
            "VALUES ($1, 'active', 1)",
            organization_id,
        )
        await connection.execute(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) "
            "VALUES ($1, $2, 'memory-pg-test', 1)",
            workspace_id,
            organization_id,
        )
    finally:
        await connection.close()


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    url = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def tenant() -> _Tenant:
    tenant_id = _Tenant(organization_id=uuid4(), workspace_id=uuid4())
    await _insert_tenant_rows(tenant_id.organization_id, tenant_id.workspace_id)
    return tenant_id


def _record(
    tenant: _Tenant,
    *,
    key: str = "editor",
    canonical_value: str = "vim",
    mem_type: MemoryType = MemoryType.PREFERENCE,
    record_id: UUID | None = None,
    source_id: str = "run-1",
    confidence: float = 0.5,
    observed_at: datetime = _T0,
    created_at: datetime = _T0,
    status: MemoryStatus = MemoryStatus.CANDIDATE,
) -> MemoryRecord:
    return MemoryRecord(
        id=record_id or new_id(),
        version=1,
        organization_id=tenant.organization_id,
        workspace_id=tenant.workspace_id,
        scope=MemoryScope.USER,
        scope_subject_id=_SUBJECT,
        type=mem_type,
        subject="editor",
        key=key,
        canonical_value=canonical_value,
        source_refs=(SourceRef(source_id=source_id, source_type="run"),),
        observed_at=observed_at,
        confidence=confidence,
        sensitivity=SensitivityLevel.LOW,
        status=status,
        author_ref=_USER,
        created_at=created_at,
        updated_at=created_at,
    )


def _memory_dict(tenant: _Tenant, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "organization_id": str(tenant.organization_id),
        "workspace_id": str(tenant.workspace_id),
        "scope": "user",
        "scope_subject_id": str(_SUBJECT),
        "type": "preference",
        "subject": "editor",
        "key": "editor.ide",
        "canonical_value": "vim",
        "sensitivity": "low",
        "created_at": _T0.isoformat(),
        "observed_at": _T0.isoformat(),
    }
    values.update(overrides)
    return values


def _activity_input(
    tenant: _Tenant, run_id: UUID, **memory_overrides: object
) -> MemoryActivityInput:
    return MemoryActivityInput(
        run_id=str(run_id),
        task_id=str(uuid4()),
        attempt_no=1,
        organization_id=str(tenant.organization_id),
        workspace_id=str(tenant.workspace_id),
        principal_id=str(_USER),
        principal_kind=PrincipalKind.USER,
        action="write",
        memory=_memory_dict(tenant, **memory_overrides),
    )


async def _seed_run(tenant: _Tenant) -> UUID:
    run_id = uuid4()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO runs (id, organization_id, workspace_id, status, schema_version) "
            "VALUES ($1, $2, $3, 'running', 1)",
            run_id,
            tenant.organization_id,
            tenant.workspace_id,
        )
    finally:
        await connection.close()
    return run_id


async def _lifecycle_actions(session: AsyncSession, record_id: UUID) -> list[str]:
    stmt = select(MemoryLifecycleEventRow.action).where(
        MemoryLifecycleEventRow.record_id == record_id
    )
    return list((await session.scalars(stmt)).all())


class TestPgRepositoryLifecycle:
    @pytest.mark.asyncio
    async def test_candidate_dedup_merges_evidence_without_new_row(
        self, tenant: _Tenant
    ) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            first = _record(tenant, source_id="run-1", confidence=0.3)
            second = _record(
                tenant,
                record_id=first.id,
                source_id="run-2",
                confidence=0.9,
                observed_at=_T0 + timedelta(hours=1),
            )
            assert second.dedup_hash == first.dedup_hash

            async with tenant_session(sessions, tenant.context) as session:
                ledger = MemoryLifecycleLedger(session, tenant.context)
                repository = PgMemoryRepository(session, tenant.context, ledger=ledger)
                stored_first = await repository.add_candidate(first, now=_T0)
                stored_second = await repository.add_candidate(second, now=_T0)

                assert stored_first.id == first.id
                assert stored_second.id == first.id
                assert stored_second.confidence == 0.9
                assert stored_second.observed_at == _T0 + timedelta(hours=1)
                assert [ref.source_id for ref in stored_second.source_refs] == [
                    "run-1",
                    "run-2",
                ]

                rows = list(
                    (
                        await session.scalars(
                            select(MemoryRecordRow).where(
                                MemoryRecordRow.dedup_hash == first.dedup_hash
                            )
                        )
                    ).all()
                )
                assert len(rows) == 1
                assert sorted(await _lifecycle_actions(session, first.id)) == sorted(
                    [ACTION_CANDIDATE_RECORDED, ACTION_CANDIDATE_MERGED]
                )
                audits = list(
                    (
                        await session.scalars(
                            select(AuditEvent.action).where(
                                AuditEvent.resource_id == first.id
                            )
                        )
                    ).all()
                )
                assert audits == [
                    "memory.candidate.recorded",
                    "memory.candidate.merged",
                ]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_confirm_is_cas_and_idempotent(self, tenant: _Tenant) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            record = _record(tenant, key="confirm-me", canonical_value="team-flow")
            dedup = DedupKey.from_record(record)

            async with tenant_session(sessions, tenant.context) as session:
                repository = PgMemoryRepository(
                    session,
                    tenant.context,
                    ledger=MemoryLifecycleLedger(session, tenant.context),
                )
                await repository.add_candidate(record, now=_T0)
                confirmed = await repository.confirm_candidate(
                    dedup, _STEWARD, now=_T0 + timedelta(minutes=1)
                )
                assert confirmed is not None
                assert confirmed.status is MemoryStatus.CONFIRMED
                assert confirmed.approver_ref == _STEWARD
                assert sorted(await _lifecycle_actions(session, record.id)) == sorted(
                    [ACTION_CANDIDATE_RECORDED, ACTION_CANDIDATE_CONFIRMED]
                )
                # 已确认后再次 confirm：无 candidate 可确认，返回 None（幂等）
                assert await repository.confirm_candidate(dedup, _STEWARD) is None

            async with tenant_session(sessions, tenant.context) as session:
                reloaded = await PgMemoryRepository(
                    session, tenant.context
                ).get_record(dedup)
                assert reloaded is not None
                assert reloaded.status is MemoryStatus.CONFIRMED
                assert reloaded.approver_ref == _STEWARD
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_supersede_records_correction_with_new_version(
        self, tenant: _Tenant
    ) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            original = _record(tenant, key="stack", canonical_value="numpy")
            correction = _record(
                tenant,
                record_id=new_id(),
                key="stack",
                canonical_value="polars",
                status=MemoryStatus.CONFIRMED,
            )

            async with tenant_session(sessions, tenant.context) as session:
                repository = PgMemoryRepository(
                    session,
                    tenant.context,
                    ledger=MemoryLifecycleLedger(session, tenant.context),
                )
                await repository.add_candidate(original, now=_T0)
                superseded, confirmed = await repository.supersede_record(
                    DedupKey.from_record(original), correction, now=_T0
                )
                assert superseded.status is MemoryStatus.SUPERSEDED
                assert superseded.superseded_by == correction.id
                assert confirmed.status is MemoryStatus.CONFIRMED
                assert confirmed.canonical_value == "polars"
                assert sorted(await _lifecycle_actions(session, original.id)) == sorted(
                    [ACTION_CANDIDATE_RECORDED, "record.superseded"]
                )
                assert await _lifecycle_actions(session, correction.id) == [
                    "record.recorded"
                ]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_ttl_expiry_marks_tombstone(self, tenant: _Tenant) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            stale = _record(tenant, key="stale-fact", canonical_value="old")
            fresh = _record(
                tenant,
                key="fresh-fact",
                canonical_value="new",
                created_at=_T0 + timedelta(hours=1),
            )

            async with tenant_session(sessions, tenant.context) as session:
                repository = PgMemoryRepository(
                    session,
                    tenant.context,
                    ledger=MemoryLifecycleLedger(session, tenant.context),
                    retention=RetentionPolicy(candidate_ttl=timedelta(0)),
                )
                await repository.add_candidate(stale, now=_T0)
                await repository.add_candidate(fresh, now=_T0)
                expired = await repository.expire_candidates(_T0 + timedelta(seconds=1))
                assert [r.id for r in expired] == [stale.id]
                assert expired[0].status is MemoryStatus.EXPIRED
                assert expired[0].tombstone is True
                assert sorted(await _lifecycle_actions(session, stale.id)) == sorted(
                    [ACTION_CANDIDATE_RECORDED, ACTION_CANDIDATE_EXPIRED]
                )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_cross_tenant_context_is_denied(self, tenant: _Tenant) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            other = _Tenant(organization_id=uuid4(), workspace_id=uuid4())
            await _insert_tenant_rows(other.organization_id, other.workspace_id)
            record = _record(tenant, key="private", canonical_value="mine")

            async with tenant_session(sessions, tenant.context) as session:
                await PgMemoryRepository(session, tenant.context).add_candidate(
                    record, now=_T0
                )

            async with tenant_session(sessions, other.context) as session:
                repository = PgMemoryRepository(session, other.context)
                assert await repository.get_record(DedupKey.from_record(record)) is None
                with pytest.raises(TenantScopeError):
                    await repository.add_candidate(record, now=_T0)

            org_only = TenantContext(organization_id=tenant.organization_id)
            async with tenant_session(sessions, org_only) as session:
                repository = PgMemoryRepository(session, org_only)
                with pytest.raises(Exception, match="workspace context"):
                    await repository.add_candidate(record, now=_T0)
        finally:
            await engine.dispose()


class TestMemoryActivityPersistence:
    @pytest.mark.asyncio
    async def test_write_commits_record_and_canonical_event_in_one_transaction(
        self, tenant: _Tenant
    ) -> None:
        run_id = await _seed_run(tenant)
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            activity = build_persistent_memory_activity(sessions)
            output = await activity.execute(_activity_input(tenant, run_id))
            assert output.status == "completed"
            assert output.decision == "auto_confirm"

            async with tenant_session(sessions, tenant.context) as session:
                rows = list((await session.scalars(select(MemoryRecordRow))).all())
                assert len(rows) == 1
                assert rows[0].status == "confirmed"
                events = list(
                    (await session.scalars(select(CanonicalEvent.event_type))).all()
                )
                assert events == ["memory.candidate.recorded"]
                outbox = list((await session.scalars(select(OutboxMessage.topic))).all())
                assert outbox == ["canonical.event.committed"]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_refusal_commits_event_without_record(self, tenant: _Tenant) -> None:
        run_id = await _seed_run(tenant)
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            activity = build_persistent_memory_activity(sessions)
            output = await activity.execute(
                _activity_input(
                    tenant,
                    run_id,
                    type="fact",
                    subject="credentials",
                    key="auth.password",
                    canonical_value="hunter2",
                )
            )
            assert output.status == "refused"
            assert output.decision == "forbidden"

            async with tenant_session(sessions, tenant.context) as session:
                assert (await session.scalar(select(MemoryRecordRow.id))) is None
                events = list(
                    (await session.scalars(select(CanonicalEvent.event_type))).all()
                )
                assert events == ["memory.write.refused"]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_missing_run_rolls_back_record_write(self, tenant: _Tenant) -> None:
        """canonical event 失败 → 记录写入整体回滚（同事务契约的直接证据）。"""
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            activity = build_persistent_memory_activity(sessions)
            output = await activity.execute(
                _activity_input(tenant, uuid4())  # 未播种的 Run
            )
            assert output.status == "error"

            async with tenant_session(sessions, tenant.context) as session:
                assert (await session.scalar(select(MemoryRecordRow.id))) is None
                assert (await session.scalar(select(CanonicalEvent.id))) is None
                assert (await session.scalar(select(MemoryLifecycleEventRow.id))) is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_non_uuid_run_id_is_refused(self, tenant: _Tenant) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            activity = build_persistent_memory_activity(sessions)
            output = await activity.execute(
                MemoryActivityInput(
                    run_id="run-1",
                    task_id="task-1",
                    attempt_no=1,
                    organization_id=str(tenant.organization_id),
                    workspace_id=str(tenant.workspace_id),
                    principal_id=str(_USER),
                    principal_kind=PrincipalKind.USER,
                    action="write",
                    memory=_memory_dict(tenant),
                )
            )
            assert output.status == "refused"
            assert output.refusal_reason is not None
            assert "invalid run_id" in output.refusal_reason
        finally:
            await engine.dispose()


class TestTerminalRedaction:
    """F-R6-05（D6 决议）：终态转移事务内覆写 canonical_value 为 digest 占位。

    0012 触发器修改（0024）：canonical_value 仅允许随 active→terminal 同一条
    UPDATE 覆写、且新值必须匹配 redacted:sha256 占位形态（防借道写入任意内
    容）；终态行/活跃行内容不可变守卫保持。"""

    @pytest.mark.asyncio
    async def test_revoke_redacts_canonical_value_in_pg(self, tenant: _Tenant) -> None:
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            record = _record(tenant, key="to-forget", canonical_value="dark secret")
            async with tenant_session(sessions, tenant.context) as session:
                repository = PgMemoryRepository(
                    session, tenant.context, ledger=MemoryLifecycleLedger(session, tenant.context)
                )
                await repository.add_candidate(record, now=_T0)
                revoked = await repository.revoke_record(
                    DedupKey.from_record(record), "user requested deletion", now=_T0
                )
                assert revoked is not None
                row = await session.get(MemoryRecordRow, record.id)
                assert row is not None
                assert row.status == "revoked"
                assert row.tombstone is True
                assert row.canonical_value == f"redacted:{digest('dark secret')}"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_active_row_content_update_still_blocked(
        self, tenant: _Tenant
    ) -> None:
        """活跃行 canonical_value 直接 UPDATE 仍被内容守卫拒绝（既有守卫保持）。"""
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            record = _record(tenant, key="guard-keep", canonical_value="keep me")
            async with tenant_session(sessions, tenant.context) as session:
                repository = PgMemoryRepository(
                    session, tenant.context, ledger=MemoryLifecycleLedger(session, tenant.context)
                )
                await repository.add_candidate(record, now=_T0)
            connection = await asyncpg.connect(ADMIN_DSN)
            try:
                with pytest.raises(asyncpg.PostgresError) as exc_info:
                    await connection.execute(
                        "UPDATE memory_records SET canonical_value = 'hacked' "
                        "WHERE id = $1",
                        record.id,
                    )
                assert "content is immutable" in str(exc_info.value)
            finally:
                await connection.close()
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_terminal_transition_with_placeholder_overwrite_allowed(
        self, tenant: _Tenant
    ) -> None:
        """active→terminal 单条 UPDATE 携带 digest 占位：唯一放行形态（redaction
        与终态转移同事务，不存在二次覆写窗口）。"""
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            record = _record(tenant, key="raw-sql-redact", canonical_value="erasure target")
            async with tenant_session(sessions, tenant.context) as session:
                repository = PgMemoryRepository(
                    session, tenant.context, ledger=MemoryLifecycleLedger(session, tenant.context)
                )
                await repository.add_candidate(record, now=_T0)
            placeholder = f"redacted:{digest('erasure target')}"
            connection = await asyncpg.connect(ADMIN_DSN)
            try:
                status_row = await connection.execute(
                    "UPDATE memory_records SET status = 'revoked', tombstone = true, "
                    "revoked_reason = 'gdpr', canonical_value = $2, updated_at = now() "
                    "WHERE id = $1",
                    record.id,
                    placeholder,
                )
                assert "UPDATE 1" in status_row
                row = await connection.fetchrow(
                    "SELECT status, tombstone, canonical_value FROM memory_records "
                    "WHERE id = $1",
                    record.id,
                )
                assert row["status"] == "revoked"
                assert row["canonical_value"] == placeholder
            finally:
                await connection.close()
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_terminal_transition_with_rogue_content_blocked(
        self, tenant: _Tenant
    ) -> None:
        """active→terminal 携带非占位内容：借道写入被拒绝（carve-out 收窄）。"""
        engine = create_database_engine(APP_SQLALCHEMY_URL)
        sessions = create_session_factory(engine)
        try:
            record = _record(tenant, key="rogue-redact", canonical_value="target")
            async with tenant_session(sessions, tenant.context) as session:
                repository = PgMemoryRepository(
                    session, tenant.context, ledger=MemoryLifecycleLedger(session, tenant.context)
                )
                await repository.add_candidate(record, now=_T0)
            connection = await asyncpg.connect(ADMIN_DSN)
            try:
                with pytest.raises(asyncpg.PostgresError) as exc_info:
                    await connection.execute(
                        "UPDATE memory_records SET status = 'revoked', tombstone = true, "
                        "canonical_value = 'rogue replacement', updated_at = now() "
                        "WHERE id = $1",
                        record.id,
                    )
                assert "content is immutable" in str(exc_info.value)
            finally:
                await connection.close()
        finally:
            await engine.dispose()
