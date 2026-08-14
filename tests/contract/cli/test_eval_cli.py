"""S0-T6 RED: eval CLI exposes the minimal executor and sealing workflow."""

from __future__ import annotations

from typer.testing import CliRunner

from zhiwei.cli.main import app

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"


def test_root_help_registers_eval_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "eval" in result.output


def test_eval_help_registers_minimal_workflow_commands() -> None:
    result = runner.invoke(app, ["eval", "--help"])
    assert result.exit_code == 0
    for command in ("seal-empty", "run", "resume", "seal"):
        assert command in result.output


def test_eval_run_help_documents_suite_executor_and_mode() -> None:
    result = runner.invoke(app, ["eval", "run", "--help"])
    assert result.exit_code == 0
    assert "--suite" in result.output
    assert "--executor" in result.output
    assert "--mode" in result.output


def test_eval_resume_and_seal_require_an_eval_run_id() -> None:
    for command in ("resume", "seal"):
        result = runner.invoke(app, ["eval", command])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "eval" in result.output.lower() and "run" in result.output.lower()


def test_eval_resume_and_seal_help_require_explicit_tenant_scope() -> None:
    for command in ("resume", "seal"):
        result = runner.invoke(app, ["eval", command, "--help"])
        assert result.exit_code == 0
        assert "--organization-id" in result.output
        assert "--workspace-id" in result.output


def test_eval_run_rejects_unknown_mode_before_execution() -> None:
    result = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "legacy-assets",
            "--executor",
            "legacy",
            "--mode",
            "production-ish",
        ],
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "production-ish" in result.output


def test_seal_empty_help_documents_independent_check() -> None:
    result = runner.invoke(app, ["eval", "seal-empty", "--help"])
    assert result.exit_code == 0
    assert "--check" in result.output
