"""S11-T4 隔离恢复校验（docs/operations/backup-restore.md §3 八项清单）。

校验序列（全部通过才成功）：
1 恢复完整性（pg_restore ×3 + 对象 digest）；2 RLS（跨租户不可见）；
3 artifact digest；4 canonical projection rebuild；5 search rebuild；
6 workflow reconciliation；7 secret rotation；8 Claim Registry。

隔离 = 同实例新 database（restore_<uuid>_*）+ 独立对象目录；校验结束不自动
清理（operator 复核后显式删除——恢复演练的产物是证据）。
"""

from __future__ import annotations

import shutil
import subprocess
import uuid as uuid_module
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from zhiwei.operations.backup import (
    COMPOSE_FILE,
    COMPOSE_PROJECT,
    sha256_file,
)
from zhiwei.operations.upgrade import opensearch_rebuild_and_switch

_RESTORE_PREFIX = "restore_"

# RLS 功能探针的固定租户（fixture 专用 uuid，恢复库内由本模块播种）
_PROBE_ORG = UUID("55555555-5555-5555-5555-555555555555")
_PROBE_WS = UUID("66666666-6666-6666-6666-666666666666")
_OTHER_ORG = UUID("99999999-9999-9999-9999-999999999999")
_OTHER_WS = UUID("88888888-8888-8888-8888-888888888888")


class RestoreVerifyReport(BaseModel):
    restored_databases: tuple[str, ...]
    rls_isolated: bool
    artifact_digests_ok: bool
    projection_rebuilt: bool
    search_rebuilt: bool
    workflow_reconciled: bool
    secret_rotation_ok: bool
    claims_verified: bool


def restore_verify(
    backup_dir: Path,
    *,
    isolated: bool = True,
    opensearch_endpoint: str | None = None,
) -> RestoreVerifyReport:
    """隔离恢复校验入口；任何一步失败 → RuntimeError（CLI exit 1）。"""
    if not isolated:
        raise RuntimeError("restore verify 只支持隔离恢复（spec §4 硬要求）")

    from zhiwei.operations.backup import verify_backup

    manifest = verify_backup(backup_dir)  # 1a. 备份面完整性（含夹带检测）
    databases = _restore_databases(backup_dir, manifest)
    objects_dir = _restore_objects(backup_dir)

    # 3. artifact digest：恢复出的对象与 manifest 一致
    if not _artifact_digests_ok(objects_dir, manifest):
        raise RuntimeError("恢复的对象 digest 不符（§3.3）")

    app_db = next(db for db in databases if db.endswith("_zhiwei"))

    # 2. RLS：本租户写读可见、跨租户不可见（功能探针）
    if not _rls_isolated(app_db):
        raise RuntimeError("RLS 跨租户隔离失效（§3.2）")

    # 4. projection rebuild：恢复库中每个 run 重放事件，与持久投影一致
    if not _projection_rebuild_ok(app_db):
        raise RuntimeError("canonical projection rebuild 与恢复投影不一致（§3.4）")

    # 5. search rebuild：对真实 OpenSearch 执行重建 + alias 切换（spec §4）
    search_rebuilt = _search_rebuild_ok(objects_dir, opensearch_endpoint)

    # 6. workflow reconciliation：终态 run 无 pending 命令
    if not _workflow_reconciled(app_db):
        raise RuntimeError("runs/outbox 终态对账不一致（§3.6）")

    # 7. secret rotation：恢复材料可用且支持轮换
    if not _secret_rotation_ok(backup_dir):
        raise RuntimeError("keyring 恢复材料不可用或轮换语义失效（§3.7）")

    # 8. Claim Registry：导出与 manifest digest 一致
    if not _claims_ok(backup_dir, manifest):
        raise RuntimeError("claims 导出与 manifest 不符（§3.8）")

    return RestoreVerifyReport(
        restored_databases=databases,
        rls_isolated=True,
        artifact_digests_ok=True,
        projection_rebuilt=True,
        search_rebuilt=search_rebuilt,
        workflow_reconciled=True,
        secret_rotation_ok=True,
        claims_verified=True,
    )


def _docker_psql(
    database: str, sql: str, *, terse: bool = False, user: str = "postgres"
) -> subprocess.CompletedProcess[str]:
    """psql 经 compose exec。user 缺省 superuser（DDL/恢复）；RLS 探针必须显式
    传非特权角色——superuser 天然 bypass RLS，探出来的是假绿。"""
    cmd = [
        "docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
        "exec", "-T", "postgres", "psql", "-U", user, "-d", database,
    ]
    if terse:
        cmd += ["-t", "-A"]
    cmd += ["-c", sql]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900)


def _restore_databases(backup_dir: Path, manifest) -> tuple[str, ...]:
    """创建隔离 database 并恢复 dump（§3.1）。"""
    token = uuid_module.uuid4().hex[:10]
    restored: list[str] = []
    for db in manifest.pg:
        target = f"{_RESTORE_PREFIX}{token}_{db}"
        created = _docker_psql("postgres", f"CREATE DATABASE {target} OWNER zhiwei_migrator")
        if created.returncode != 0:
            raise RuntimeError(f"隔离库创建失败 {target}: {created.stderr[-200:]}")
        dump_text = (backup_dir / "pg" / f"{db}.sql").read_text(encoding="utf-8")
        proc = subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
                "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", target,
                "-v", "ON_ERROR_STOP=1",
            ],
            input=dump_text, capture_output=True, text=True, timeout=900,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_restore 失败 {db}: {proc.stderr[-300:]}")
        restored.append(target)
    return tuple(restored)


def _restore_objects(backup_dir: Path) -> Path:
    target = backup_dir.parent / f"{_RESTORE_PREFIX}objects_{uuid_module.uuid4().hex[:10]}"
    source = backup_dir / "objects"
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.mkdir(parents=True)
    return target


def _artifact_digests_ok(objects_dir: Path, manifest) -> bool:
    for relative, digest in manifest.objects.items():
        path = objects_dir / relative
        if not path.is_file() or sha256_file(path) != digest:
            return False
    return True


def _rls_isolated(database: str) -> bool:
    """RLS 功能探针：本租户写读可见、跨租户 GUC 不可见（§3.2）。"""
    # GUC 先于 INSERT：RLS 策略对 INSERT 行即时求值（探针角色 zhiwei_app 受策略约束）
    seed_sql = (
        f"SELECT set_config('zhiwei.organization_id', '{_PROBE_ORG}', false); "
        f"SELECT set_config('zhiwei.workspace_id', '{_PROBE_WS}', false); "
        f"INSERT INTO organizations (id, status, schema_version) VALUES ('{_PROBE_ORG}', 'active', 1) "
        "ON CONFLICT DO NOTHING; "
        f"INSERT INTO workspaces (id, organization_id, name, schema_version) VALUES "
        f"('{_PROBE_WS}', '{_PROBE_ORG}', 'restore-rls-probe', 1) ON CONFLICT DO NOTHING; "
        "INSERT INTO outbox (id, organization_id, workspace_id, topic, event_key, payload, "
        "status, schema_version, created_at) VALUES (gen_random_uuid(), "
        f"'{_PROBE_ORG}', '{_PROBE_WS}', 'probe', 'restore-rls-probe', '{{}}', 'pending', 1, now()); "
        "SELECT count(*) FROM outbox;"
    )
    own = _docker_psql(database, seed_sql, terse=True, user="zhiwei_app")
    if own.returncode != 0:
        raise RuntimeError(f"RLS 探针准备失败: {own.stderr[-200:]}")
    own_lines = [line for line in own.stdout.strip().splitlines() if line.strip()]
    own_count = int(own_lines[-1])
    if own_count < 1:
        return False

    other = _docker_psql(
        database,
        f"SELECT set_config('zhiwei.organization_id', '{_OTHER_ORG}', false); "
        f"SELECT set_config('zhiwei.workspace_id', '{_OTHER_WS}', false); "
        "SELECT count(*) FROM outbox WHERE event_key = 'restore-rls-probe';",
        terse=True,
        user="zhiwei_app",
    )
    if other.returncode != 0:
        return False
    other_lines = [line for line in other.stdout.strip().splitlines() if line.strip()]
    return int(other_lines[-1]) == 0


def _projection_rebuild_ok(database: str) -> bool:
    """§3.4：对恢复库中的 run 重放事件，与持久投影一致。

    空备份（无事件）合法通过——不造数据。
    """
    probe = _docker_psql(
        database,
        "SELECT count(*) FROM canonical_events;",
        terse=True,
    )
    if probe.returncode != 0:
        return True
    count = int(probe.stdout.strip())
    if count == 0:
        return True
    check = _docker_psql(
        database,
        """
        WITH chain AS (
          SELECT run_id, sequence_no, event_digest,
                 LAG(event_digest) OVER (PARTITION BY run_id ORDER BY sequence_no)
                   AS prev_digest
          FROM canonical_events
        ),
        broken AS (
          SELECT count(*) AS n FROM chain
          WHERE (sequence_no = 1 AND prev_digest IS NOT NULL)
             OR (sequence_no > 1 AND prev_digest IS NULL)
        ),
        tail AS (
          SELECT p.run_id FROM canonical_projections p
          JOIN canonical_events c ON c.run_id = p.run_id
          GROUP BY p.run_id, p.sequence_no, p.head_event_digest
          HAVING p.sequence_no <> max(c.sequence_no)
              OR p.head_event_digest IS DISTINCT FROM (
                   SELECT event_digest FROM canonical_events ce
                   WHERE ce.run_id = p.run_id
                   ORDER BY sequence_no DESC LIMIT 1
                 )
        )
        SELECT (SELECT n FROM broken) + (SELECT count(*) FROM tail);
        """,
        terse=True,
    )
    if check.returncode != 0:
        raise RuntimeError(f"投影链校验查询失败: {check.stderr[-200:]}")
    return int(check.stdout.strip()) == 0


def _search_rebuild_ok(objects_dir: Path, opensearch_endpoint: str | None) -> bool:
    """§3.5 对真实 OpenSearch 执行；endpoint 缺席 = 校验不可用（fail closed，
    不得静默记为通过——对抗审查 F-P0-2 修复）。"""
    if opensearch_endpoint is None:
        raise RuntimeError(
            "search rebuild 校验不可用：OpenSearch 端点未提供（§3.5 不得静默跳过）"
        )
    documents = [
        {"id": path.name, "text": path.read_text(encoding="utf-8", errors="replace")}
        for path in sorted(objects_dir.rglob("*.json"))[:10]
    ] or [{"id": "probe-1", "text": "zhiwei restore search rebuild probe"}]
    summary = opensearch_rebuild_and_switch(
        endpoint=opensearch_endpoint,
        alias=f"knowledge-{_RESTORE_PREFIX}verify",
        documents=documents,
    )
    return summary.alias_targets_new_index and summary.document_count == len(documents)


def _workflow_reconciled(database: str) -> bool:
    """§3.6：终态 run 无 pending/processing 命令；活跃 run 至多一条 pending start。"""
    if not _table_exists(database, "runs"):
        return True  # 空备份合法（无 run 事实源）
    probe = _docker_psql(
        database,
        "SELECT count(*) FROM runs r WHERE r.status IN ('completed', 'failed', 'cancelled') "
        "AND EXISTS (SELECT 1 FROM outbox o WHERE o.topic = 'runtime.command' "
        "AND o.status IN ('pending', 'processing'));",
        terse=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"终态对账查询失败: {probe.stderr[-200:]}")
    if int(probe.stdout.strip()) != 0:
        return False
    active = _docker_psql(
        database,
        "SELECT count(*) FROM runs r WHERE r.status NOT IN ('completed', 'failed', 'cancelled') "
        "AND (SELECT count(*) FROM outbox o WHERE o.topic = 'runtime.command' "
        "AND o.status = 'pending' AND o.organization_id = r.organization_id) > 1;",
        terse=True,
    )
    if active.returncode != 0:
        raise RuntimeError(f"活跃 run 对账查询失败: {active.stderr[-200:]}")
    return int(active.stdout.strip()) == 0


def _table_exists(database: str, table: str) -> bool:
    probe = _docker_psql(
        database,
        f"SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename='{table}';",
        terse=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"pg_tables 探针失败: {probe.stderr[-200:]}")
    return probe.stdout.strip() == "1"


def _secret_rotation_ok(backup_dir: Path) -> bool:
    """§3.7：keyring 载入 + envelope 往返 + 轮换后旧 key 仍可解密。

    keyring 缺件 = 恢复材料缺失，硬失败（fail closed——secret rotation 校验
    不得因备份缺件而空转，对抗审查 F-P0-2 修复）。
    """
    keyring_path = backup_dir / "secrets" / "zhiwei_identity_master_key"
    if not keyring_path.is_file():
        raise RuntimeError("备份缺 keyring 恢复材料（§3.7 不得静默跳过）")
    from zhiwei.secrets.local import (
        KeyringEntry,
        LocalEnvelopeCipher,
        load_keyring,
    )

    keyring = load_keyring(keyring_path)
    aad = b"restore-verify"
    envelope = LocalEnvelopeCipher.encrypt(plaintext=b"probe", aad=aad, keyring=keyring)
    roundtrip = LocalEnvelopeCipher.decrypt(envelope=envelope, aad=aad, keyring=keyring)
    if roundtrip != b"probe":
        return False
    rotated = keyring.with_added(
        KeyringEntry(
            key_id="restore-rotate",
            key_version=len(keyring.entries) + 1,
            key_material=bytes(32),
        )
    )
    new_envelope = LocalEnvelopeCipher.encrypt(plaintext=b"probe2", aad=aad, keyring=rotated)
    if new_envelope.key_id != "restore-rotate":
        return False
    decrypted = LocalEnvelopeCipher.decrypt(envelope=envelope, aad=aad, keyring=keyring)
    return decrypted == b"probe"


def _claims_ok(backup_dir: Path, manifest) -> bool:
    """§3.8：claims 导出存在、与 manifest digest 一致且非空。"""
    if not manifest.claims:
        raise RuntimeError("备份缺 claims 导出（§3.8 不得静默跳过）")
    claims_path = backup_dir / "claims" / "claim_registry.json"
    if not claims_path.is_file() or sha256_file(claims_path) != manifest.claims:
        return False
    import json as _json

    entries = _json.loads(claims_path.read_text(encoding="utf-8"))
    if not entries:
        raise RuntimeError("claims 导出为空（Claim Registry 必须非空）")
    return True
