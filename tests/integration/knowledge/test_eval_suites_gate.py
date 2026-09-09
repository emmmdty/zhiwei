"""S5 Gate：四个 knowledge suite 经 `zhiwei eval run --mode offline --seal` 全量执行并密封。

事实源：specs/s5-knowledge-fabric.md §8 的四条 Gate 命令、ADR-013 决策 2。

执行走生产检索路径（Retrieve TaskHandler → Knowledge Planner），全部离线、无 live 模型、
无 PG 之外的任何外部网络；密封后的 payload 必须声明 registered/terminal units 数、
executor 与生产路径标记。
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

_KNOWLEDGE_DIR = REPO_ROOT / "evals" / "knowledge"
_SUITE_FILES = {
    "knowledge-doc-v1": "doc_table_v1.jsonl",
    "knowledge-code-github-v1": "code_github_v1.jsonl",
    "knowledge-cross-source-v1": "cross_source_v1.jsonl",
    "knowledge-acl-freshness-v1": "acl_freshness_v1.jsonl",
}

ADMIN_DSN = "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
APP_DSN = "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)


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


def _line_count(suite: str) -> int:
    path = _KNOWLEDGE_DIR / _SUITE_FILES[suite]
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


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


@pytest.mark.parametrize("suite_name", sorted(_SUITE_FILES))
def test_gate_command_runs_and_seals_suite(
    suite_name: str, tmp_path: Path, no_external_network: list[object]
) -> None:
    result = CLI_RUNNER.invoke(
        app,
        ["eval", "run", "--suite", suite_name, "--mode", "offline", "--seal"],
        env=_cli_env(tmp_path / "objects"),
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["suite"] == suite_name
    assert payload["executor"] == "knowledge-retrieval"
    assert payload["production_path"] == "RetrieveTaskHandler->KnowledgePlanner"
    assert payload["registered_units"] == _line_count(suite_name)
    assert payload["terminal_units"] == payload["registered_units"]
    assert set(payload["status_counts"]) == {"completed"}
    assert payload["sealed"] is True
    assert payload["seal_digest"]
    assert no_external_network == []
