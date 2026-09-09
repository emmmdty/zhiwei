"""S1-T4 RED：结构化 audit 记录、digest chain、事务原子性与 fail-closed deny 审计。

设计/验收方确认的方案（docs/handoffs/s1-t4-design-gap.md）：
- audit_events 追加 audit_schema_version + effective_identity_ref / resource_version /
  decision_id / policy_revision / decision_reason / result / request_id / trace_id；
- digest 按版本分派：v1（既有公式）逐字节不变，v2 覆盖全部语义字段；同一 scope 链可混 v1/v2；
- typed AuditRecord 固定 v2：effective identity / resource version / result / request /
  trace 必填；decision_id/revision 允许 NULL——fail-closed 本地拒绝绝不伪造 OPA metadata；
- mutation 与 audit/outbox 同事务提交或回滚；denied mutation 写独立 fail-closed 审计事务；
- audit/outbox 不含 token、cookie、authorization header 或 secret。

audit_events 在 (org, workspace) scope 内共享单链：本文件断言一律按本文件独有
request_id/resource_id 过滤，链完整性断言读取全链验证。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from zhiwei.identity.audit import (
    AuditRecord,
    append_audit,
    append_fail_closed_audit,
)
from zhiwei.persistence.events import (
    AuditEventData,
    EventChainError,
    audit_data_from_row,
    verify_audit_chain,
)
from zhiwei.persistence.models import AuditEvent
from zhiwei.persistence.tenant import TenantContext, tenant_session

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
ADMIN_SQLALCHEMY_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
APP_SQLALCHEMY_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
WS_A = UUID("11111111-1111-1111-1111-111111111111")
WS_B = UUID("22222222-2222-2222-2222-222222222222")

_SECRET_MARKERS = (
    "Bearer secret-token-9f2c",
    "session-cookie-value-7e1a",
    "authorization",
    "set-cookie",
)


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_SQLALCHEMY_URL)
    config.attributes["database_url"] = ADMIN_SQLALCHEMY_URL
    return config


async def _assert_safe_test_database(dsn: str) -> None:
    url = make_url(dsn)
    if url.database != "zhiwei_test" or url.username != "zhiwei_migrator":
        raise RuntimeError("destructive migration tests require the dedicated zhiwei_test database")
    connection = await asyncpg.connect(dsn)
    try:
        database, user = await connection.fetchrow("SELECT current_database(), current_user")
        if database != "zhiwei_test" or user != "zhiwei_migrator":
            raise RuntimeError(
                "connected database identity is not the dedicated migration test target"
            )
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[None]:
    """从 base 重建到 head（GREEN 后含 0005）；供本文件所有用例使用。"""
    asyncio.run(_assert_safe_test_database(ADMIN_DSN))
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


@pytest.fixture(scope="function")
def sessions() -> Iterator[async_sessionmaker[AsyncSession]]:
    """每个测试独立 engine；NullPool 让连接只活在测试自己的 event loop 里。"""
    engine = create_async_engine(APP_SQLALCHEMY_URL, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(engine.dispose())


async def _seed_tenants() -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) "
            "VALUES ($1, 'active', 1), ($2, 'active', 1) "
            "ON CONFLICT (id) DO NOTHING",
            ORG_A,
            ORG_B,
        )
        await connection.execute(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) "
            "VALUES ($1, $2, 'sales', 1), ($3, $4, 'sales', 1) "
            "ON CONFLICT (id) DO NOTHING",
            WS_A,
            ORG_A,
            WS_B,
            ORG_B,
        )
    finally:
        await connection.close()


def _record(
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    action: str = "workspace.create",
    result: Literal["allowed", "denied", "failed"] = "allowed",
    actor: str = "user:alice",
    effective: str = "user:alice",
    decision_id: str | None = "decision-1234",
    policy_revision: str | None = "bundle-2026.08.1",
    decision_reason: str = "rbac.allow",
    resource_version: int = 1,
    request_id: str = "req-0001",
    trace_id: str = "trace-0001",
    resource_id: UUID | None = None,
) -> AuditRecord:
    return AuditRecord(
        organization_id=organization_id,
        workspace_id=workspace_id,
        action=action,
        resource_type="workspace",
        resource_id=resource_id if resource_id is not None else WS_A if workspace_id is None else workspace_id,
        resource_version=resource_version,
        actor_ref=actor,
        effective_identity_ref=effective,
        decision_id=decision_id,
        policy_revision=policy_revision,
        decision_reason=decision_reason,
        result=result,
        request_id=request_id,
        trace_id=trace_id,
        payload_digest="sha256:" + "a" * 64,
    )


async def _read_audit_rows(
    organization_id: UUID, workspace_id: UUID | None
) -> list[dict]:
    """以 migrator 读取单 scope 链的全部 audit 行（owner 不受 RLS 限制）。"""
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        if workspace_id is None:
            rows = await connection.fetch(
                "SELECT * FROM audit_events "
                "WHERE organization_id = $1 AND workspace_id IS NULL ORDER BY id",
                organization_id,
            )
        else:
            rows = await connection.fetch(
                "SELECT * FROM audit_events "
                "WHERE organization_id = $1 AND workspace_id = $2 ORDER BY id",
                organization_id,
                workspace_id,
            )
        return [dict(row) for row in rows]
    finally:
        await connection.close()


def _event_data(row: dict) -> AuditEventData:
    return audit_data_from_row(AuditEvent(**row))


async def _chain_order(organization_id: UUID, workspace_id: UUID | None) -> list[dict]:
    """按 digest 链从 root 到 head 的顺序展开指定 scope 的完整链。"""
    rows = await _read_audit_rows(organization_id, workspace_id)
    successors = {row["previous_event_digest"]: row for row in rows}
    if None not in successors:
        return []
    order: list[dict] = []
    current = successors[None]
    visited: set[str] = set()
    while current is not None:
        if current["event_digest"] in visited:
            raise AssertionError("audit chain contains a cycle")
        visited.add(current["event_digest"])
        order.append(current)
        current = successors.get(current["event_digest"])
    return order


# --------------------------------------------------------------------------- 结构化字段


@pytest.mark.asyncio
async def test_allow_record_stores_all_structured_fields(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    async with tenant_session(sessions, context) as session:
        await append_audit(
            session,
            context,
            _record(
                organization_id=ORG_A,
                workspace_id=WS_A,
                request_id="req-allow-fields",
                trace_id="trace-allow-fields",
            ),
        )

    rows = [row for row in await _read_audit_rows(ORG_A, WS_A) if row["request_id"] == "req-allow-fields"]
    assert len(rows) == 1
    row = rows[0]
    assert row["organization_id"] == ORG_A
    assert row["workspace_id"] == WS_A
    assert row["action"] == "workspace.create"
    assert row["resource_type"] == "workspace"
    assert row["resource_id"] == WS_A
    assert row["resource_version"] == 1
    assert row["actor_ref"] == "user:alice"
    assert row["effective_identity_ref"] == "user:alice"
    assert row["decision_id"] == "decision-1234"
    assert row["policy_revision"] == "bundle-2026.08.1"
    assert row["decision_reason"] == "rbac.allow"
    assert row["result"] == "allowed"
    assert row["trace_id"] == "trace-allow-fields"
    assert row["audit_schema_version"] == 2
    verify_audit_chain(_event_data(row) for row in await _read_audit_rows(ORG_A, WS_A))


@pytest.mark.asyncio
async def test_deny_record_stores_fail_closed_decision_metadata(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """fail-closed 拒绝不伪造 OPA decision_id/revision，但必须留下结构化 reason。"""
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A)
    async with tenant_session(sessions, context) as session:
        await append_audit(
            session,
            context,
            _record(
                organization_id=ORG_A,
                workspace_id=None,
                action="organization.member.add",
                result="denied",
                decision_id=None,
                policy_revision=None,
                decision_reason="rbac.deny",
                request_id="req-deny-metadata",
            ),
        )

    rows = [row for row in await _read_audit_rows(ORG_A, None) if row["request_id"] == "req-deny-metadata"]
    assert len(rows) == 1
    row = rows[0]
    assert row["result"] == "denied"
    assert row["decision_id"] is None
    assert row["policy_revision"] is None
    assert row["decision_reason"] == "rbac.deny"
    assert row["effective_identity_ref"] == "user:alice"
    assert row["resource_version"] == 1


@pytest.mark.asyncio
async def test_actor_and_effective_identity_are_distinct_columns(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """actor 与 effective identity 分字段存储；delegation 形状（S2+）也能忠实记录并进入 digest。"""
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    async with tenant_session(sessions, context) as session:
        await append_audit(
            session,
            context,
            _record(
                organization_id=ORG_A,
                workspace_id=WS_A,
                actor="user:alice",
                effective="user:alice",
                request_id="req-actor-plain",
            ),
        )
    async with tenant_session(sessions, context) as session:
        await append_audit(
            session,
            context,
            _record(
                organization_id=ORG_A,
                workspace_id=WS_A,
                action="agent.run.start",
                actor="user:alice",
                effective="agent:delegated-build-77",
                request_id="req-actor-delegated",
            ),
        )

    rows = await _read_audit_rows(ORG_A, WS_A)
    by_request = {row["request_id"]: row for row in rows}
    plain = by_request["req-actor-plain"]
    delegated = by_request["req-actor-delegated"]
    assert plain["actor_ref"] == "user:alice"
    assert plain["effective_identity_ref"] == "user:alice"
    assert delegated["actor_ref"] == "user:alice"
    assert delegated["effective_identity_ref"] == "agent:delegated-build-77"
    # 分字段：actor 与 effective identity 都进入各自 digest（篡改任一都会断链）
    verify_audit_chain(_event_data(row) for row in rows)
    tampered_rows = [
        row.model_copy(update={"effective_identity_ref": "user:mallory"})
        if row.request_id == "req-actor-delegated"
        else row
        for row in (_event_data(row) for row in rows)
    ]
    with pytest.raises(EventChainError):
        verify_audit_chain(tampered_rows)


# --------------------------------------------------------------------------- 事务原子性


@pytest.mark.asyncio
async def test_successful_mutation_commits_business_audit_outbox_together(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A)
    new_workspace = uuid4()
    async with tenant_session(sessions, context) as session:
        from zhiwei.identity.repositories import IdentityRepository

        created, _ = await IdentityRepository(session, context).create_workspace(
            new_workspace, organization_id=ORG_A, name="eng"
        )
        assert created
        await append_audit(
            session,
            context,
            _record(
                organization_id=ORG_A,
                workspace_id=None,
                action="workspace.create",
                resource_id=new_workspace,
                request_id="req-mut-ok",
            ),
        )

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM workspaces WHERE id = $1", new_workspace
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM audit_events WHERE resource_id = $1", new_workspace
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM outbox WHERE topic = 'audit.decision' AND "
            "payload->>'resource_id' = $1",
            str(new_workspace),
        ) == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_rolled_back_mutation_leaves_no_business_audit_or_outbox(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A)
    new_workspace = uuid4()
    with pytest.raises(RuntimeError):
        async with tenant_session(sessions, context) as session:
            from zhiwei.identity.repositories import IdentityRepository

            await IdentityRepository(session, context).create_workspace(
                new_workspace, organization_id=ORG_A, name="eng"
            )
            await append_audit(
                session,
                context,
                _record(
                    organization_id=ORG_A,
                    workspace_id=None,
                    action="workspace.create",
                    resource_id=new_workspace,
                    request_id="req-mut-rollback",
                ),
            )
            raise RuntimeError("rollback the tenant transaction")

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM workspaces WHERE id = $1", new_workspace
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM audit_events WHERE resource_id = $1", new_workspace
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM outbox WHERE payload->>'resource_id' = $1",
            str(new_workspace),
        ) == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_denied_mutation_writes_independent_fail_closed_audit_transaction(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """denied mutation 在独立事务写审计（绝不丢失拒绝记录），业务数据零写入。"""
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A)
    denied_workspace = uuid4()
    await append_fail_closed_audit(
        sessions,
        context,
        _record(
            organization_id=ORG_A,
            workspace_id=None,
            action="workspace.create",
            resource_id=denied_workspace,
            result="denied",
            decision_id=None,
            policy_revision=None,
            decision_reason="rbac.deny",
            request_id="req-deny-failclosed",
        ),
    )

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await connection.fetchval(
            "SELECT count(*) FROM workspaces WHERE id = $1", denied_workspace
        ) == 0
        rows = await connection.fetch(
            "SELECT result, decision_id, policy_revision, decision_reason, "
            "actor_ref, effective_identity_ref, request_id, trace_id, audit_schema_version "
            "FROM audit_events WHERE resource_id = $1",
            denied_workspace,
        )
        assert len(rows) == 1
        assert rows[0]["result"] == "denied"
        assert rows[0]["decision_id"] is None
        assert rows[0]["decision_reason"] == "rbac.deny"
        assert rows[0]["actor_ref"] == "user:alice"
        assert rows[0]["effective_identity_ref"] == "user:alice"
        assert rows[0]["request_id"] == "req-deny-failclosed"
        assert rows[0]["trace_id"] == "trace-0001"
        assert rows[0]["audit_schema_version"] == 2
        assert await connection.fetchval(
            "SELECT count(*) FROM outbox WHERE topic = 'audit.decision' AND "
            "payload->>'resource_id' = $1",
            str(denied_workspace),
        ) == 1
    finally:
        await connection.close()


# --------------------------------------------------------------------------- digest chain


@pytest.mark.asyncio
async def test_chain_digests_link_and_verify(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    for index in range(3):
        async with tenant_session(sessions, context) as session:
            await append_audit(
                session,
                context,
                _record(
                    organization_id=ORG_A,
                    workspace_id=WS_A,
                    action="chain.step",
                    request_id=f"req-chain-{index}",
                    trace_id=f"trace-chain-{index}",
                ),
            )

    rows = await _read_audit_rows(ORG_A, WS_A)
    data = [_event_data(row) for row in rows]
    head = verify_audit_chain(data)
    order = await _chain_order(ORG_A, WS_A)
    # 本文件的 3 条记录必须是全链的最后 3 条（按 request 顺序连续成链）
    mine = [row for row in order if row["request_id"].startswith("req-chain-")]
    assert len(mine) == 3
    assert [row["request_id"] for row in mine] == ["req-chain-0", "req-chain-1", "req-chain-2"]
    assert mine[-1]["event_digest"] == head
    assert len({row["event_digest"] for row in mine}) == 3
    assert mine[1]["previous_event_digest"] == mine[0]["event_digest"]
    assert mine[2]["previous_event_digest"] == mine[1]["event_digest"]


@pytest.mark.asyncio
async def test_tampering_any_semantic_field_breaks_chain(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    for index in range(2):
        async with tenant_session(sessions, context) as session:
            await append_audit(
                session,
                context,
                _record(
                    organization_id=ORG_A,
                    workspace_id=WS_A,
                    action="tamper.step",
                    request_id=f"req-tamper-{index}",
                ),
            )

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch("SELECT * FROM audit_events WHERE organization_id = $1 AND workspace_id = $2 ORDER BY id", ORG_A, WS_A)
        target = next(row for row in rows if row["request_id"] == "req-tamper-1")
        target_id = target["id"]
        original = dict(target)
        tamper_cases = [
            ("effective_identity_ref", "user:mallory"),
            ("resource_version", 99),
            ("decision_id", "decision-tampered"),
            ("policy_revision", "bundle-tampered"),
            ("decision_reason", "tampered"),
            ("result", "denied"),
            ("request_id", "req-tampered"),
            ("trace_id", "trace-tampered"),
            ("action", "tampered.action"),
            ("resource_type", "tampered"),
            ("resource_id", uuid4()),
            ("actor_ref", "user:mallory"),
            ("payload_digest", "sha256:" + "f" * 64),
            ("previous_event_digest", "e" * 71),
        ]
        for column, tampered_value in tamper_cases:
            await connection.execute(
                f'UPDATE audit_events SET "{column}" = $1 WHERE id = $2',
                tampered_value,
                target_id,
            )
            refreshed = await connection.fetch(
                "SELECT * FROM audit_events WHERE organization_id = $1 AND workspace_id = $2 "
                "ORDER BY id",
                ORG_A,
                WS_A,
            )
            with pytest.raises(EventChainError):
                verify_audit_chain(_event_data(row) for row in refreshed)
            await _restore_row(connection, original, target_id)
        # 版本降级篡改：v2 行改标 v1 必须同时清空 v2 字段才能满足 v1_shape 不变量；
        # 即便满足 DB 形状，digest 公式按 v1 重算仍与存储的 v2 digest 不符 → 断链
        await connection.execute(
            "UPDATE audit_events SET audit_schema_version = 1, "
            "effective_identity_ref = NULL, resource_version = NULL, "
            "decision_id = NULL, policy_revision = NULL, decision_reason = NULL, "
            "result = NULL, request_id = NULL, trace_id = NULL WHERE id = $1",
            target_id,
        )
        refreshed = await connection.fetch(
            "SELECT * FROM audit_events WHERE organization_id = $1 AND workspace_id = $2 "
            "ORDER BY id",
            ORG_A,
            WS_A,
        )
        with pytest.raises(EventChainError):
            verify_audit_chain(_event_data(row) for row in refreshed)
        await _restore_row(connection, original, target_id)
        # 跨 scope 篡改（org/workspace 一起改到另一租户的合法 FK 对）：scope 检查拒绝。
        # 该篡改让行离开本 scope，必须读全库两链才能触发 scope 交叉检查。
        await connection.execute(
            "UPDATE audit_events SET organization_id = $1, workspace_id = $2 WHERE id = $3",
            ORG_B,
            WS_B,
            target_id,
        )
        refreshed = await connection.fetch("SELECT * FROM audit_events ORDER BY id")
        with pytest.raises(EventChainError):
            verify_audit_chain(_event_data(row) for row in refreshed)
        await _restore_row(connection, original, target_id)
        # 恢复后链条必须重新完整可验证（不变量：篡改可被检测、恢复可证明）
        restored = await connection.fetch("SELECT * FROM audit_events WHERE organization_id = $1 AND workspace_id = $2 ORDER BY id", ORG_A, WS_A)
        verify_audit_chain(_event_data(row) for row in restored)
    finally:
        await connection.close()


async def _restore_row(connection: asyncpg.Connection, original: dict, target_id: UUID) -> None:
    await connection.execute(
        "UPDATE audit_events SET organization_id = $1, workspace_id = $2, "
        "effective_identity_ref = $3, resource_version = $4, decision_id = $5, "
        "policy_revision = $6, decision_reason = $7, result = $8, request_id = $9, "
        "trace_id = $10, action = $11, resource_type = $12, resource_id = $13, "
        "actor_ref = $14, payload_digest = $15, previous_event_digest = $16, "
        "audit_schema_version = $17 WHERE id = $18",
        original["organization_id"],
        original["workspace_id"],
        original["effective_identity_ref"],
        original["resource_version"],
        original["decision_id"],
        original["policy_revision"],
        original["decision_reason"],
        original["result"],
        original["request_id"],
        original["trace_id"],
        original["action"],
        original["resource_type"],
        original["resource_id"],
        original["actor_ref"],
        original["payload_digest"],
        original["previous_event_digest"],
        original["audit_schema_version"],
        target_id,
    )


@pytest.mark.asyncio
async def test_v1_and_v2_audit_rows_coexist_in_one_chain(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """v1（canonical 路径形状）与 v2（typed）行在同一 scope 链内共存且按版本各自验证。"""
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    from zhiwei.persistence.unit_of_work import append_audit_chain

    v1_resource = uuid4()
    async with tenant_session(sessions, context) as session:
        await append_audit_chain(
            session,
            organization_id=ORG_A,
            workspace_id=WS_A,
            data=AuditEventData(
                organization_id=ORG_A,
                workspace_id=WS_A,
                action="canonical_event.append",
                resource_type="run",
                resource_id=v1_resource,
                actor_ref="user:alice",
                payload_digest="sha256:" + "b" * 64,
                previous_event_digest="",
                event_digest="",
                audit_schema_version=1,
            ),
        )
        await append_audit(
            session,
            context,
            _record(
                organization_id=ORG_A,
                workspace_id=WS_A,
                action="policy.decision",
                request_id="req-mixed-v2",
            ),
        )

    rows = await _read_audit_rows(ORG_A, WS_A)
    v1_rows = [row for row in rows if row["resource_id"] == v1_resource]
    v2_rows = [row for row in rows if row["request_id"] == "req-mixed-v2"]
    assert len(v1_rows) == 1
    assert len(v2_rows) == 1
    assert v1_rows[0]["audit_schema_version"] == 1
    assert v2_rows[0]["audit_schema_version"] == 2
    data = [_event_data(row) for row in rows]
    verify_audit_chain(data)
    # v1 行的 v1 digest 契约仍然逐字节可复算（既有公式不被 v2 扩展改变）
    assert v1_rows[0]["event_digest"] == _v1_digest(_event_data(v1_rows[0]))


def _v1_digest(event: AuditEventData) -> str:
    from zhiwei.contracts.canonical import digest

    return digest(
        {
            "organization_id": str(event.organization_id),
            "workspace_id": None if event.workspace_id is None else str(event.workspace_id),
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": str(event.resource_id),
            "actor_ref": event.actor_ref,
            "payload_digest": event.payload_digest,
            "previous_event_digest": event.previous_event_digest,
        }
    )


# --------------------------------------------------------------------------- secret 与并发


@pytest.mark.asyncio
async def test_audit_and_outbox_contain_no_secrets(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """audit/outbox 不含 token、cookie、authorization header 或 secret。"""
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
    async with tenant_session(sessions, context) as session:
        await append_audit(
            session,
            context,
            _record(
                organization_id=ORG_A,
                workspace_id=WS_A,
                request_id="req-secret-scan",
            ),
        )

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch("SELECT * FROM audit_events")
        outbox_rows = await connection.fetch("SELECT * FROM outbox")
        audit_text = " ".join(str(dict(row)) for row in rows).lower()
        outbox_text = " ".join(str(dict(row)) for row in outbox_rows).lower()
        for marker in _SECRET_MARKERS:
            assert marker.lower() not in audit_text
            assert marker.lower() not in outbox_text
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_concurrent_appends_do_not_fork_chain(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """并发 append 不产生分叉链：advisory lock + 唯一约束兜底，链条完整且单一 head。"""
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)

    async def _append(index: int) -> None:
        async with tenant_session(sessions, context) as session:
            await append_audit(
                session,
                context,
                _record(
                    organization_id=ORG_A,
                    workspace_id=WS_A,
                    action="concurrent.step",
                    request_id=f"req-concurrent-{index}",
                ),
            )

    await asyncio.gather(*[_append(index) for index in range(8)])

    rows = await _read_audit_rows(ORG_A, WS_A)
    mine = [
        row
        for row in rows
        if row["request_id"] is not None and row["request_id"].startswith("req-concurrent-")
    ]
    assert len(mine) == 8
    data = [_event_data(row) for row in rows]
    head = verify_audit_chain(data)
    order = await _chain_order(ORG_A, WS_A)
    assert order[-1]["event_digest"] == head
