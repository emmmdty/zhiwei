"""S11-T4 备份（docs/operations/backup-restore.md §1/§2 冻结机制）。

事实源进备份：PG 三库（业务/identity/Temporal）pg_dump、ObjectStore 逐文件、
keyring 恢复材料、release claims 导出。Redis/OpenSearch 不是事实源——不进备份，
恢复期重建（excluded 字段是重建语义声明，不是遗漏）。

manifest 逐组件 sha256 + 自身 digest；verify 逐项重算，任何不一致/夹带 → 失败。
local_product 档经 compose 服务执行 pg_dump；外部托管 PG（production_reference）
由 operator 显式提供 DSN，本机制不自动连接。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.yaml"
COMPOSE_PROJECT = "zhiwei-local"

PG_DATABASES = ("zhiwei", "zhiwei_identity", "zhiwei_local_temporal")
EXCLUDED_TRUTH_SOURCES = ("redis", "search")
MANIFEST_NAME = "manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compose_psql(database: str, sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
            "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", database,
            "-t", "-A", "-c", sql,
        ],
        capture_output=True, text=True, timeout=300,
    )


def _compose_pg_dump(database: str, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT,
            "exec", "-T", "postgres", "pg_dump", "-U", "postgres", database,
        ],
        capture_output=True, text=True, timeout=600,
    )


class BackupManifest(BaseModel):
    created_at: str
    profile: str
    pg: dict[str, str]  # db -> dump digest
    objects: dict[str, str]  # relative path -> digest
    keyring: str
    claims: str
    excluded: tuple[str, ...] = EXCLUDED_TRUTH_SOURCES
    manifest_digest: str


def create_backup(*, output_dir: Path, profile: str = "local_product",
                  object_store_root: Path | None = None,
                  keyring_file: Path | None = None) -> BackupManifest:
    """创建备份目录并返回 manifest（§2 冻结结构）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "pg").mkdir(exist_ok=True)
    (output_dir / "objects").mkdir(exist_ok=True)
    (output_dir / "secrets").mkdir(exist_ok=True)
    (output_dir / "claims").mkdir(exist_ok=True)

    pg_digests: dict[str, str] = {}
    for db in PG_DATABASES:
        dump = _compose_pg_dump(db, output_dir)
        if dump.returncode != 0:
            raise RuntimeError(f"pg_dump {db} 失败: {dump.stderr[-300:]}")
        dump_path = output_dir / "pg" / f"{db}.sql"
        dump_path.write_text(dump.stdout, encoding="utf-8")
        pg_digests[db] = sha256_file(dump_path)

    # objects：local-product 的 ObjectStore 是 POSIX 目录（compose 卷的宿主侧
    # digest 不可直接枚举——经 postgres 容器网络不可达，故从宿主侧 compose 默认
    # 卷路径不可移植；这里以 settings 的 object store 目录为准，由调用方注入。
    object_digests: dict[str, str] = {}
    if object_store_root is not None and object_store_root.is_dir():
        for path in sorted(object_store_root.rglob("*")):
            if path.is_file():
                relative = path.relative_to(object_store_root).as_posix()
                target = output_dir / "objects" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                object_digests[relative] = sha256_file(target)

    # keyring 恢复材料是 local_product 备份的必需组件（§1 事实源边界）：
    # 缺件即失败——没有恢复材料的备份不是合法备份（对抗审查 F-P1-4 修复）。
    keyring_digest = ""
    if keyring_file is not None and keyring_file.is_file():
        target = output_dir / "secrets" / "zhiwei_identity_master_key"
        target.write_bytes(keyring_file.read_bytes())
        target.chmod(0o600)
        keyring_digest = sha256_file(target)
    elif profile == "local_product":
        raise RuntimeError(
            "备份缺 keyring 恢复材料（ZHIWEI_IDENTITY_MASTER_KEY_FILE 未配置或文件缺失）"
        )

    claims_digest = ""
    claims_export = _export_claims()
    if claims_export is not None:
        claims_path = output_dir / "claims" / "claim_registry.json"
        claims_path.write_text(claims_export, encoding="utf-8")
        claims_digest = sha256_file(claims_path)

    payload = {
        "created_at": datetime.now(tz=UTC).isoformat(),
        "profile": profile,
        "pg": pg_digests,
        "objects": object_digests,
        "keyring": keyring_digest,
        "claims": claims_digest,
        "excluded": list(EXCLUDED_TRUTH_SOURCES),
    }
    manifest = BackupManifest(
        manifest_digest=sha256_bytes(json.dumps(payload, sort_keys=True).encode()),
        **payload,
    )
    (output_dir / MANIFEST_NAME).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def _export_claims() -> str | None:
    """claims 系统级导出（maintenance 语义与 release checker 一致）。"""
    result = _compose_psql(
        "zhiwei",
        "SELECT COALESCE(json_agg(t), '[]'::json) FROM (SELECT * FROM claim_registry) t",
    )
    if result.returncode != 0:
        # claim_registry 表缺席（未迁移数据库）时导出为空表——restore 校验空集合法
        result2 = _compose_psql("zhiwei", "SELECT 1 FROM pg_tables WHERE tablename='claim_registry'")
        if result2.returncode == 0 and result2.stdout.strip() == "1":
            raise RuntimeError(f"claims 导出失败: {result.stderr[-300:]}")
        return None
    return result.stdout.strip() or "[]"


def load_manifest(backup_dir: Path) -> tuple[BackupManifest, dict[str, Any]]:
    """载入 manifest 并校验自身 digest（tamper 传播到任何字段都会在此暴露）。"""
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeError(f"manifest 缺失: {manifest_path}")
    raw = manifest_path.read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(raw)
    declared = document.pop("manifest_digest", "")
    payload = dict(document)
    computed = sha256_bytes(json.dumps(payload, sort_keys=True).encode())
    if declared != computed:
        raise RuntimeError(f"manifest digest 不符（declared={declared[:12]}… computed={computed[:12]}…）")
    payload["manifest_digest"] = declared
    return BackupManifest.model_validate(payload), payload


def verify_backup(backup_dir: Path) -> BackupManifest:
    """逐组件重算 sha256；缺失/不符/夹带 → RuntimeError（exit 1 语义）。"""
    manifest, _ = load_manifest(backup_dir)

    for db, digest in manifest.pg.items():
        dump_path = backup_dir / "pg" / f"{db}.sql"
        if not dump_path.is_file():
            raise RuntimeError(f"pg dump 缺失: {db}")
        if sha256_file(dump_path) != digest:
            raise RuntimeError(f"pg dump digest 不符: {db}")

    objects_dir = backup_dir / "objects"
    present: dict[str, str] = {}
    for path in sorted(objects_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(objects_dir).as_posix()
            present[relative] = sha256_file(path)
    if present != manifest.objects:
        raise RuntimeError("objects digest 不符或存在夹带/缺失")

    if manifest.keyring:
        keyring_path = backup_dir / "secrets" / "zhiwei_identity_master_key"
        if not keyring_path.is_file() or sha256_file(keyring_path) != manifest.keyring:
            raise RuntimeError("keyring digest 不符")

    if manifest.claims:
        claims_path = backup_dir / "claims" / "claim_registry.json"
        if not claims_path.is_file() or sha256_file(claims_path) != manifest.claims:
            raise RuntimeError("claims digest 不符")

    # 夹带检测覆盖全部组件目录（§4：manifest 之外的额外文件 → 失败）
    for component in ("pg", "secrets", "claims"):
        component_dir = backup_dir / component
        if not component_dir.is_dir():
            continue
        declared_names = set()
        if component == "pg":
            declared_names = {f"{db}.sql" for db in manifest.pg}
        elif component == "secrets" and manifest.keyring:
            declared_names = {"zhiwei_identity_master_key"}
        elif component == "claims" and manifest.claims:
            declared_names = {"claim_registry.json"}
        for path in component_dir.rglob("*"):
            if path.is_file() and path.name not in declared_names:
                raise RuntimeError(f"{component}/ 存在未登记文件（夹带）: {path.name}")

    return manifest
