"""S2-T6 契约：runtime CLI 暴露 replay-check 与 eval run（真实环境绑定）。"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zhiwei.cli.main import app

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _cli_env(object_root: Path) -> dict[str, str | None]:
    return {
        "ZHIWEI_PROFILE": "test",
        "ZHIWEI_RELEASE_MODE": "fixture_only",
        "ZHIWEI_DATABASE_URL": "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test",
        "ZHIWEI_OBJECT_STORE_ROOT": str(object_root),
        "OPENAI_API_KEY": "dummy",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "OPENAI_MODEL": None,
    }


@pytest.fixture(autouse=True)
def _require_test_database() -> Iterator[None]:
    """数据库不可达时跳过（本契约测试需要真实 PG）。"""
    try:
        with socket.create_connection(("127.0.0.1", 55432), timeout=1):
            yield
    except OSError:
        pytest.skip("test PostgreSQL at 127.0.0.1:55432 unavailable")


def test_root_help_registers_runtime_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "runtime" in result.output


def test_runtime_help_registers_replay_check_command() -> None:
    result = runner.invoke(app, ["runtime", "--help"])
    assert result.exit_code == 0
    assert "replay-check" in result.output


def test_runtime_replay_check_help_documents_all_fixtures_flag() -> None:
    result = runner.invoke(app, ["runtime", "replay-check", "--help"])
    assert result.exit_code == 0
    assert "--all-fixtures" in result.output


def test_runtime_replay_check_runs_deterministic_check_over_pg(tmp_path: Path) -> None:
    """replay-check 走真实 PG 事件序列（生产命令路径执行后重放）。"""
    result = runner.invoke(
        app,
        ["runtime", "replay-check", "--all-fixtures"],
        env=_cli_env(tmp_path / "objects"),
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["fixture_count"] == 7
    for fixture in payload["results"]:
        assert fixture["deterministic"] is True
        assert fixture["terminal"] is True
        assert fixture["chain_verified"] is True


def test_eval_run_help_documents_seal_flag() -> None:
    result = runner.invoke(app, ["eval", "run", "--help"])
    assert result.exit_code == 0
    assert "--seal" in result.output
