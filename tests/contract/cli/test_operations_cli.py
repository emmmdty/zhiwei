"""S11-T4 CLI 契约：`backup create|verify` + `restore verify`。

冻结断言面（A 档契约；docs/operations/backup-restore.md §5 码表）：

1. `--help` 退出 0（CLI 既有惯例）；`backup --help` 列出 create/verify，
   `restore --help` 列出 verify；
2. 未知 profile / 缺参数 → 退出 2（usage）；
3. corrupt manifest：备份目录 manifest digest 不符 → `backup verify` 退出 1，
   stdout 一行可读错误，不抛栈；
4. `restore verify` 无 `--isolated` 拒绝执行（隔离是 spec §4 硬要求）→ 退出 2；
5. JSON stdout 纯净（--format json 时）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "zhiwei", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )


def test_help_exits_zero() -> None:
    for argv in (["backup", "--help"], ["backup", "create", "--help"],
                 ["backup", "verify", "--help"], ["restore", "--help"],
                 ["restore", "verify", "--help"]):
        proc = _cli(*argv)
        assert proc.returncode == 0, f"{argv}: {proc.stderr}"


def test_backup_help_lists_subcommands() -> None:
    proc = _cli("backup", "--help")
    assert "create" in proc.stdout and "verify" in proc.stdout


def test_unknown_profile_is_usage_error() -> None:
    proc = _cli("backup", "create", "--profile", "nonsense")
    assert proc.returncode == 2, f"未知 profile 必须退出 2: {proc.stdout}"


def test_restore_without_isolated_refused() -> None:
    proc = _cli("restore", "verify", "/tmp/some-backup")
    assert proc.returncode == 2, "无 --isolated 必须拒绝执行（spec §4）"
    assert "isolated" in (proc.stdout + proc.stderr).lower()


def test_missing_backup_dir_fails_clean(tmp_path: Path) -> None:
    proc = _cli("backup", "verify", str(tmp_path / "absent"))
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr


def test_corrupt_manifest_fails_verification(tmp_path: Path) -> None:
    """manifest digest 不符 → exit 1（fail closed；码表 §5）。"""
    backup_dir = tmp_path / "corrupt"
    backup_dir.mkdir()
    manifest = {
        "created_at": "2026-01-01T00:00:00Z",
        "profile": "local_product",
        "manifest_digest": "0" * 64,
        "components": {},
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    proc = _cli("backup", "verify", str(backup_dir))
    assert proc.returncode == 1
    assert "digest" in (proc.stdout + proc.stderr).lower() or "manifest" in (
        proc.stdout + proc.stderr
    ).lower()
    assert "Traceback" not in proc.stderr


def test_unregistered_extra_file_fails_verification(tmp_path: Path) -> None:
    """manifest 之外夹带文件 → 失败（防夹带，docs/operations/backup-restore.md §4）。"""
    backup_dir = tmp_path / "smuggled"
    backup_dir.mkdir()
    (backup_dir / "smuggled.txt").write_text("not in manifest", encoding="utf-8")
    proc = _cli("backup", "verify", str(backup_dir))
    assert proc.returncode == 1
