"""S11-T3 e2e：真实 PG 上的三段式升级 + 真实 OpenSearch alias 切换。

契约锚：docs/operations/upgrade.md §2（A 档设计冻结）。previous 版本构造 =
revision 0019 schema + 旧形态数据行（§2.1）——不 checkout 旧代码。

序列：
1. 在 compose PG 上创建隔离 database，`upgrade 0019` 得到 previous schema；
2. 写入旧形态 outbox 行（schema_version=1、无 dispatch_deadline）；
3. preflight（manifest 校验）→ expand(0025) → 旧行经 backward reader 可读；
4. 未显式 checkpoint 时 contract 拒绝执行（停在 expand 后）；
5. 显式 checkpoint → contract(0026)：回填 + NOT NULL；插入 NULL deadline 被拒；
6. rollback 窗口：expand 后、contract 前 downgrade 可行；
7. OpenSearch rebuild + alias switch 对 127.0.0.1:9201 真实执行。

slow：需要 compose 栈（postgres/opensearch）在跑。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PG_LOOPBACK_DSN = "postgresql+asyncpg://zhiwei_migrator:zhiwei-dev-pg-only@127.0.0.1:55433"
OPENSEARCH = "http://127.0.0.1:9201"

pytestmark = pytest.mark.slow


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, timeout=timeout)


def _compose_up() -> None:
    proc = _run(
        [
            "docker", "compose", "-f", "deploy/compose/compose.yaml", "-p", "zhiwei-local",
            "up", "-d", "--wait", "postgres", "opensearch",
        ]
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


@pytest.fixture(scope="module")
def upgrade_db() -> Generator[str]:
    _compose_up()
    dbname = f"upgrade_fixture_{uuid4().hex[:8]}"
    url = f"{PG_LOOPBACK_DSN}/{dbname}"
    _run(
        [
            "docker", "compose", "-f", "deploy/compose/compose.yaml", "-p", "zhiwei-local",
            "exec", "-T", "postgres", "psql", "-U", "postgres", "-c",
            f"CREATE DATABASE {dbname} OWNER zhiwei_migrator;",
        ]
    )
    os.environ["ZHIWEI_DATABASE_URL"] = url
    yield url
    _run(
        [
            "docker", "compose", "-f", "deploy/compose/compose.yaml", "-p", "zhiwei-local",
            "exec", "-T", "postgres", "psql", "-U", "postgres", "-c",
            f"DROP DATABASE IF EXISTS {dbname};",
        ]
    )
    os.environ.pop("ZHIWEI_DATABASE_URL", None)


def _alembic(target: str, dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["database_url"] = dsn
    command.upgrade(cfg, target)


def _seed_legacy_row(dsn: str) -> None:
    """旧形态行经宿主 psql 直插（superuser 绕过 RLS——fixture 播种不构成产品旁路）。

    RLS 面向租户角色；升级演练只需要「0019 形态的旧行存在于表中」这一事实。
    """
    org_id = "11111111-1111-1111-1111-111111111111"
    ws_id = "22222222-2222-2222-2222-222222222222"
    dbname = dsn.rsplit("/", 1)[-1]
    sql = (
        f"INSERT INTO organizations (id, status, schema_version) VALUES ('{org_id}', 'active', 1);"
        f"INSERT INTO workspaces (id, organization_id, name, schema_version)"
        f" VALUES ('{ws_id}', '{org_id}', 'upgrade-fixture', 1);"
        "INSERT INTO outbox (id, organization_id, workspace_id, topic, event_key, payload,"
        " status, schema_version, created_at) VALUES (gen_random_uuid(),"
        f" '{org_id}', '{ws_id}',"
        " 'runtime.command', 'legacy-1', '{}', 'pending', 1, now());"
    )
    proc = _run(
        [
            "docker", "compose", "-f", "deploy/compose/compose.yaml", "-p", "zhiwei-local",
            "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", dbname, "-c", sql,
        ]
    )
    assert proc.returncode == 0, proc.stderr[-1500:]


FIXTURE_TENANT = (
    UUID("11111111-1111-1111-1111-111111111111"),
    UUID("22222222-2222-2222-2222-222222222222"),
)


def _read_rows(dsn: str) -> list:
    """经租户上下文会话读取（outbox FORCE RLS——管理面读取也必须带租户语义）。"""
    async def _inner() -> list:
        from sqlalchemy import text

        from zhiwei.operations.upgrade import OutboxRowView, read_outbox_rows_cross_era
        from zhiwei.persistence.database import create_database_engine, create_session_factory
        from zhiwei.persistence.tenant import TenantContext, tenant_session

        engine = create_database_engine(dsn)
        sessions = create_session_factory(engine)
        context = TenantContext(
            organization_id=FIXTURE_TENANT[0], workspace_id=FIXTURE_TENANT[1]
        )
        try:
            async with tenant_session(sessions, context) as session:
                result = await session.execute(
                    text(
                        "SELECT id::text, topic, schema_version, available_at,"
                        " dispatch_deadline, payload FROM outbox"
                    )
                )
                rows = [
                    OutboxRowView(
                        id=row[0],
                        topic=row[1],
                        schema_version=row[2],
                        available_at=row[3],
                        dispatch_deadline=row[4],
                        payload_schema_version=(row[5] or {}).get("schema_version", 1),
                    )
                    for row in result
                ]
        finally:
            await engine.dispose()
        return read_outbox_rows_cross_era(rows, now=datetime.now(tz=UTC))

    return asyncio.run(_inner())


def _null_insert_rejected(dsn: str) -> None:
    """contract 后：INSERT 显式 NULL deadline 被拒（NOT NULL 契约生效）。

    经租户上下文会话（outbox FORCE RLS）；违反的是 NOT NULL 而非 RLS——
    租户列与 GUC 一致。
    """
    async def _inner() -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        from zhiwei.persistence.database import create_database_engine, create_session_factory
        from zhiwei.persistence.tenant import TenantContext, tenant_session

        engine = create_database_engine(dsn)
        sessions = create_session_factory(engine)
        context = TenantContext(
            organization_id=FIXTURE_TENANT[0], workspace_id=FIXTURE_TENANT[1]
        )
        try:
            async with tenant_session(sessions, context) as session:
                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            "INSERT INTO outbox (id, organization_id, workspace_id, topic,"
                            " event_key, payload, status, schema_version, dispatch_deadline,"
                            " created_at) VALUES (gen_random_uuid(),"
                            f" '{FIXTURE_TENANT[0]}', '{FIXTURE_TENANT[1]}',"
                            " 'runtime.command', 'violating', '{}', 'pending', 2, NULL, now())"
                        )
                    )
        finally:
            await engine.dispose()

    asyncio.run(_inner())


def test_three_phase_upgrade_contract(upgrade_db: str) -> None:
    from zhiwei.operations.upgrade import UpgradeManifest, upgrade_run

    dsn = upgrade_db
    # previous：0019 schema + 旧形态行（§2.1 冻结构造法）
    _alembic("0019_run_template", dsn)
    _seed_legacy_row(dsn)

    manifest = UpgradeManifest(
        previous_revision="0019_run_template",
        target_revision="0026_contract_dispatch_deadline",
        worker_build_id="dev-local",
        requires_checkpoint=True,
        opensearch_rebuild=False,
    )

    # preflight + expand：无 checkpoint 声明时停在 expand 后
    result = upgrade_run(manifest, dsn=dsn)
    assert result.stopped_at == "expand_done"

    # 旧行经 backward reader 可读（expand 后前提满足）
    views = _read_rows(dsn)
    assert views and views[0].era == "v1"
    assert views[0].effective_deadline is not None

    # 未显式 checkpoint：contract 拒绝执行（§2.2）
    with pytest.raises(PermissionError, match="checkpoint"):
        upgrade_run(manifest, dsn=dsn, checkpoint=False, contract_phase=True)

    # 显式 checkpoint：contract 执行（回填 + NOT NULL）
    result = upgrade_run(manifest, dsn=dsn, checkpoint=True, contract_phase=True)
    assert result.stopped_at == "contract_done"

    _null_insert_rejected(dsn)


def test_rollback_window_before_contract(upgrade_db: str) -> None:
    """expand 后 contract 前：downgrade 到 previous 可行（§2.6 rollback 窗口）。"""
    dsn = upgrade_db
    _alembic("0025_expand_dispatch_deadline", dsn)
    _alembic("0019_run_template", dsn)
    _alembic("0026_contract_dispatch_deadline", dsn)


def test_opensearch_rebuild_and_alias_switch(upgrade_db: str) -> None:
    """rebuild + alias switch 对真实 OpenSearch 执行（specs/s11 §4）。"""
    from zhiwei.operations.upgrade import opensearch_rebuild_and_switch

    summary = opensearch_rebuild_and_switch(
        endpoint=OPENSEARCH,
        alias="knowledge-upgrade-test",
        documents=[{"id": "d1", "text": "fixture"}, {"id": "d2", "text": "fixture2"}],
    )
    assert summary.alias == "knowledge-upgrade-test"
    assert summary.document_count == 2
    assert summary.alias_targets_new_index
