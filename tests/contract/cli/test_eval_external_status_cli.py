"""S7：`eval external-status` CLI 注册面——未知 adapter fail closed，注册在案才进入执行。

事实源：specs/s7-memory.md §8（external-status 二选一 sealed artifact）、
ADR-013 决策 2、S7 plan Task 7（CLI 注册/--help 测试）。

用 sentinel 替换 `_settings_runtime`，把「adapter 解析先于 runtime 依赖」变成可观测断言：
未知 suite 名必须在触碰任何 runtime 依赖（DB/ObjectStore）之前被拒绝。
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


def test_external_status_command_is_registered() -> None:
    result = runner.invoke(app, ["eval", "external-status", "--help"])
    assert result.exit_code == 0, result.output
    assert "--suite" in result.output
    assert "--seal" in result.output


def test_unknown_external_adapter_fails_closed_before_runtime(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    result = runner.invoke(app, ["eval", "external-status", "--suite", "not-an-adapter"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "未知 external adapter" in result.output
    assert _SENTINEL not in result.output, "拒绝不得晚于 runtime 依赖检查"


def test_registered_adapter_passes_resolution_gate(monkeypatch: Any) -> None:
    _install_runtime_sentinel(monkeypatch)
    result = runner.invoke(
        app, ["eval", "external-status", "--suite", "longmemeval-adapter"]
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "未知 external adapter" not in result.output
    assert _SENTINEL in result.output, "已注册 adapter 应进入 runtime 执行阶段"
