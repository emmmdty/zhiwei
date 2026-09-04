"""S4-T8 contract: provider CLI — inspect/test/admit commands。

覆盖：
- `zhiwei provider` help registers subcommands
- `zhiwei provider inspect` prints provider info as JSON
- `zhiwei provider test --all-reference --sealed` is the S4 Gate command
- `zhiwei provider test` requires either ID or --all-reference
- `zhiwei provider test --all-reference` requires --sealed
- `zhiwei provider admit` records admission decision
- Unknown subcommand fails gracefully (no traceback)
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from zhiwei.cli.main import app

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"


def _parse_json_output(output: str) -> dict:
    """Parse the first JSON object from CLI stdout."""
    lines = output.strip().splitlines()
    for line in lines:
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON found in output: {output!r}")


def test_provider_help_registers_subcommands() -> None:
    result = runner.invoke(app, ["provider", "--help"])
    assert result.exit_code == 0
    assert "inspect" in result.output
    assert "test" in result.output
    assert "admit" in result.output


def test_provider_inspect_outputs_json() -> None:
    result = runner.invoke(app, ["provider", "inspect", "00000000-0000-0000-0000-000000000001"])
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    data = _parse_json_output(result.output)
    assert "provider_id" in data
    assert "name" in data
    assert "status" in data
    assert "content_digest" in data


def test_provider_inspect_rejects_invalid_id() -> None:
    result = runner.invoke(app, ["provider", "inspect", "not-a-uuid"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


def test_provider_test_all_reference_sealed_gate() -> None:
    """S4 Gate command: provider test --all-reference --sealed."""
    result = runner.invoke(
        app,
        ["provider", "test", "--all-reference", "--sealed"],
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    data = _parse_json_output(result.output)
    assert data["all_reference"] is True
    assert data["sealed"] is True
    assert data["total"] > 0
    assert data["passed"] == data["total"]
    for r in data["results"]:
        assert r["status"] == "passed"
        assert r["runner"] == "prebuilt"


def test_provider_test_requires_id_or_all_reference() -> None:
    result = runner.invoke(app, ["provider", "test"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


def test_provider_test_all_reference_requires_sealed() -> None:
    result = runner.invoke(app, ["provider", "test", "--all-reference"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "sealed" in result.output.lower()


def test_provider_test_single_provider() -> None:
    result = runner.invoke(
        app,
        ["provider", "test", "--provider-id", "00000000-0000-0000-0000-000000000001"],
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    data = _parse_json_output(result.output)
    assert data["total"] == 1
    assert data["results"][0]["status"] == "passed"


def test_provider_admit_approve() -> None:
    result = runner.invoke(
        app,
        [
            "provider",
            "admit",
            "00000000-0000-0000-0000-000000000001",
            "--decision",
            "approve",
            "--role",
            "capability_publisher",
        ],
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    data = _parse_json_output(result.output)
    assert data["decision"] == "approved"
    assert data["role"] == "capability_publisher"


def test_provider_admit_reject() -> None:
    result = runner.invoke(
        app,
        [
            "provider",
            "admit",
            "00000000-0000-0000-0000-000000000001",
            "--decision",
            "reject",
            "--reason",
            "security concern",
        ],
    )
    assert result.exit_code == 0, result.output
    data = _parse_json_output(result.output)
    assert data["decision"] == "rejected"


def test_provider_admit_invalid_decision() -> None:
    result = runner.invoke(
        app,
        [
            "provider",
            "admit",
            "00000000-0000-0000-0000-000000000001",
            "--decision",
            "maybe",
        ],
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


def test_provider_admit_invalid_id() -> None:
    result = runner.invoke(
        app,
        [
            "provider",
            "admit",
            "not-a-uuid",
            "--decision",
            "approve",
        ],
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


def test_root_help_shows_provider() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "provider" in result.output
