"""S8：`eval run` 的 discover suite 注册面（specs/s8 §8 Gate，ADR-013 决策 2）。

与 S7 memory suite 同款：未知 suite 在触碰任何 runtime 依赖之前 fail closed；
executor 由注册表绑定生产路径，不得手工指定 empty/agent-runtime。
"""

from __future__ import annotations

from typing import Any

import click
import pytest
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


def test_discover_suites_are_registered_in_cli_gate() -> None:
    assert "numeric-risk-v1" in evals_cli._KNOWN_SUITES
    assert "discover-blind-v1" in evals_cli._KNOWN_SUITES


def test_eval_run_rejects_unknown_discover_suite_before_execution(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    result = runner.invoke(app, ["eval", "run", "--suite", "numeric-risk-v2", "--mode", "offline"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "未知 suite" in result.output
    assert _SENTINEL not in result.output, "未知 suite 的拒绝不得晚于 runtime 依赖检查"


@pytest.mark.parametrize("suite", ["numeric-risk-v1", "discover-blind-v1"])
def test_eval_run_recognizes_discover_suites(monkeypatch: Any, suite: str) -> None:
    _install_runtime_sentinel(monkeypatch)
    result = runner.invoke(app, ["eval", "run", "--suite", suite, "--mode", "offline"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "未知 suite" not in result.output
    assert _SENTINEL in result.output, "suite 解析未通过"


@pytest.mark.parametrize("suite", ["numeric-risk-v1", "discover-blind-v1"])
def test_eval_run_rejects_non_default_executor_for_discover_suites(
    monkeypatch: Any, suite: str
) -> None:
    _install_runtime_sentinel(monkeypatch)
    for executor_name in ("empty", "agent-runtime"):
        result = runner.invoke(
            app,
            ["eval", "run", "--suite", suite, "--executor", executor_name, "--mode", "offline"],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert executor_name in result.output
        assert _SENTINEL not in result.output, "executor 拒绝不得晚于 runtime 依赖检查"
