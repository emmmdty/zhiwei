"""S7：`eval run` 的 enterprise-memory-v1 suite 注册面。

事实源：specs/s7-memory.md §8（Gate 命令）、ADR-013 决策 2。

用 sentinel 替换 `_settings_runtime`，把「suite 解析先于 runtime 依赖」变成可观测断言；
executor 由注册表绑定生产路径（memory-lifecycle），empty/agent-runtime 不得手工指定。
"""

from __future__ import annotations

from typing import Any

import click
import typer
from typer.testing import CliRunner

import zhiwei.cli.evals as evals_cli
from zhiwei.cli.main import app

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"

_SENTINEL = "sentinel: runtime dependencies reached"


def _install_runtime_sentinel(monkeypatch: Any) -> None:
    def _reject() -> None:
        click.echo(_SENTINEL, err=True)
        raise typer.Exit(1)

    monkeypatch.setattr(evals_cli, "_settings_runtime", _reject)


def test_enterprise_memory_suite_is_registered_in_cli_gate() -> None:
    assert "enterprise-memory-v1" in evals_cli._KNOWN_SUITES


def test_eval_run_rejects_unknown_memory_suite_before_execution(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    result = runner.invoke(
        app, ["eval", "run", "--suite", "enterprise-memory-v2", "--mode", "offline"]
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "未知 suite" in result.output
    assert _SENTINEL not in result.output, "未知 suite 的拒绝不得晚于 runtime 依赖检查"


def test_eval_run_recognizes_enterprise_memory_suite(monkeypatch: Any) -> None:
    _install_runtime_sentinel(monkeypatch)
    result = runner.invoke(
        app, ["eval", "run", "--suite", "enterprise-memory-v1", "--mode", "offline"]
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "未知 suite" not in result.output
    assert _SENTINEL in result.output, "suite 解析未通过"


def test_eval_run_rejects_non_default_executor_for_memory_suite(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    for executor_name in ("empty", "agent-runtime"):
        result = runner.invoke(
            app,
            [
                "eval",
                "run",
                "--suite",
                "enterprise-memory-v1",
                "--executor",
                executor_name,
                "--mode",
                "offline",
            ],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert executor_name in result.output
        assert _SENTINEL not in result.output, "executor 拒绝不得晚于 runtime 依赖检查"
