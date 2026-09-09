"""S11-T6 CLI 契约：`ops load-run` 码表（docs/API.md §12.1 冻结）。

- `--help` → 0；`--dry-run` → 0（列出将执行的 workload，不执行）；
- 未知 workload / profile → 2；
- 环境不可用（需要 PG 而不可达且未被 --fixture 覆盖）→ 3；
- workload 失败 → 1；成功 → 0 且可 --sealed。
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PG = "postgresql+asyncpg://zhiwei_migrator:zhiwei-dev-pg-only@127.0.0.1:55433/zhiwei"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "zhiwei", *args],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=600,
    )


def test_load_run_help_exits_zero() -> None:
    proc = _cli("ops", "load-run", "--help")
    assert proc.returncode == 0, proc.stderr


def test_load_run_dry_run_lists_workloads_exit_zero() -> None:
    proc = _cli("ops", "load-run", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    for workload in ("ask", "discover", "sync-index", "eval"):
        assert workload in proc.stdout


def test_load_run_unknown_workload_exit_two() -> None:
    proc = _cli("ops", "load-run", "--workload", "nonsense", "--max-runs", "1")
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


def test_load_run_unknown_profile_exit_two() -> None:
    proc = _cli("ops", "load-run", "--profile", "nonsense", "--max-runs", "1")
    assert proc.returncode == 2


def test_load_run_bounded_smoke_exit_zero(tmp_path: Path) -> None:
    """有界 smoke：CI 可跑的子集，退出 0 并可密封（需要可达 PG；环境守卫跳过）。"""
    try:
        with socket.create_connection(("127.0.0.1", 55433), timeout=1):
            pass
    except OSError:
        pytest.skip("测试 PG (55433) 不可达：负载 smoke 需要 compose 栈（环境守卫）")
    env = dict(os.environ, ZHIWEI_DATABASE_URL=COMPOSE_PG)
    proc = subprocess.run(
        ["uv", "run", "zhiwei", "ops", "load-run", "--workload", "ask", "--concurrency", "2",
         "--max-runs", "2", "--sealed", "--seal-dir", str(tmp_path)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=600, env=env,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert (tmp_path / "load-run-seal.json").is_file()
