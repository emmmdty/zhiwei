"""S5：`eval run` 的 knowledge suite 注册面——注册在案才可执行，未知 suite fail closed。

事实源：specs/s5-knowledge-fabric.md §8、ADR-013 决策 2（Gate 命令引用的 suite id 是
真实产品能力）。

用 sentinel 替换 `_settings_runtime`，把「suite 解析先于 runtime 依赖」变成可观测断言：
- 未知 suite 必须在触碰任何 runtime 依赖（DB/ObjectStore）之前被拒绝；
- 已注册的 knowledge suite 必须能穿过 suite 解析（到达 runtime 依赖层）；
- knowledge suite 的 executor 由注册表绑定生产检索路径，empty/agent-runtime 不得指定。
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

_KNOWLEDGE_SUITES = (
    "knowledge-doc-v1",
    "knowledge-code-github-v1",
    "knowledge-cross-source-v1",
    "knowledge-acl-freshness-v1",
)

_SENTINEL = "sentinel: runtime dependencies reached"


def _install_runtime_sentinel(monkeypatch: Any) -> None:
    def _reject() -> None:
        # 与 `_fail` 同构：诊断走 stderr、非零退出，保证 sentinel 在 result.output 可观测。
        click.echo(_SENTINEL, err=True)
        raise typer.Exit(1)

    monkeypatch.setattr(evals_cli, "_settings_runtime", _reject)


def test_eval_run_rejects_unknown_suite_before_execution(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    result = runner.invoke(
        app, ["eval", "run", "--suite", "knowledge-doc-v2", "--mode", "offline"]
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "未知 suite" in result.output
    assert "knowledge-doc-v2" in result.output
    assert _SENTINEL not in result.output, "未知 suite 的拒绝不得晚于 runtime 依赖检查"


def test_cli_suite_gate_registers_the_four_knowledge_suites() -> None:
    # 注册表本体：CLI 的 suite 解析集必须包含四个 Gate suite 名。
    for name in _KNOWLEDGE_SUITES:
        assert name in evals_cli._KNOWN_SUITES, f"suite 未注册: {name}"


def test_eval_run_recognizes_registered_knowledge_suites(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    for name in _KNOWLEDGE_SUITES:
        result = runner.invoke(app, ["eval", "run", "--suite", name, "--mode", "offline"])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "未知 suite" not in result.output, f"suite 未注册: {name}"
        assert _SENTINEL in result.output, f"suite 解析未通过: {name}"


def test_eval_run_rejects_non_default_executor_for_knowledge_suites(
    monkeypatch: Any,
) -> None:
    _install_runtime_sentinel(monkeypatch)
    for name in _KNOWLEDGE_SUITES:
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
