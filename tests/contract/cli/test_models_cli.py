"""S3-T7 RED: `zhiwei models` CLI contract tests.

Verifies --help, fixture attestation mode, live preflight refusal,
and JSON output structure — all without network.
"""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest
from typer.testing import CliRunner

from zhiwei.cli.main import app

runner = CliRunner()

TRACEBACK_MARKER = "Traceback (most recent call last)"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Block all socket connections to guarantee zero network calls."""
    attempts: list[Any] = []

    def _refuse(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(address)
        raise AssertionError(f"models attest 不得发起网络连接，但尝试连接了 {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _refuse, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse, raising=True)
    return attempts


# --------------------------------------------------------------------------- --help


def test_root_help_lists_models_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "models" in result.output


def test_models_help_lists_attest() -> None:
    result = runner.invoke(app, ["models", "--help"])
    assert result.exit_code == 0
    assert "attest" in result.output


def test_models_help_lists_list() -> None:
    result = runner.invoke(app, ["models", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output


def test_attest_help_documents_format_option() -> None:
    result = runner.invoke(app, ["models", "attest", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.output
    assert "--live" in result.output


def test_list_help_documents_format_option() -> None:
    result = runner.invoke(app, ["models", "list", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.output


# --------------------------------------------------------------------------- fixture attestation — text


def test_attest_text_output_shows_qualified_count(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "attest"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    assert "fixture attestation" in result.output
    assert "profiles qualified" in result.output


def test_attest_text_output_shows_model_names(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "attest"])
    assert result.exit_code == 0
    # At least some known model names from the YAML should appear
    assert "fixture_tested" in result.output


# --------------------------------------------------------------------------- fixture attestation — JSON


def test_attest_json_output_is_valid_json(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "attest", "--format", "json"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    payload = json.loads(result.stdout)
    assert "fixture_attestations" in payload
    assert "total_profiles" in payload
    assert "qualified_count" in payload


def test_attest_json_has_correct_total(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "attest", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["total_profiles"] == 18
    assert payload["qualified_count"] == 18


def test_attest_json_each_entry_has_required_fields(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "attest", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    for att in payload["fixture_attestations"]:
        assert "id" in att
        assert "model_name" in att
        assert "endpoint_id" in att
        assert "qualification_level" in att
        assert att["qualification_level"] == "fixture_tested"
        assert "status" in att
        assert att["status"] == "valid"
        assert "probed_at" in att
        assert "valid_until" in att
        assert "capabilities_count" in att
        assert att["capabilities_count"] > 0


# --------------------------------------------------------------------------- live preflight refusal


def test_attest_live_flag_refused_without_operator(no_network: list[Any]) -> None:
    """Live attestation must be rejected without operator preflight."""
    result = runner.invoke(app, ["models", "attest", "--live"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "live" in result.output.lower() or "operator" in result.output.lower()


def test_attest_live_json_flag_refused(no_network: list[Any]) -> None:
    """Live flag + JSON format still refuses."""
    result = runner.invoke(app, ["models", "attest", "--live", "--format", "json"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


# --------------------------------------------------------------------------- no network


def test_attest_makes_no_network_calls(no_network: list[Any]) -> None:
    """Fixture attestation must not open any sockets."""
    result = runner.invoke(app, ["models", "attest"])
    assert result.exit_code == 0
    assert no_network == []


def test_attest_json_makes_no_network_calls(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "attest", "--format", "json"])
    assert result.exit_code == 0
    assert no_network == []


# --------------------------------------------------------------------------- list command


def test_list_text_output(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output


def test_list_json_output(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "list", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "profiles" in payload
    assert "total" in payload
    assert payload["total"] == 18


def test_list_json_entries_have_fields(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["models", "list", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    for p in payload["profiles"]:
        assert "id" in p
        assert "model_name" in p
        assert "endpoint_id" in p
        assert "wire_protocol" in p
        assert "verification_level" in p
        assert "context_window" in p
        assert "max_output" in p


# --------------------------------------------------------------------------- qualification level visibility


def test_attest_distinguishes_qualification_levels(no_network: list[Any]) -> None:
    """All fixture attestations should be at fixture_tested level."""
    result = runner.invoke(app, ["models", "attest", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    levels = {att["qualification_level"] for att in payload["fixture_attestations"]}
    assert levels == {"fixture_tested"}
