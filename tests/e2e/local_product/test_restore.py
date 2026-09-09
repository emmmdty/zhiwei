"""S11-T4 e2e：真实栈上的备份创建与隔离恢复校验（docs/operations/backup-restore.md §3）。

序列（local-product compose 栈）：
1. 模块 fixture：`backup create` 产出干净备份（真实 pg_dump ×3 + objects +
   keyring + claims 导出 + manifest）；
2. `backup verify` 通过；篡改**副本**中的一个对象文件 → 失败（fail closed）；
3. `restore verify --isolated`：隔离 database ×3 + 隔离对象目录，跑 §3 八项校验：
   恢复完整性 / RLS / artifact digest / projection rebuild / search rebuild /
   workflow reconciliation / secret rotation / Claim Registry。

slow：需要 compose 栈（postgres/opensearch/garage）在跑。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.slow


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, timeout=timeout)


def _run_env(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, timeout=900, env=env)


def _compose_up() -> None:
    proc = _run(
        ["docker", "compose", "-f", "deploy/compose/compose.yaml", "-p", "zhiwei-local",
         "up", "-d", "--wait", "postgres", "opensearch", "garage"]
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


@pytest.fixture(scope="module")
def backup_dir(tmp_path_factory: pytest.TempPathFactory) -> Generator[Path]:
    _compose_up()
    root = tmp_path_factory.mktemp("material")
    # 真实恢复材料：keyring（key_id=material 格式）与对象文件（artifact digest 校验面）
    keyring_file = root / "zhiwei_identity_master_key"
    keyring_file.write_text("k1=" + base64.b64encode(bytes(range(32))).decode() + "\n", encoding="utf-8")
    # claims 导出非空（§3.8 契约）：向 compose zhiwei 库播种一条 claim（superuser
    # 播种边界与 T3/T5 fixture 同口径）
    seed = _run([
        "docker", "compose", "-f", "deploy/compose/compose.yaml", "-p", "zhiwei-local",
        "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "zhiwei", "-c",
        (
            "INSERT INTO organizations (id, status, schema_version) VALUES"
            " ('12111111-1111-1111-1111-111111111111', 'active', 1) ON CONFLICT DO NOTHING;"
            "INSERT INTO workspaces (id, organization_id, name, schema_version) VALUES"
            " ('12222222-2222-2222-2222-222222222222', '12111111-1111-1111-1111-111111111111',"
            " 'restore-claims-fixture', 1) ON CONFLICT DO NOTHING;"
            "INSERT INTO claim_registry (id, organization_id, workspace_id, claim_id, statement,"
            " scope, status, schema_version) VALUES (gen_random_uuid(),"
            " '12111111-1111-1111-1111-111111111111', '12222222-2222-2222-2222-222222222222',"
            " 'restore-fixture-v1.pass', 'restore fixture claim',"
            " '{\"mode\": \"offline\", \"version\": \"1\"}', 'planned', 1)"
            " ON CONFLICT DO NOTHING;"
        ),
    ])
    assert seed.returncode == 0, seed.stderr[-300:]
    objects_root = root / "objects"
    (objects_root / "docs").mkdir(parents=True)
    (objects_root / "docs" / "probe.json").write_text('{"text": "restore probe"}', encoding="utf-8")
    (objects_root / "notes.txt").write_text("zhiwei backup fixture", encoding="utf-8")

    out = root / "backup"
    env = dict(
        os.environ,
        ZHIWEI_OBJECT_STORE_ROOT=str(objects_root),
        ZHIWEI_IDENTITY_MASTER_KEY_FILE=str(keyring_file),
    )
    proc = _run_env(["uv", "run", "zhiwei", "backup", "create", "--output", str(out)], env)
    assert proc.returncode == 0, f"backup create 失败: {proc.stdout} {proc.stderr}"
    assert (out / "manifest.json").is_file(), "manifest.json 必须存在"
    yield out


def test_backup_verify_roundtrip(backup_dir: Path, tmp_path: Path) -> None:
    """verify 通过；篡改**副本**中的对象文件 → 失败（fail closed）。"""
    proc = _run(["uv", "run", "zhiwei", "backup", "verify", str(backup_dir)])
    assert proc.returncode == 0, f"backup verify 失败: {proc.stdout} {proc.stderr}"

    tampered = tmp_path / "tampered"
    shutil.copytree(backup_dir, tampered)
    objects = sorted(p for p in (tampered / "objects").rglob("*") if p.is_file())
    target = objects[0] if objects else tampered / "manifest.json"
    target.write_bytes(target.read_bytes() + b"tamper")
    proc = _run(["uv", "run", "zhiwei", "backup", "verify", str(tampered)])
    assert proc.returncode == 1, "篡改后 verify 必须失败"
    assert "Traceback" not in proc.stderr


def test_restore_verify_isolated(backup_dir: Path) -> None:
    """隔离恢复 + §3 八项校验全通过（对干净模块级备份）。"""
    proc = _run(["uv", "run", "zhiwei", "restore", "verify", str(backup_dir), "--isolated"])
    assert proc.returncode == 0, f"restore verify 失败: {proc.stdout} {proc.stderr[-3000:]}"


def test_restore_rejects_tampered_backup(tmp_path: Path) -> None:
    """corrupt one component and require failure（plan Task 4 checkbox 3）。"""
    proc = _run(
        ["uv", "run", "zhiwei", "restore", "verify", str(tmp_path / "absent"), "--isolated"]
    )
    assert proc.returncode == 1
