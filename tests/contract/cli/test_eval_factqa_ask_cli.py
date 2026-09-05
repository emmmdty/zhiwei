"""S6：`eval run` 的 factqa-v1 / ask-v1 suite 注册面——注册在案才可执行，未知 suite fail closed。

事实源：specs/s6-evidence-ask.md §6/§7、ADR-013 决策 2。

用 sentinel 替换 `_settings_runtime`，把「suite 解析先于 runtime 依赖」变成可观测断言：
- factqa-v1 / ask-v1 必须注册进 `_KNOWN_SUITES`（Gate 命令引用的 suite id 是真实产品能力）；
- 未知 suite 必须在触碰任何 runtime 依赖（DB/ObjectStore）之前被拒绝；
- 两个 suite 的 executor 都由注册表绑定生产路径，empty/agent-runtime 不得手工指定。
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

_S6_SUITES = ("factqa-v1", "ask-v1")

_SENTINEL = "sentinel: runtime dependencies reached"


def _install_runtime_sentinel(monkeypatch: Any) -> None:
    def _reject() -> None:
        click.echo(_SENTINEL, err=True)
        raise typer.Exit(1)

    monkeypatch.setattr(evals_cli, "_settings_runtime", _reject)


def test_s6_suites_are_registered_in_cli_gate() -> None:
    for name in _S6_SUITES:
        assert name in evals_cli._KNOWN_SUITES, f"suite 未注册: {name}"


def test_eval_run_rejects_unknown_suite_before_execution(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    result = runner.invoke(app, ["eval", "run", "--suite", "factqa-v2", "--mode", "offline"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "未知 suite" in result.output
    assert _SENTINEL not in result.output, "未知 suite 的拒绝不得晚于 runtime 依赖检查"


def test_eval_run_recognizes_s6_suites(monkeypatch: Any) -> None:
    _install_runtime_sentinel(monkeypatch)
    for name in _S6_SUITES:
        result = runner.invoke(
            app, ["eval", "run", "--suite", name, "--mode", "offline"]
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "未知 suite" not in result.output, f"suite 未注册: {name}"
        assert _SENTINEL in result.output, f"suite 解析未通过: {name}"


def test_eval_run_rejects_non_default_executor_for_s6_suites(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    for name in _S6_SUITES:
        for executor_name in ("empty", "agent-runtime"):
            result = runner.invoke(
                app,
                [
                    "eval",
                    "run",
                    "--suite",
                    name,
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
