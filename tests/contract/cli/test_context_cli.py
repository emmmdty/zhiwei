"""S3-T5 Contract: `zhiwei verify context` CLI.

Tests the CLI command contract: help text, output format, exit codes,
scenario dispatch, and error handling. No network calls.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from zhiwei.cli.main import app

runner = CliRunner()

TRACEBACK_MARKER = "Traceback (most recent call last)"


# --------------------------------------------------------------------------- --help


def test_root_help_lists_verify_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "verify" in result.output


def test_verify_help_lists_context() -> None:
    result = runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "context" in result.output


def test_context_help_documents_format_option() -> None:
    result = runner.invoke(app, ["verify", "context", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.output
    assert "--scenario" in result.output


# --------------------------------------------------------------------------- Valid scenario


def test_valid_scenario_exit_code_zero() -> None:
    result = runner.invoke(app, ["verify", "context", "--scenario", "valid", "--format", "json"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["scenario"] == "valid"
    assert len(payload["checks"]) >= 5


def test_valid_scenario_all_checks_pass() -> None:
    result = runner.invoke(app, ["verify", "context", "--scenario", "valid", "--format", "json"])
    payload = json.loads(result.stdout)
    assert all(c["ok"] for c in payload["checks"])


# --------------------------------------------------------------------------- Tamper scenarios


@pytest.mark.parametrize("scenario", [
    "tampered-ir",
    "tampered-body",
    "tampered-inventory",
    "tampered-profile",
    "send-after-capture",
])
def test_tamper_scenarios_exit_code_zero(scenario: str) -> None:
    """Tamper detection scenarios should exit 0 (detection succeeded)."""
    result = runner.invoke(app, ["verify", "context", "--scenario", scenario, "--format", "json"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["scenario"] == scenario


def test_transition_scenario_exit_code_zero() -> None:
    result = runner.invoke(app, ["verify", "context", "--scenario", "transition", "--format", "json"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


# --------------------------------------------------------------------------- Text output


def test_text_output_format() -> None:
    result = runner.invoke(app, ["verify", "context", "--scenario", "valid", "--format", "text"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    assert "PASS" in result.output
    assert "body_sha256_match" in result.output


def test_text_tamper_shows_pass() -> None:
    result = runner.invoke(app, ["verify", "context", "--scenario", "tampered-ir", "--format", "text"])
    assert result.exit_code == 0
    assert "PASS" in result.output
    assert "ir_tamper_detected" in result.output


# --------------------------------------------------------------------------- JSON is pipeable


def test_json_output_is_valid_json() -> None:
    result = runner.invoke(app, ["verify", "context", "--scenario", "valid", "--format", "json"])
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert set(payload.keys()) >= {"scenario", "ok", "checks", "manifest_id", "summary"}


# --------------------------------------------------------------------------- Error handling


def test_unknown_scenario_exits_nonzero() -> None:
    result = runner.invoke(app, ["verify", "context", "--scenario", "nonexistent"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
