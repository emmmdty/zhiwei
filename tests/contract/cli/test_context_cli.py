"""S3-T5 Contract: `zhiwei verify context` CLI.

Tests the CLI command contract: help text, output format, exit codes,
scenario dispatch, and error handling. No network calls.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from zhiwei.cli import context as context_cli
from zhiwei.cli.main import app
from zhiwei.evidence.context_verify import VerificationResult

runner = CliRunner()

TRACEBACK_MARKER = "Traceback (most recent call last)"

# --all 聚合语义的固定场景集合：与 spec s3 §6 Gate 的「全部验证场景」一一对应。
ALL_SCENARIOS = [
    "valid",
    "tampered-ir",
    "tampered-body",
    "tampered-inventory",
    "tampered-profile",
    "send-after-capture",
    "transition",
]


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


# --------------------------------------------------------------------------- --all aggregate


def test_all_scenarios_execute_and_exit_zero() -> None:
    """--all 依次执行全部 7 个场景；内置场景集本身全为检测成功，聚合必须 exit 0。"""
    result = runner.invoke(app, ["verify", "context", "--all", "--format", "json"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    entries = payload["scenarios"]
    assert [e["scenario"] for e in entries] == ALL_SCENARIOS
    assert all(e["ok"] is True for e in entries)
    assert all(e["checks"] >= 1 for e in entries)
    assert all(e["checks_failed"] == 0 for e in entries)


def test_all_text_output_lists_every_scenario() -> None:
    result = runner.invoke(app, ["verify", "context", "--all", "--format", "text"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    assert result.output.count("PASS") == len(ALL_SCENARIOS)
    for name in ALL_SCENARIOS:
        assert name in result.output


def _injected_failure(scenario: str) -> VerificationResult:
    """构造单场景必 FAIL 的结果，用于验证聚合退出码与汇总输出。"""
    return VerificationResult(
        ok=False,
        checks=[{"id": "body_sha256_match", "ok": False, "detail": f"injected failure in {scenario}"}],
        manifest_id="fixture-manifest-001",
    )


def test_all_single_failure_exits_one_and_names_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    """任一场景 FAIL → exit 1；其余场景仍被执行且出现在汇总里。"""
    real_run = context_cli._run_scenario

    def fake_run(scenario: str) -> VerificationResult:
        if scenario == "tampered-body":
            return _injected_failure(scenario)
        return real_run(scenario)

    monkeypatch.setattr(context_cli, "_run_scenario", fake_run)
    result = runner.invoke(app, ["verify", "context", "--all", "--format", "json"])
    assert result.exit_code == 1
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    by_name = {e["scenario"]: e for e in payload["scenarios"]}
    assert set(by_name) == set(ALL_SCENARIOS)
    assert by_name["tampered-body"]["ok"] is False
    assert by_name["tampered-body"]["checks_failed"] >= 1
    for name in ALL_SCENARIOS:
        if name != "tampered-body":
            assert by_name[name]["ok"] is True


def test_all_single_failure_marked_in_text(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = context_cli._run_scenario

    def fake_run(scenario: str) -> VerificationResult:
        if scenario == "tampered-profile":
            return _injected_failure(scenario)
        return real_run(scenario)

    monkeypatch.setattr(context_cli, "_run_scenario", fake_run)
    result = runner.invoke(app, ["verify", "context", "--all", "--format", "text"])
    assert result.exit_code == 1
    assert "tampered-profile" in result.output
    assert "FAIL" in result.output
    assert result.output.count("PASS") == len(ALL_SCENARIOS) - 1


def test_all_conflicts_with_explicit_scenario() -> None:
    """--all 与显式 --scenario 并存是歧义输入，必须 fail closed 拒绝而非静默取其一。"""
    result = runner.invoke(app, ["verify", "context", "--all", "--scenario", "valid"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
