"""`eval run` 的 live 门禁契约（S11 live 演示任务，RED）。

- mode=live 需要 --operator-token（显式 operator 触发，AGENTS.md「live 只由
  operator 显式触发」的 CLI 落点）；缺 token 在触碰 DB/runtime 之前拒绝；
- live-gated suite 拒绝 fixture/offline 模式（live suite 的离线运行没有语义，
  只会造成口径混淆——fail closed）；
- help 文档化 --operator-token。
"""

from __future__ import annotations

from typer.testing import CliRunner

from zhiwei.cli.main import app

runner = CliRunner()

TRACEBACK_MARKER = "Traceback (most recent call last)"


class TestLiveGate:
    def test_help_documents_operator_token(self) -> None:
        result = runner.invoke(app, ["eval", "run", "--help"])
        assert result.exit_code == 0, result.output
        assert "--operator-token" in result.output

    def test_live_without_operator_token_refused_before_runtime_deps(self) -> None:
        result = runner.invoke(
            app,
            ["eval", "run", "--suite", "live-synthesis-v1", "--mode", "live"],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "operator" in result.output.lower()

    def test_live_gated_suite_refuses_offline_mode(self) -> None:
        result = runner.invoke(
            app,
            [
                "eval", "run",
                "--suite", "live-synthesis-v1",
                "--mode", "offline",
                "--operator-token", "operator-token-1",
            ],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "live" in result.output

    def test_unknown_suite_still_refused_before_db(self) -> None:
        result = runner.invoke(
            app,
            [
                "eval", "run",
                "--suite", "no-such-suite",
                "--mode", "live",
                "--operator-token", "operator-token-1",
            ],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
