"""S11-T5 CLI 契约：`ops fault-run` 码表（docs/API.md §12.1 冻结）。

- `--help` → 0；`--dry-run` → 0（列出将执行的场景，不执行任何故障注入）；
- 未知 scenario / profile → 2；
- compose 栈未就绪 + compose backend → 3（环境不可用，显式登记语义）；
- 场景失败 → 1。
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _fixture_pg_reachable() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        return sock.connect_ex(("127.0.0.1", 55433)) == 0
    finally:
        sock.close()


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "zhiwei", *args],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=600,
    )


def test_fault_run_help_exits_zero() -> None:
    proc = _cli("ops", "fault-run", "--help")
    assert proc.returncode == 0, proc.stderr


def test_fault_run_dry_run_lists_scenarios_exit_zero() -> None:
    proc = _cli("ops", "fault-run", "--fixture", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "crash_window_2_pending_over_deadline" in proc.stdout
    assert "crash_window_11_can_intent_recheck" in proc.stdout


def test_fault_run_unknown_scenario_exit_two() -> None:
    proc = _cli("ops", "fault-run", "--fixture", "--scenario", "nonexistent")
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


def test_fault_run_unknown_profile_exit_two() -> None:
    proc = _cli("ops", "fault-run", "--profile", "nonsense", "--fixture")
    assert proc.returncode == 2


def test_fault_run_fixture_backend_runs_sealed(tmp_path: Path) -> None:
    """fixture backend 的崩溃窗口场景依赖测试 PG——环境守卫（skip 非放宽）。"""
    if not _fixture_pg_reachable():
        pytest.skip("测试 PG (55433) 不可达：崩溃窗口 fixture 场景需要 compose 栈")
    proc = _cli("ops", "fault-run", "--fixture", "--sealed",
                "--seal-dir", str(tmp_path / "seal"))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "passed" in proc.stdout
    seal_files = list((tmp_path / "seal").glob("*.json"))
    assert seal_files, "--sealed 必须产出 seal 文件"


def test_fault_run_compose_backend_without_stack_exit_three(tmp_path: Path) -> None:
    """compose backend + 栈未就绪 → 3（环境不可用；不是 0 也不是 1）。"""
    proc = _cli("ops", "fault-run", "--profile", "local-product",
                "--seal-dir", str(tmp_path / "seal2"),
                "--compose-file", "/nonexistent/compose.yaml")
    assert proc.returncode == 3, f"{proc.stdout}\n{proc.stderr}"
