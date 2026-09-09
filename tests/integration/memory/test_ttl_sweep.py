"""F-R6-06 RED——ADR-009 TTL 自动过期的生产调度。

现状：PgMemoryRepository.expire_candidates（_TTL_EXPIRY_ACTOR + 台账/审计
同事务）只是可调用机制，workflows/、workers/ 无任何 sweep 触发——30 天 TTL
在生产环境不会发生（F-R6-06）。

交付契约：

- 0022_ttl_sweep 迁移：窄 SECURITY DEFINER 函数 zhiwei_ttl_sweep_targets(
  p_cutoff)——跨租户 sweep 目标发现（0003 同款纪律：固定 SQL、无动态 SQL、
  REVOKE PUBLIC、EXECUTE 仅授 zhiwei_app、只暴露 org/ws 两列）；tenant 表
  全 FORCE RLS，应用角色无法自行枚举租户——sweep 目标发现必须走窄函数；
- MemoryTtlSweepActivity（@activity.defn）：definer 函数枚举 → 逐租户
  tenant_session 内 expire_candidates（事件与审计同事务，既有机制复用）；
- MemoryTtlSweepWorkflow（@workflow.defn）注册进 build_agent_worker；
- ensure_memory_ttl_schedule：Temporal Schedule 幂等创建（周期 interval），
  handle.trigger() 可手动触发（测试/运维入口）；
- 生产 worker 组装点属 S11 compose（当前仓内 worker 仅 evals/测试组装）——
  登记于台账。

事实源：specs/s7-memory.md §5/ADR-009、findings F-R6-06、
migrations/versions/0003_auth_sessions.py（窄 definer 函数先例）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text

from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
)
from zhiwei.memory.repositories import PgMemoryRepository
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
async def sessions() -> AsyncIterator[Any]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant(sessions) -> TenantContext:
    return await _make_tenant(sessions)


async def _make_tenant(sessions) -> TenantContext:
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="memory-ttl-sweep")
    return context


def _candidate(created_at: datetime) -> MemoryRecord:
    return MemoryRecord(
        id=uuid4(),
        version=1,
        organization_id=UUID(int=0),  # 由仓储租户上下文校验（seed 时覆写）
        workspace_id=UUID(int=0),
        scope=MemoryScope.USER,
        scope_subject_id=uuid4(),
        type=MemoryType.PREFERENCE,
        subject="ttl.sweep",
        key="ttl.sweep",
        canonical_value="value",
        source_refs=(),
        observed_at=created_at,
        confidence=0.8,
        sensitivity=SensitivityLevel.LOW,
        status=MemoryStatus.CANDIDATE,
        author_ref=uuid4(),
        created_at=created_at,
        updated_at=created_at,
    )


async def _seed_candidate(
    sessions: Any, context: TenantContext, *, created_at: datetime
) -> MemoryRecord:
    record = _candidate(created_at)
    record = record.model_copy(
        update={
            "organization_id": context.organization_id,
            "workspace_id": context.workspace_id,
        }
    )
    async with tenant_session(sessions, context) as session:
        await PgMemoryRepository(session, context).add_candidate(record)
    return record


async def _row(sessions: Any, context: TenantContext, record_id) -> MemoryRecord | None:
    async with tenant_session(sessions, context) as session:
        return await PgMemoryRepository(session, context).get_by_id(record_id)


class TestSweepTargetsFunction:
    async def test_definer_function_enumerates_only_expired_tenants(
        self, tenant, sessions
    ) -> None:
        """窄 definer 函数：返回存在过期 candidate 的 (org, ws)（共享测试库
        可能有其他模块的存量过期行——断言包含关系）；仅有 fresh 候选的租户
        不出现（0022，0003 窄函数纪律）。"""
        expired_a = await _seed_candidate(
            sessions, tenant, created_at=_NOW - timedelta(days=40)
        )
        other = await _make_tenant(sessions)
        expired_b = await _seed_candidate(
            sessions, other, created_at=_NOW - timedelta(days=31)
        )
        # fresh 候选（未过期）
        await _seed_candidate(sessions, tenant, created_at=_NOW - timedelta(days=1))
        # 仅 fresh 的租户：不得出现
        fresh_only = await _make_tenant(sessions)
        await _seed_candidate(
            sessions, fresh_only, created_at=_NOW - timedelta(days=1)
        )

        cutoff = _NOW - timedelta(days=30)
        connection = await asyncpg.connect(ADMIN_DSN.replace("zhiwei_migrator", "zhiwei_app"))
        try:
            rows = await connection.fetch(
                "SELECT organization_id, workspace_id FROM zhiwei_ttl_sweep_targets($1)",
                cutoff,
            )
        finally:
            await connection.close()
        pairs = {(str(r["organization_id"]), str(r["workspace_id"])) for r in rows}
        assert {
            (str(tenant.organization_id), str(tenant.workspace_id)),
            (str(other.organization_id), str(other.workspace_id)),
        } <= pairs
        assert (str(fresh_only.organization_id), str(fresh_only.workspace_id)) not in pairs
        _ = (expired_a, expired_b)

    async def test_function_not_executable_by_public(self) -> None:
        """REVOKE PUBLIC：函数仅 zhiwei_app 可执行（0003 纪律）。"""
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            granted = await connection.fetchval(
                "SELECT has_function_privilege('zhiwei_app',"
                " 'zhiwei_ttl_sweep_targets(timestamptz)', 'EXECUTE')"
            )
            public_granted = await connection.fetchval(
                "SELECT has_function_privilege('public',"
                " 'zhiwei_ttl_sweep_targets(timestamptz)', 'EXECUTE')"
            )
        finally:
            await connection.close()
        assert granted is True
        assert public_granted is False


class TestTtlSweepActivity:
    async def test_sweep_expires_candidates_across_tenants(
        self, tenant, sessions
    ) -> None:
        """sweep：definer 枚举 → 逐租户 expire_candidates（EXPIRED+tombstone +
        lifecycle 台账 + 审计同事务）；fresh 候选不动；跨租户隔离。"""
        from zhiwei.workflows.activities.memory_ttl import MemoryTtlSweepActivity, TtlSweepInput

        expired = await _seed_candidate(
            sessions, tenant, created_at=_NOW - timedelta(days=40)
        )
        fresh = await _seed_candidate(
            sessions, tenant, created_at=_NOW - timedelta(days=1)
        )
        other = await _make_tenant(sessions)
        other_expired = await _seed_candidate(
            sessions, other, created_at=_NOW - timedelta(days=31)
        )

        activity = MemoryTtlSweepActivity(sessions)
        output = await activity.sweep(TtlSweepInput(now=_NOW.isoformat()))

        # 共享测试库有其他模块的存量过期行——断言本租户的行数下限与目标覆盖
        assert output.expired_count >= 2
        assert output.target_count >= 2

        expired_row = await _row(sessions, tenant, expired.id)
        assert expired_row is not None
        assert expired_row.status == MemoryStatus.EXPIRED.value
        assert expired_row.tombstone is True
        fresh_row = await _row(sessions, tenant, fresh.id)
        assert fresh_row is not None
        assert fresh_row.status == MemoryStatus.CANDIDATE.value
        other_row = await _row(sessions, other, other_expired.id)
        assert other_row is not None
        assert other_row.status == MemoryStatus.EXPIRED.value

        # 审计同事务：逐租户落 candidate.expired（actor = system:memory-ttl-expiry）
        async with tenant_session(sessions, tenant) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT action, actor_ref FROM audit_events WHERE resource_id = :rid"
                    ),
                    {"rid": expired.id},
                )
            ).fetchall()
        assert any(
            r.action == "memory.candidate.expired"
            and r.actor_ref == "system:memory-ttl-expiry"
            for r in rows
        )

        # 重试幂等：重复 sweep 零新过期、零新审计（SQL 预过滤承接——已 EXPIRED
        # 行不会被再次选出）
        async with tenant_session(sessions, tenant) as session:
            audits_before = (
                await session.scalar(
                    text("SELECT count(*) FROM audit_events WHERE resource_id = :rid"),
                    {"rid": expired.id},
                )
            )
        second = await activity.sweep(TtlSweepInput(now=_NOW.isoformat()))
        assert second.expired_count == 0
        async with tenant_session(sessions, tenant) as session:
            audits_after = (
                await session.scalar(
                    text("SELECT count(*) FROM audit_events WHERE resource_id = :rid"),
                    {"rid": expired.id},
                )
            )
        assert audits_after == audits_before

    async def test_sweep_no_targets_is_noop(self, tenant, sessions) -> None:
        from zhiwei.workflows.activities.memory_ttl import MemoryTtlSweepActivity, TtlSweepInput

        activity = MemoryTtlSweepActivity(sessions)
        output = await activity.sweep(TtlSweepInput(now=_NOW.isoformat()))
        assert output.expired_count == 0
        assert output.target_count == 0


class TestScheduleRegistration:
    async def test_schedule_creates_and_triggers_workflow(self) -> None:
        """E2E（Temporal dev server——E-R1 同形态）：ensure 幂等创建 →
        handle.trigger() 立即触发 → workflow 执行 sweep activity → PG 过期
        生效。"""
        from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
        from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE, build_agent_worker
        from zhiwei.workers.schedules import (
            TTL_SWEEP_SCHEDULE_ID,
            ensure_memory_ttl_schedule,
        )

        env = None
        engine = create_database_engine(APP_URL)
        sessions = create_session_factory(engine)
        try:
            from temporalio.testing import WorkflowEnvironment

            env = await WorkflowEnvironment.start_local()
            client = env.client
            tenant = await _make_tenant(sessions)
            expired = await _seed_candidate(
                sessions, tenant, created_at=_NOW - timedelta(days=40)
            )

            # 幂等创建（两次调用不报错）
            await ensure_memory_ttl_schedule(
                client, task_queue=DEFAULT_TASK_QUEUE, interval_seconds=3600
            )
            await ensure_memory_ttl_schedule(
                client, task_queue=DEFAULT_TASK_QUEUE, interval_seconds=3600
            )
            handle = client.get_schedule_handle(TTL_SWEEP_SCHEDULE_ID)
            description = await handle.describe()
            assert description.schedule is not None

            # 真实 worker（含 TTL workflow/activity 注册）跑一轮 trigger
            registry = TaskHandlerRegistry()
            worker = build_agent_worker(
                client,
                task_queue=DEFAULT_TASK_QUEUE,
                session_factory=sessions,
                handler_registry=registry,
            )
            async with worker:
                await handle.trigger()
                # 等 workflow 完成（sweep 幂等；轮询 PG 直至过期生效）
                deadline = datetime.now(tz=UTC) + timedelta(seconds=20)
                while datetime.now(tz=UTC) < deadline:
                    row = await _row(sessions, tenant, expired.id)
                    if row is not None and row.status == MemoryStatus.EXPIRED.value:
                        break
                    await asyncio.sleep(0.3)
                row = await _row(sessions, tenant, expired.id)
                assert row is not None
                assert row.status == MemoryStatus.EXPIRED.value
                assert row.tombstone is True
        finally:
            if env is not None:
                await env.shutdown()
            await engine.dispose()
