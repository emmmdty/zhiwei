"""S7 Gate：enterprise-memory-v1 全量执行密封 + external-status 二选一 sealed artifact。

事实源：specs/s7-memory.md §8 的 Gate 命令、ADR-009、ADR-013 决策 2。

- `eval run --suite enterprise-memory-v1 --mode offline --seal`：全部 units 经生产
  memory 路径（WriteMemoryCandidateHandler → policy → confirm/conflict/forget 服务）
  执行并密封；全程离线、无 live 模型、无 PG 之外的任何外部网络。
- `eval external-status --suite longmemeval-adapter --seal`：仓库无 LongMemEval 数据 →
  unavailable artifact（机器可读原因 + LongMemEval claim 保持 planned/unavailable）。
  available 分支用注入的 fixture 清单驱动（--config），验证「附许可/version/checksum
  并实际运行」的完整密封路径。
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

_UNIT_FIXTURE_LINES = (
    '{"question_id": "q-1", "question_type": "single-session-user", '
    '"question": "q", "answer": "a"}\n'
    '{"question_id": "q-2", "question_type": "multi-session", '
    '"question": "q", "answer": "a"}\n'
)


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


def test_gate_enterprise_memory_v1_runs_and_seals(
    tmp_path: Path, no_external_network: list[object]
) -> None:
    result = CLI_RUNNER.invoke(
        app,
        ["eval", "run", "--suite", "enterprise-memory-v1", "--mode", "offline", "--seal"],
        env=_cli_env(tmp_path / "objects"),
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["suite"] == "enterprise-memory-v1"
    assert payload["executor"] == "memory-lifecycle"
    assert "WriteMemoryCandidateHandler" in payload["production_path"]
    assert payload["registered_units"] >= 12
    assert payload["terminal_units"] == payload["registered_units"]
    assert set(payload["status_counts"]) == {"completed"}
    assert payload["sealed"] is True
    assert payload["seal_digest"]
    assert no_external_network == []


def test_gate_external_status_seals_unavailable_artifact(
    tmp_path: Path, no_external_network: list[object]
) -> None:
    result = CLI_RUNNER.invoke(
        app,
        ["eval", "external-status", "--suite", "longmemeval-adapter", "--seal"],
        env=_cli_env(tmp_path / "objects"),
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    # 仓库无 LongMemEval 数据 → unavailable + 机器可读缺失原因
    assert payload["external_status"] == "unavailable"
    codes = {reason["code"] for reason in payload["reasons"]}
    assert codes
    assert all(reason["path"] for reason in payload["reasons"])
    # LongMemEval claim 保持 planned/unavailable（机器可读字段）
    assert payload["claim"] == {
        "benchmark": "longmemeval",
        "claim_status": "planned/unavailable",
    }
    assert payload["sealed"] is True
    assert payload["seal_digest"]
    assert no_external_network == []


def test_gate_external_status_available_branch_seals_executed_artifact(
    tmp_path: Path, no_external_network: list[object]
) -> None:
    """available 分支：fixture 数据 + 注入清单 → 附许可/version/checksum 并实际运行。"""
    root = tmp_path / "fixture-root"
    data_dir = root / "evals/external/longmemeval/data"
    data_dir.mkdir(parents=True)
    (data_dir / "episodes.jsonl").write_text(_UNIT_FIXTURE_LINES, encoding="utf-8")
    (root / "evals/external/longmemeval/LICENSE").write_text("CC-BY-4.0\n", encoding="utf-8")
    (root / "evals/external/longmemeval/VERSION").write_text("v1.0-fixture\n", encoding="utf-8")
    manifest = tmp_path / "external_adapters.yaml"
    manifest.write_text(
        "adapters:\n"
        "  - name: longmemeval-adapter\n"
        "    benchmark: longmemeval\n"
        "    claim_id: longmemeval\n"
        "    data_dir: evals/external/longmemeval/data\n"
        "    data_glob: '*.jsonl'\n"
        "    license_file: evals/external/longmemeval/LICENSE\n"
        "    version_file: evals/external/longmemeval/VERSION\n"
        "    required_fields: [question_id, question_type, question, answer]\n",
        encoding="utf-8",
    )
    # adapter 清单中的路径相对 data-root 解析：显式注入 fixture 数据根。
    result = CLI_RUNNER.invoke(
        app,
        [
            "eval",
            "external-status",
            "--suite",
            "longmemeval-adapter",
            "--seal",
            "--config",
            str(manifest),
            "--data-root",
            str(root),
        ],
        env=_cli_env(tmp_path / "objects"),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["external_status"] == "available"
    assert payload["reasons"] == []
    assert payload["run_kind"] == "corpus-integrity"
    assert payload["claim"]["claim_status"] == "planned/unavailable"
    assert payload["sealed"] is True
    assert no_external_network == []
