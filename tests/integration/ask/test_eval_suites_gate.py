"""S6 Gate：factqa-v1 / ask-v1 经 `zhiwei eval run --seal` 全量执行并密封。

事实源：specs/s6-evidence-ask.md §6/§7 的 Gate 命令、ADR-013 决策 2。

factqa-v1 走生产 Evidence 路径（冻结 snapshot 重放 → QueryReplay → verifier），
ask-v1 走生产 Runtime 命令路径（RunCommandService → Temporal dev server →
AgentRunWorkflow）。全部离线、无 live 模型、无 PG/Temporal 之外的任何外部网络；
密封后的 payload 必须声明 registered/terminal units 数、executor 与生产路径标记。
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from typer.testing import CliRunner

from zhiwei.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_RUNNER = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"

ADMIN_DSN = "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
APP_DSN = "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

_FACTQA_UNIT_COUNT = 120  # 冻结题集行数（120 题 = 112 个 independence unit）
_ASK_UNIT_COUNT = 6  # ask-v1 行为场景数


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN)
    config.attributes["database_url"] = ADMIN_DSN
    command.upgrade(config, "head")
    yield


@pytest.fixture
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    refused: list[object] = []

    def guard_connect(self: socket.socket, address: object) -> object:
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == 55432:
            return original_connect(self, address)
        refused.append(address)
        raise AssertionError(f"eval CLI attempted external network access: {address!r}")

    def guard_connect_ex(self: socket.socket, address: object) -> int:
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == 55432:
            return original_connect_ex(self, address)
        refused.append(address)
        raise AssertionError(f"eval CLI attempted external network access: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)
    return refused


def _cli_env(object_root: Path) -> dict[str, str | None]:
    return {
        "ZHIWEI_PROFILE": "test",
        "ZHIWEI_RELEASE_MODE": "fixture_only",
        "ZHIWEI_DATABASE_URL": APP_URL,
        "ZHIWEI_OBJECT_STORE_ROOT": str(object_root),
        "OPENAI_API_KEY": None,
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "OPENAI_MODEL": None,
    }


def test_gate_command_runs_and_seals_factqa_v1(
    tmp_path: Path, no_external_network: list[object]
) -> None:
    result = CLI_RUNNER.invoke(
        app,
        ["eval", "run", "--suite", "factqa-v1", "--mode", "fixture", "--seal"],
        env=_cli_env(tmp_path / "objects"),
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["suite"] == "factqa-v1"
    assert payload["executor"] == "evidence-sql-replay"
    assert payload["production_path"] == (
        "FrozenSnapshotReplay->QueryReplayRef->EvidenceVerifier"
    )
    assert payload["registered_units"] == _FACTQA_UNIT_COUNT
    assert payload["terminal_units"] == payload["registered_units"]
    assert set(payload["status_counts"]) == {"completed"}
    assert payload["sealed"] is True
    assert payload["seal_digest"]
    assert no_external_network == []


def test_gate_command_runs_and_seals_ask_v1(
    tmp_path: Path, no_external_network: list[object]
) -> None:
    result = CLI_RUNNER.invoke(
        app,
        ["eval", "run", "--suite", "ask-v1", "--mode", "offline", "--seal"],
        env=_cli_env(tmp_path / "objects"),
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["suite"] == "ask-v1"
    assert payload["executor"] == "agent-runtime"
    assert payload["production_path"] == (
        "RunCommandService->AgentRunWorkflow->AskTaskGraph"
    )
    assert payload["registered_units"] == _ASK_UNIT_COUNT
    assert payload["terminal_units"] == payload["registered_units"]
    assert set(payload["status_counts"]) == {"completed"}
    assert payload["sealed"] is True
    assert payload["seal_digest"]
    assert no_external_network == []
