"""F-R6-04 RED——Source Ledger 持久层 + SyncIntent DELETE/REVOKE 生产处理。

ledger.py 自述 "In production, this is backed by PostgreSQL + ObjectStore"，
实际生产只有进程内字典；SyncManager 产出的 DELETE/REVOKE SyncIntent 无任何
ledger 消费方（F-R6-04）。本批交付：

- 0021_source_ledger 迁移：source_objects（acl 为唯一可变列——ADR-006 失权
  投影的当前 ACL 权威）+ source_versions（state/tombstone/updated_at 之外
  内容不可变，digest+immutable 协议：CHECK sha256 格式 + 内容列触发器守护 +
  列级 UPDATE 授权），RLS FORCE；
- PgSourceLedger：ledger.py 全部不变量的异步持久版（幂等 register、
  version_seq、重复 digest DuplicateVersionError、latest 仅 ACTIVE、
  stale/revoke 转移、tombstone）；
- SyncIntent DELETE/REVOKE 生产处理（apply_delete_revoke）：REVOKE = 对象全部
  活跃版本失权（ADR-006 priority），DELETE = 全版本 tombstone；幂等（重复
  apply 零新审计）；经 append_audit_chain 同事务落审计；
- SourceLedgerActivity（@activity.defn）注册进生产 worker。

事实源：specs/s5-knowledge.md §3（Source Ledger）、ADR-006、
findings F-R6-04、tests/unit/knowledge/test_ledger.py（域不变量锚点）。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceObject,
    SourceVersionState,
)
from zhiwei.knowledge.ledger import DuplicateVersionError
from zhiwei.knowledge.sync import SyncEventType, SyncIntent
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

pytestmark = pytest.mark.asyncio

ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_URL = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
).replace("postgresql://", "postgresql+asyncpg://", 1)

_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[3]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN)
    config.attributes["database_url"] = ADMIN_DSN
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def sessions():
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant(sessions) -> TenantContext:
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="source-ledger-pg")
    return context


def _object(context: TenantContext, **overrides) -> SourceObject:
    fields = {
        "id": uuid4(),
        "organization_id": context.organization_id,
        "workspace_id": context.workspace_id,
        "source_type": "document",
        "acl": ACLSnapshot(),
        "classification": Classification.PUBLIC,
        "metadata": {},
    }
    fields.update(overrides)
    return SourceObject(**fields)


def _locator(uri: str = "github://acme/widgets") -> Locator:
    return Locator(connector="github", uri=uri)


def _sha256(seed: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


@pytest_asyncio.fixture
async def ledger(sessions, tenant):
    """共享同一租户事务的 (session, ledger)——审计断言必须与被测写在同一
    事务内可见（tenant_session 退出才提交）。"""
    async with tenant_session(sessions, tenant) as session:
        from zhiwei.knowledge.pg_ledger import PgSourceLedger

        yield session, PgSourceLedger(session, tenant)


async def _audits(session: Any, context: TenantContext) -> list[Any]:
    from sqlalchemy import select

    from zhiwei.persistence.models import AuditEvent as AuditEventModel

    rows = (
        await session.scalars(
            select(AuditEventModel).where(
                AuditEventModel.organization_id == context.organization_id
            )
        )
    ).all()
    return list(rows)


class TestPgLedgerInvariants:
    async def test_register_object_idempotent(self, ledger, tenant) -> None:
        _session, ledger = ledger
        obj = _object(tenant)
        await ledger.register_object(obj)
        await ledger.register_object(obj)  # 同 id 幂等
        fetched = await ledger.get_object(obj.id)
        assert fetched.id == obj.id

    async def test_get_unknown_object_raises(self, ledger, tenant) -> None:
        _session, ledger = ledger
        from zhiwei.knowledge.ledger import ObjectNotFoundError

        with pytest.raises(ObjectNotFoundError):
            await ledger.get_object(uuid4())

    async def test_create_version_seq_and_inheritance(self, ledger, tenant) -> None:
        _session, ledger = ledger
        obj = _object(tenant)
        await ledger.register_object(obj)
        v1 = await ledger.create_version(
            obj.id,
            locator=_locator(),
            content_digest=_sha256("v1"),
            observed_at=_NOW,
            valid_at=_NOW,
        )
        assert v1.version_seq == 1
        assert v1.state is SourceVersionState.ACTIVE
        # acl/classification 缺省继承对象
        assert v1.acl == obj.acl
        assert v1.classification == obj.classification

        v2 = await ledger.create_version(
            obj.id,
            locator=_locator(uri="github://acme/widgets@abc"),
            content_digest=_sha256("v2"),
            observed_at=_NOW,
            valid_at=_NOW,
        )
        assert v2.version_seq == 2

    async def test_duplicate_digest_rejected(self, ledger, tenant) -> None:
        _session, ledger = ledger
        obj = _object(tenant)
        await ledger.register_object(obj)
        digest = _sha256("same")
        await ledger.create_version(
            obj.id,
            locator=_locator(),
            content_digest=digest,
            observed_at=_NOW,
            valid_at=_NOW,
        )
        with pytest.raises(DuplicateVersionError):
            await ledger.create_version(
                obj.id,
                locator=_locator(uri="github://acme/other"),
                content_digest=digest,
                observed_at=_NOW,
                valid_at=_NOW,
            )

    async def test_version_content_is_immutable_at_data_plane(
        self, ledger, tenant
    ) -> None:
        """digest+immutable 协议：内容列在数据面不可变（触发器守护）。"""
        session, ledger = ledger
        obj = _object(tenant)
        await ledger.register_object(obj)
        version = await ledger.create_version(
            obj.id,
            locator=_locator(),
            content_digest=_sha256("locked"),
            observed_at=_NOW,
            valid_at=_NOW,
        )
        import sqlalchemy as sa

        from zhiwei.persistence.models import SourceVersionRow

        # 同事务内触发器拦截：内容列 UPDATE 必须失败（列级授权/触发器双层）
        with pytest.raises(Exception):  # noqa: B017 - 触发器异常经 DBAPI 包装
            await session.execute(
                sa.update(SourceVersionRow)
                .where(SourceVersionRow.id == version.id)
                .values(content_digest=_sha256("tampered"))
            )

    async def test_content_immutability_trigger_fires_with_full_grants(
        self, sessions, tenant
    ) -> None:
        """对抗审查 gap 1 回归锚点：以持全列授权的 migrator 角色直连验证
        0021 触发器拦截（列级授权拒绝 42501 会让 zhiwei_app 路径的 raises
        测试对触发器零检出能力——本测试钉死触发器层本体）。"""
        import asyncpg

        from zhiwei.knowledge.pg_ledger import PgSourceLedger

        async with tenant_session(sessions, tenant) as session:
            ledger = PgSourceLedger(session, tenant)
            obj = _object(tenant)
            await ledger.register_object(obj)
            version = await ledger.create_version(
                obj.id,
                locator=_locator(),
                content_digest=_sha256("trigger-check"),
                observed_at=_NOW,
                valid_at=_NOW,
            )
        # tenant_session 退出即提交（独立连接可见；GUC 是 SET LOCAL——
        # 事务级作用域，触发器测试必须自持事务）
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            with pytest.raises(asyncpg.PostgresError) as exc_info:
                await connection.execute(
                    "UPDATE source_versions SET content_digest = $1 WHERE id = $2",
                    _sha256("tampered"),
                    version.id,
                )
            assert "source version" in str(exc_info.value)
            assert "content is immutable" in str(exc_info.value)
        finally:
            await connection.close()

    async def test_mark_stale_and_revoke_transitions(self, ledger, tenant) -> None:
        _session, ledger = ledger
        obj = _object(tenant)
        await ledger.register_object(obj)
        v1 = await ledger.create_version(
            obj.id,
            locator=_locator(),
            content_digest=_sha256("s1"),
            observed_at=_NOW,
            valid_at=_NOW,
        )
        stale = await ledger.mark_stale(v1.id)
        assert stale.state is SourceVersionState.STALE

        revoked = await ledger.revoke_version(v1.id)
        assert revoked.state is SourceVersionState.REVOKED
        assert revoked.tombstone is True

        # 重复 revoke 幂等（不抛错）
        again = await ledger.revoke_version(v1.id)
        assert again.state is SourceVersionState.REVOKED

    async def test_mark_stale_rejected_on_revoked(self, ledger, tenant) -> None:
        """域语义对齐（内存版 parity）：REVOKED/tombstone 不可 mark_stale。"""
        _session, ledger = ledger
        obj = _object(tenant)
        await ledger.register_object(obj)
        v1 = await ledger.create_version(
            obj.id,
            locator=_locator(),
            content_digest=_sha256("mr1"),
            observed_at=_NOW,
            valid_at=_NOW,
        )
        await ledger.revoke_version(v1.id)
        with pytest.raises(ValueError, match="Cannot mark a revoked version as stale"):
            await ledger.mark_stale(v1.id)

    async def test_latest_version_only_active(self, ledger, tenant) -> None:
        _session, ledger = ledger
        obj = _object(tenant)
        await ledger.register_object(obj)
        v1 = await ledger.create_version(
            obj.id,
            locator=_locator(),
            content_digest=_sha256("l1"),
            observed_at=_NOW,
            valid_at=_NOW,
        )
        assert (await ledger.latest_version(obj.id)).id == v1.id
        await ledger.mark_stale(v1.id)
        assert await ledger.latest_version(obj.id) is None


class TestSyncIntentApplication:
    async def _seed_object_with_versions(
        self, session, ledger, tenant
    ) -> tuple[SourceObject, list]:
        obj = _object(tenant)
        await ledger.register_object(obj)
        versions = []
        for i in range(2):
            versions.append(
                await ledger.create_version(
                    obj.id,
                    locator=_locator(uri=f"github://acme/widgets@{i}"),
                    content_digest=_sha256(f"intent-{i}"),
                    observed_at=_NOW,
                    valid_at=_NOW,
                )
            )
        return obj, versions

    async def test_revoke_intent_disables_all_active_versions(
        self, ledger, tenant
    ) -> None:
        session, ledger = ledger
        """delete/revoke 优先（spec §3）：REVOKE intent → 对象全部未失权版本
        （active 与 stale）REVOKED+tombstone + 同事务审计；重复 apply 幂等
        零新审计。"""
        obj, versions = await self._seed_object_with_versions(session, ledger, tenant)
        intent = SyncIntent(
            event_type=SyncEventType.REVOKE,
            connector="github",
            source_object_id=obj.id,
            event_id="evt-1",
            payload={"reason": "installation revoked"},
            idempotency_key="github:evt-1",
        )
        applied = await ledger.apply_delete_revoke(intent)
        assert applied.revoked_version_ids == {v.id for v in versions}
        for version in versions:
            current = await ledger.get_version(version.id)
            assert current.state is SourceVersionState.REVOKED
            assert current.tombstone is True

        audit_actions = {
            a.action
            for a in await _audits(session, tenant)
            if a.resource_id == obj.id
        }
        assert "knowledge.source.revoke" in audit_actions

        # 幂等：重复 apply 零新审计
        before = len(await _audits(session, tenant))
        await ledger.apply_delete_revoke(intent)
        assert len(await _audits(session, tenant)) == before

    async def test_revoke_intent_also_tombstones_stale_versions(
        self, ledger, tenant
    ) -> None:
        """对抗审查 gap 2 契约钉死：REVOKE 与 DELETE 同语义面——stale 版本
        同样失权（spec §3 delete/revoke 优先：源失权后任何版本不可再被
        检索/物化；历史 Run 引用保留、可见性由 ADR-006 查询期拒绝）。"""
        session, ledger = ledger
        obj, versions = await self._seed_object_with_versions(session, ledger, tenant)
        await ledger.mark_stale(versions[0].id)
        intent = SyncIntent(
            event_type=SyncEventType.REVOKE,
            connector="github",
            source_object_id=obj.id,
            event_id="evt-stale",
            payload={"reason": "installation revoked"},
            idempotency_key="github:evt-stale",
        )
        applied = await ledger.apply_delete_revoke(intent)
        assert applied.revoked_version_ids == {v.id for v in versions}

    async def test_delete_intent_tombstones_everything(
        self, ledger, tenant
    ) -> None:
        session, ledger = ledger
        obj, versions = await self._seed_object_with_versions(session, ledger, tenant)
        await ledger.mark_stale(versions[0].id)  # stale 版本也在删除面内
        intent = SyncIntent(
            event_type=SyncEventType.DELETE,
            connector="github",
            source_object_id=obj.id,
            event_id="evt-2",
            payload={"reason": "repository deleted"},
            idempotency_key="github:evt-2",
        )
        applied = await ledger.apply_delete_revoke(intent)
        assert applied.revoked_version_ids == {v.id for v in versions}
        for version in versions:
            current = await ledger.get_version(version.id)
            assert current.state is SourceVersionState.REVOKED
            assert current.tombstone is True
        audit_actions = {
            a.action
            for a in await _audits(session, tenant)
            if a.resource_id == obj.id
        }
        assert "knowledge.source.delete" in audit_actions

    async def test_intent_for_unknown_object_fails_closed(self, ledger, tenant) -> None:
        _session, ledger = ledger
        from zhiwei.knowledge.ledger import ObjectNotFoundError

        intent = SyncIntent(
            event_type=SyncEventType.REVOKE,
            connector="github",
            source_object_id=uuid4(),
            event_id="evt-3",
            payload={},
            idempotency_key="github:evt-3",
        )
        with pytest.raises(ObjectNotFoundError):
            await ledger.apply_delete_revoke(intent)

    async def test_create_update_intents_not_consumed_here(self, ledger, tenant) -> None:
        session, ledger = ledger
        """CREATE/UPDATE 走 connector 物化路径（新版本），不由 DELETE/REVOKE
        消费方处理——fail closed 拒绝而非静默吞掉。"""
        obj, _ = await self._seed_object_with_versions(session, ledger, tenant)
        intent = SyncIntent(
            event_type=SyncEventType.UPDATE,
            connector="github",
            source_object_id=obj.id,
            event_id="evt-4",
            payload={},
            idempotency_key="github:evt-4",
        )
        with pytest.raises(ValueError):
            await ledger.apply_delete_revoke(intent)


class TestProductionActivity:
    async def test_activity_applies_intent_via_worker_shape(
        self, sessions, tenant
    ) -> None:
        """SourceLedgerActivity 是 @activity.defn 生产 activity：输入输出走
        dataclass，内部 tenant 事务自持（worker 直调形态，不依赖 dev server）。"""
        from zhiwei.knowledge.pg_ledger import PgSourceLedger
        from zhiwei.workflows.activities.source_ledger import (
            ApplyIntentInput,
            SourceLedgerActivity,
        )

        async with tenant_session(sessions, tenant) as session:
            ledger = PgSourceLedger(session, tenant)
            obj = _object(tenant)
            await ledger.register_object(obj)
            version = await ledger.create_version(
                obj.id,
                locator=_locator(),
                content_digest=_sha256("act-1"),
                observed_at=_NOW,
                valid_at=_NOW,
            )

        activity = SourceLedgerActivity(sessions)
        output = await activity.apply_sync_intent(
            ApplyIntentInput(
                event_type="revoke",
                connector="github",
                source_object_id=str(obj.id),
                event_id="evt-act-1",
                payload={"reason": "installation revoked"},
                idempotency_key="github:evt-act-1",
                organization_id=str(tenant.organization_id),
                workspace_id=str(tenant.workspace_id),
            )
        )
        assert output.applied is True
        assert UUID(output.revoked_version_ids[0]) == version.id

    async def test_activity_rejects_unknown_event_type(
        self, sessions, tenant
    ) -> None:
        """fail closed：非法 event_type 在输入边界拒绝（不静默吞）。"""
        from zhiwei.workflows.activities.source_ledger import (
            ApplyIntentInput,
            SourceLedgerActivity,
        )

        activity = SourceLedgerActivity(sessions)
        with pytest.raises(ValueError):
            await activity.apply_sync_intent(
                ApplyIntentInput(
                    event_type="explode",
                    connector="github",
                    source_object_id=str(uuid4()),
                    event_id="evt-bad",
                    payload={},
                    idempotency_key="github:evt-bad",
                    organization_id=str(tenant.organization_id),
                    workspace_id=str(tenant.workspace_id),
                )
            )
