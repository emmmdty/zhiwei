"""S1-T4 修复 addendum RED：AuditRecord/DB CHECK/digest 契约冻结（0006_audit_contract）。

冻结 docs/handoffs/s1-t4-repair-design.md §3.1.6（三类 metadata 规则 + reason 非空）与
§3.2（0006 六条 CHECK、payload_digest 严格格式、resource_version >= 0），分四层：

- AuditRecord（Pydantic 边界，无 DB）：payload_digest 必须匹配 ^sha256:[0-9a-f]{64}$；
  result/decision_id/policy_revision 配对——allowed → 两者非空、failed → 两者 NULL、
  denied → 两者或全空，配对约束 (decision_id IS NULL) = (policy_revision IS NULL)；
  decision_reason 非空；resource_version 0 合法、负值非法。
- AuditEventData v2（Pydantic 边界，无 DB）：与 AuditRecord 同款校验，但只约束 v2 行；
  v1 行（audit_schema_version=1、全部 v2 字段 NULL）保持 0005 冻结契约不受影响。
- DB CHECK（direct INSERT 不可绕过）：以 zhiwei_app（FORCE RLS）直接 INSERT 违例 v2 行
  必须触发 asyncpg CheckViolationError（sqlstate 23514）；每个违例用例使用全新
  org/workspace scope（migrator 播种）以通过 RLS；v1 行携带 v2 样式字段仍被
  0005 的 ck_audit_events_v1_shape 拒绝（回归守卫）。
- 迁移契约：base → head 后六条 0006 CHECK 必须存在于 pg_constraint；0005 → head 升级
  必须保留存量合规模 v1/v2 行且链完整可验证；head → 0005 → head 可逆（CHECK 全撤再重建）。

RED 状态（head=0005_audit_structured）：A/B 的格式与配对校验不存在、C 的违例 INSERT
全部成功、D 的六条 CHECK 缺失——本文件在这些点精确失败；E 是回归冻结（v2 digest 已覆盖
全部语义字段，今日即通过）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from zhiwei.identity.audit import AuditRecord, append_audit
from zhiwei.persistence.events import (
    AuditEventData,
    audit_data_from_row,
    build_audit_digest,
    verify_audit_chain,
)
from zhiwei.persistence.models import AuditEvent
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.persistence.unit_of_work import append_audit_chain

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
WS_A = UUID("11111111-1111-1111-1111-111111111111")

_SHA256_PREFIX = "sha256:"
_VALID_DIGEST = _SHA256_PREFIX + "a" * 64
_V1_VALID_DIGEST = _SHA256_PREFIX + "b" * 64

# §3.2 冻结的 0006 CHECK 约束名（downgrade 全撤）
_V2_CONSTRAINTS = frozenset(
    {
        "ck_audit_events_v2_decision_reason",
        "ck_audit_events_v2_decision_pairing",
        "ck_audit_events_v2_allowed_metadata",
        "ck_audit_events_v2_failed_metadata",
        "ck_audit_events_v2_payload_digest",
        "ck_audit_events_v2_resource_version",
    }
)

_V2_RAW_COLUMNS = (
    "id",
    "organization_id",
    "workspace_id",
    "action",
    "resource_type",
    "resource_id",
    "actor_ref",
    "payload_digest",
    "previous_event_digest",
    "event_digest",
    "schema_version",
    "audit_schema_version",
    "effective_identity_ref",
    "resource_version",
    "decision_id",
    "policy_revision",
    "decision_reason",
    "result",
    "request_id",
    "trace_id",
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
    """从 base 重建到 head（GREEN 后含 0006）；供本文件所有用例使用。"""
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
            "VALUES ($1, 'active', 1) ON CONFLICT (id) DO NOTHING",
            ORG_A,
        )
        await connection.execute(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) "
            "VALUES ($1, $2, 'sales', 1) ON CONFLICT (id) DO NOTHING",
            WS_A,
            ORG_A,
        )
    finally:
        await connection.close()


async def _seed_fresh_scope() -> tuple[UUID, UUID]:
    """播种全新 (org, workspace)：每个 direct INSERT 违例用例独占一个 RLS 可见 scope。"""
    organization_id, workspace_id = uuid4(), uuid4()
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO organizations (id, status, schema_version) VALUES ($1, 'active', 1)",
            organization_id,
        )
        await connection.execute(
            "INSERT INTO workspaces (id, organization_id, name, schema_version) "
            "VALUES ($1, $2, $3, 1)",
            workspace_id,
            organization_id,
            "scope-" + uuid4().hex[:12],
        )
        return organization_id, workspace_id
    finally:
        await connection.close()


async def _set_scope(
    connection: asyncpg.Connection, organization_id: UUID, workspace_id: UUID
) -> None:
    await connection.execute(
        "SELECT set_config('zhiwei.organization_id', $1, false)", str(organization_id)
    )
    await connection.execute(
        "SELECT set_config('zhiwei.workspace_id', $1, false)", str(workspace_id)
    )


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
    payload_digest: str = _VALID_DIGEST,
) -> AuditRecord:
    return AuditRecord(
        organization_id=organization_id,
        workspace_id=workspace_id,
        action=action,
        resource_type="workspace",
        resource_id=resource_id
        if resource_id is not None
        else WS_A
        if workspace_id is None
        else workspace_id,
        resource_version=resource_version,
        actor_ref=actor,
        effective_identity_ref=effective,
        decision_id=decision_id,
        policy_revision=policy_revision,
        decision_reason=decision_reason,
        result=result,
        request_id=request_id,
        trace_id=trace_id,
        payload_digest=payload_digest,
    )


def _v2_event(**overrides: Any) -> AuditEventData:
    """合规模 v2 AuditEventData 基准；用例只覆盖被测字段。"""
    values: dict[str, Any] = {
        "organization_id": ORG_A,
        "workspace_id": WS_A,
        "action": "policy.decision",
        "resource_type": "workspace",
        "resource_id": WS_A,
        "actor_ref": "user:alice",
        "payload_digest": _VALID_DIGEST,
        "previous_event_digest": "",
        "event_digest": "",
        "audit_schema_version": 2,
        "effective_identity_ref": "user:alice",
        "resource_version": 1,
        "decision_id": "decision-1234",
        "policy_revision": "bundle-2026.08.1",
        "decision_reason": "rbac.allow",
        "result": "allowed",
        "request_id": "req-0001",
        "trace_id": "trace-0001",
    }
    values.update(overrides)
    return AuditEventData(**values)


async def _read_audit_rows(organization_id: UUID, workspace_id: UUID | None) -> list[dict]:
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


async def _audit_check_names() -> set[str]:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await connection.fetch(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'audit_events'::regclass AND contype = 'c'"
        )
        return {row["conname"] for row in rows}
    finally:
        await connection.close()


async def _insert_raw_row(connection: asyncpg.Connection, values: dict[str, Any]) -> None:
    await connection.execute(
        "INSERT INTO audit_events ("
        " id, organization_id, workspace_id, action, resource_type, resource_id, actor_ref,"
        " payload_digest, previous_event_digest, event_digest, schema_version,"
        " audit_schema_version, effective_identity_ref, resource_version, decision_id,"
        " policy_revision, decision_reason, result, request_id, trace_id"
        ") VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)",
        *[values[column] for column in _V2_RAW_COLUMNS],
    )


async def _insert_raw_v2(
    connection: asyncpg.Connection,
    *,
    organization_id: UUID,
    workspace_id: UUID,
    **mutations: Any,
) -> None:
    """直接 INSERT 一条 v2 行（绕过 Pydantic 边界，专测 DB CHECK 是否可被 bypass）。

    payload_digest/event_digest 必须逐字满足 sha256: + 64hex 形状：0006 的
    ck_audit_events_v2_payload_digest 会先于被测违例被触发，shape 错误会让 PG 报告
    错误约束名（RED 修订登记：repair addendum §3.3 机制缺陷，uuid4().hex 只有 32 位）。
    """
    values: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "action": "policy.decision",
        "resource_type": "workspace",
        "resource_id": uuid4(),
        "actor_ref": "user:alice",
        "payload_digest": _SHA256_PREFIX + uuid4().hex * 2,
        "previous_event_digest": None,
        "event_digest": _SHA256_PREFIX + uuid4().hex,
        "schema_version": 1,
        "audit_schema_version": 2,
        "effective_identity_ref": "user:alice",
        "resource_version": 1,
        "decision_id": "decision-x",
        "policy_revision": "rev-x",
        "decision_reason": "rbac.allow",
        "result": "allowed",
        "request_id": "req-" + uuid4().hex[:8],
        "trace_id": "trace-" + uuid4().hex[:8],
    }
    values.update(mutations)
    await _insert_raw_row(connection, values)


async def _insert_raw_v1(
    connection: asyncpg.Connection,
    *,
    organization_id: UUID,
    workspace_id: UUID,
    **mutations: Any,
) -> None:
    values: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "action": "canonical_event.append",
        "resource_type": "run",
        "resource_id": uuid4(),
        "actor_ref": "user:alice",
        "payload_digest": _SHA256_PREFIX + uuid4().hex * 2,
        "previous_event_digest": None,
        "event_digest": _SHA256_PREFIX + uuid4().hex,
        "schema_version": 1,
        "audit_schema_version": 1,
        "effective_identity_ref": None,
        "resource_version": None,
        "decision_id": None,
        "policy_revision": None,
        "decision_reason": None,
        "result": None,
        "request_id": None,
        "trace_id": None,
    }
    values.update(mutations)
    await _insert_raw_row(connection, values)


# --------------------------------------------------------------------------- A. AuditRecord 边界


def test_audit_record_accepts_sha256_64_lowercase_hex_digest() -> None:
    _record(organization_id=ORG_A, workspace_id=WS_A)


@pytest.mark.parametrize(
    "payload_digest",
    [
        "a" * 71,
        _SHA256_PREFIX + "a" * 63,
        _SHA256_PREFIX + "a" * 65,
        _SHA256_PREFIX + "A" * 64,
        "sha1:" + "a" * 64,
        "a" * 64,
    ],
)
def test_audit_record_rejects_non_sha256_64_digest_formats(payload_digest: str) -> None:
    with pytest.raises(ValidationError):
        _record(organization_id=ORG_A, workspace_id=WS_A, payload_digest=payload_digest)


@pytest.mark.parametrize(
    "mutations",
    [
        {"decision_id": None},
        {"policy_revision": None},
    ],
)
def test_audit_record_allowed_requires_full_decision_pair(mutations: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _record(organization_id=ORG_A, workspace_id=WS_A, **mutations)


def test_audit_record_failed_without_decision_metadata_is_valid() -> None:
    _record(
        organization_id=ORG_A,
        workspace_id=WS_A,
        result="failed",
        decision_id=None,
        policy_revision=None,
        decision_reason="enforcement_internal_error",
    )


@pytest.mark.parametrize(
    "mutations",
    [
        {"result": "failed", "decision_id": "decision-x", "policy_revision": None},
        {"result": "failed", "decision_id": None, "policy_revision": "rev-x"},
        {"result": "failed", "decision_id": "decision-x", "policy_revision": "rev-x"},
    ],
)
def test_audit_record_failed_rejects_decision_metadata(mutations: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _record(organization_id=ORG_A, workspace_id=WS_A, **mutations)


@pytest.mark.parametrize(
    "mutations",
    [
        {"result": "denied", "decision_id": "decision-x", "policy_revision": "rev-x"},
        {"result": "denied", "decision_id": None, "policy_revision": None},
    ],
)
def test_audit_record_denied_accepts_full_or_empty_pair(mutations: dict[str, Any]) -> None:
    _record(organization_id=ORG_A, workspace_id=WS_A, **mutations)


@pytest.mark.parametrize(
    "mutations",
    [
        {"result": "denied", "decision_id": "decision-x", "policy_revision": None},
        {"result": "denied", "decision_id": None, "policy_revision": "rev-x"},
    ],
)
def test_audit_record_denied_rejects_partial_pair(mutations: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _record(organization_id=ORG_A, workspace_id=WS_A, **mutations)


def test_audit_record_rejects_empty_decision_reason() -> None:
    with pytest.raises(ValidationError):
        _record(organization_id=ORG_A, workspace_id=WS_A, decision_reason="")


def test_audit_record_accepts_resource_version_zero() -> None:
    _record(organization_id=ORG_A, workspace_id=WS_A, resource_version=0)


def test_audit_record_rejects_negative_resource_version() -> None:
    with pytest.raises(ValidationError):
        _record(organization_id=ORG_A, workspace_id=WS_A, resource_version=-1)


# --------------------------------------------------------------------------- B. AuditEventData v2 边界


def test_audit_event_v2_accepts_conforming_row() -> None:
    _v2_event()


@pytest.mark.parametrize(
    "payload_digest",
    ["a" * 71, _SHA256_PREFIX + "a" * 63, _SHA256_PREFIX + "A" * 64, "sha1:" + "a" * 64],
)
def test_audit_event_v2_rejects_non_sha256_64_digest_formats(payload_digest: str) -> None:
    with pytest.raises(ValidationError):
        _v2_event(payload_digest=payload_digest)


def test_audit_event_v2_rejects_empty_decision_reason() -> None:
    with pytest.raises(ValidationError):
        _v2_event(decision_reason="")


@pytest.mark.parametrize(
    "mutations",
    [
        {"decision_id": None},
        {"policy_revision": None},
    ],
)
def test_audit_event_v2_allowed_requires_full_decision_pair(mutations: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _v2_event(**mutations)


@pytest.mark.parametrize(
    "mutations",
    [
        {"result": "failed", "decision_id": "decision-x", "policy_revision": None},
        {"result": "failed", "decision_id": None, "policy_revision": "rev-x"},
        {"result": "failed", "decision_id": "decision-x", "policy_revision": "rev-x"},
    ],
)
def test_audit_event_v2_failed_rejects_decision_metadata(mutations: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _v2_event(**mutations)


@pytest.mark.parametrize(
    "mutations",
    [
        {"result": "denied", "decision_id": "decision-x", "policy_revision": None},
        {"result": "denied", "decision_id": None, "policy_revision": "rev-x"},
    ],
)
def test_audit_event_v2_denied_rejects_partial_pair(mutations: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _v2_event(**mutations)


def test_audit_event_v2_rejects_negative_resource_version() -> None:
    with pytest.raises(ValidationError):
        _v2_event(resource_version=-1)


def test_audit_event_v1_row_with_legacy_payload_digest_stays_valid() -> None:
    """v1 行不受 v2 校验影响：0005 冻结契约（任意 payload_digest）保持不变。"""
    _v2_event(
        audit_schema_version=1,
        payload_digest="b" * 71,
        effective_identity_ref=None,
        resource_version=None,
        decision_id=None,
        policy_revision=None,
        decision_reason=None,
        result=None,
        request_id=None,
        trace_id=None,
    )


# --------------------------------------------------------------------------- E. v2 digest 语义字段覆盖（回归冻结）


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effective_identity_ref", "user:mallory"),
        ("resource_version", 2),
        ("decision_id", "decision-tampered"),
        ("policy_revision", "bundle-tampered"),
        ("decision_reason", "deny:no-binding"),
        ("result", "denied"),
        ("request_id", "req-tampered"),
        ("trace_id", "trace-tampered"),
        ("payload_digest", _SHA256_PREFIX + "c" * 64),
    ],
)
def test_v2_digest_covers_all_semantic_fields(field: str, value: Any) -> None:
    base = _v2_event()
    modified = base.model_copy(update={field: value})
    assert build_audit_digest(base) != build_audit_digest(modified)


# --------------------------------------------------------------------------- C. DB CHECK 不可绕过


@pytest.mark.parametrize(
    ("mutations", "constraint"),
    [
        ({"decision_reason": None}, "ck_audit_events_v2_decision_reason"),
        ({"decision_reason": ""}, "ck_audit_events_v2_decision_reason"),
        (
            {"decision_id": None, "policy_revision": None},
            "ck_audit_events_v2_allowed_metadata",
        ),
        (
            {"result": "failed", "decision_id": "decision-x", "policy_revision": "rev-x"},
            "ck_audit_events_v2_failed_metadata",
        ),
        (
            {"result": "denied", "policy_revision": None},
            "ck_audit_events_v2_decision_pairing",
        ),
        (
            {"result": "denied", "decision_id": None},
            "ck_audit_events_v2_decision_pairing",
        ),
        ({"payload_digest": "a" * 71}, "ck_audit_events_v2_payload_digest"),
        ({"resource_version": -1}, "ck_audit_events_v2_resource_version"),
    ],
)
@pytest.mark.asyncio
async def test_direct_insert_v2_violating_row_is_rejected_by_check(
    migrated_database: None,
    mutations: dict[str, Any],
    constraint: str,
) -> None:
    """以 zhiwei_app 绕过 Pydantic 直接 INSERT：每条 0006 CHECK 必须在 DB 层拒绝违例行。"""
    organization_id, workspace_id = await _seed_fresh_scope()
    connection = await asyncpg.connect(APP_DSN)
    try:
        await _set_scope(connection, organization_id, workspace_id)
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as excinfo:
            await _insert_raw_v2(
                connection,
                organization_id=organization_id,
                workspace_id=workspace_id,
                **mutations,
            )
        assert excinfo.value.sqlstate == "23514"
        assert getattr(excinfo.value, "constraint_name", None) == constraint
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_direct_insert_conforming_v2_row_is_accepted(
    migrated_database: None,
) -> None:
    """正向对照：满足全部 0006 CHECK 的 v2 行直接 INSERT 必须成功（CHECK 不误伤合规模）。"""
    organization_id, workspace_id = await _seed_fresh_scope()
    connection = await asyncpg.connect(APP_DSN)
    try:
        await _set_scope(connection, organization_id, workspace_id)
        await _insert_raw_v2(connection, organization_id=organization_id, workspace_id=workspace_id)
    finally:
        await connection.close()
    rows = await _read_audit_rows(organization_id, workspace_id)
    assert len(rows) == 1
    assert rows[0]["audit_schema_version"] == 2
    assert rows[0]["result"] == "allowed"
    assert rows[0]["decision_id"] is not None
    assert rows[0]["policy_revision"] is not None
    assert rows[0]["decision_reason"] == "rbac.allow"
    assert rows[0]["resource_version"] == 1


@pytest.mark.asyncio
async def test_direct_insert_v1_row_with_v2_style_result_rejected(
    migrated_database: None,
) -> None:
    """回归守卫（0005 既有规则）：v1 行携带 v2 样式字段仍被 ck_audit_events_v1_shape 拒绝。"""
    organization_id, workspace_id = await _seed_fresh_scope()
    connection = await asyncpg.connect(APP_DSN)
    try:
        await _set_scope(connection, organization_id, workspace_id)
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as excinfo:
            await _insert_raw_v1(
                connection,
                organization_id=organization_id,
                workspace_id=workspace_id,
                result="allowed",
            )
        assert excinfo.value.sqlstate == "23514"
        assert getattr(excinfo.value, "constraint_name", None) == "ck_audit_events_v1_shape"
    finally:
        await connection.close()


# --------------------------------------------------------------------------- D. 迁移契约


@pytest.mark.asyncio
async def test_fresh_base_to_head_installs_0006_checks(migrated_database: None) -> None:
    """全新 base → head：alembic 版本必须停在当前 head 且六条 0006 CHECK 全部存在。"""
    config = _alembic_config()
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        version = await connection.fetchval("SELECT version_num FROM alembic_version")
        assert version == head
        names = await _audit_check_names()
        assert names >= _V2_CONSTRAINTS
    finally:
        await connection.close()


async def _insert_conforming_rows_via_migrator() -> tuple[str, str]:
    """在 0005 形态下写入一条 v1 + 一条合规模 v2（真实 digest 链），供升级生存性校验。"""
    await _seed_tenants()
    v1_data = AuditEventData(
        organization_id=ORG_A,
        workspace_id=WS_A,
        action="canonical_event.append",
        resource_type="run",
        resource_id=uuid4(),
        actor_ref="user:alice",
        payload_digest=_V1_VALID_DIGEST,
        previous_event_digest="",
        event_digest="",
        audit_schema_version=1,
    )
    v1_digest = build_audit_digest(v1_data.model_copy(update={"previous_event_digest": None}))
    v2_data = _v2_event(
        action="policy.decision",
        request_id="req-contract-upgrade",
        trace_id="trace-contract-upgrade",
    )
    v2_digest = build_audit_digest(v2_data.model_copy(update={"previous_event_digest": v1_digest}))
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "INSERT INTO audit_events ("
            " id, organization_id, workspace_id, action, resource_type, resource_id, actor_ref,"
            " payload_digest, previous_event_digest, event_digest, schema_version,"
            " audit_schema_version, effective_identity_ref, resource_version, decision_id,"
            " policy_revision, decision_reason, result, request_id, trace_id"
            ") VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)",
            uuid4(),
            ORG_A,
            WS_A,
            v1_data.action,
            v1_data.resource_type,
            v1_data.resource_id,
            v1_data.actor_ref,
            v1_data.payload_digest,
            None,
            v1_digest,
            1,
            v1_data.audit_schema_version,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        await connection.execute(
            "INSERT INTO audit_events ("
            " id, organization_id, workspace_id, action, resource_type, resource_id, actor_ref,"
            " payload_digest, previous_event_digest, event_digest, schema_version,"
            " audit_schema_version, effective_identity_ref, resource_version, decision_id,"
            " policy_revision, decision_reason, result, request_id, trace_id"
            ") VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)",
            uuid4(),
            ORG_A,
            WS_A,
            v2_data.action,
            v2_data.resource_type,
            v2_data.resource_id,
            v2_data.actor_ref,
            v2_data.payload_digest,
            v1_digest,
            v2_digest,
            1,
            v2_data.audit_schema_version,
            v2_data.effective_identity_ref,
            v2_data.resource_version,
            v2_data.decision_id,
            v2_data.policy_revision,
            v2_data.decision_reason,
            v2_data.result,
            v2_data.request_id,
            v2_data.trace_id,
        )
    finally:
        await connection.close()
    return v1_digest, v2_digest


async def _assert_rows_survive_and_chain_verifies(v1_digest: str, v2_digest: str) -> None:
    rows = await _read_audit_rows(ORG_A, WS_A)
    by_digest = {row["event_digest"]: row for row in rows}
    assert set(by_digest) == {v1_digest, v2_digest}
    assert by_digest[v1_digest]["previous_event_digest"] is None
    assert by_digest[v1_digest]["audit_schema_version"] == 1
    assert by_digest[v2_digest]["previous_event_digest"] == v1_digest
    assert by_digest[v2_digest]["audit_schema_version"] == 2
    data = [_event_data(row) for row in rows]
    assert verify_audit_chain(data) == v2_digest


def test_upgrade_to_head_preserves_conforming_v1_v2_rows(migrated_database: None) -> None:
    """0005 → head 升级必须带着存量合规模 v1/v2 行成功，且混合链完整可验证。"""
    config = _alembic_config()
    command.downgrade(config, "0005_audit_structured")
    v1_digest, v2_digest = asyncio.run(_insert_conforming_rows_via_migrator())
    command.upgrade(config, "head")
    asyncio.run(_assert_rows_survive_and_chain_verifies(v1_digest, v2_digest))


def test_downgrade_head_to_0005_drops_checks_and_upgrade_restores(
    migrated_database: None,
) -> None:
    """head → 0005 → head 可逆：downgrade 全撤六条 CHECK，upgrade 全部重建。"""
    config = _alembic_config()
    command.downgrade(config, "0005_audit_structured")
    assert asyncio.run(_audit_check_names()) & _V2_CONSTRAINTS == set()
    command.upgrade(config, "head")
    assert asyncio.run(_audit_check_names()) >= _V2_CONSTRAINTS


@pytest.mark.asyncio
async def test_mixed_v1_v2_chain_verifies_after_head(
    migrated_database: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """head 之上继续追加 v1 + v2 混合链：digest 链端到端完整（复用 append 设施，无旁路）。"""
    await _seed_tenants()
    context = TenantContext(organization_id=ORG_A, workspace_id=WS_A)
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
                payload_digest=_V1_VALID_DIGEST,
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
                request_id="req-contract-mixed-v2",
            ),
        )

    rows = await _read_audit_rows(ORG_A, WS_A)
    v1_rows = [row for row in rows if row["resource_id"] == v1_resource]
    v2_rows = [row for row in rows if row["request_id"] == "req-contract-mixed-v2"]
    assert len(v1_rows) == 1
    assert len(v2_rows) == 1
    assert v1_rows[0]["audit_schema_version"] == 1
    assert v2_rows[0]["audit_schema_version"] == 2
    data = [_event_data(row) for row in rows]
    assert verify_audit_chain(data) == v2_rows[0]["event_digest"]
    # v1 行 digest 仍按 0001 冻结公式逐字节可复算（v2 扩展不改变 v1 公式）
    assert v1_rows[0]["event_digest"] == _v1_digest(_event_data(v1_rows[0]))
