"""S5-T7 contract: source CLI — sync|status commands.

覆盖：
- `zhiwei source` help registers subcommands
- `zhiwei source sync --help` documents --force, --all-reference, --reconcile
- `zhiwei source status --help` documents source_id argument
- `zhiwei source sync` with a reference UUID produces JSON output
- `zhiwei source status` with a reference UUID produces JSON output
- `zhiwei source sync --all-reference` syncs all reference fixtures
- `zhiwei source sync --all-reference --reconcile` includes reconciliation flag
- Unknown subcommand fails gracefully (no traceback)
- Root help shows "source" group
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from typer.testing import CliRunner

from zhiwei.cli.main import app

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"


def _parse_json_output(output: str) -> dict[str, Any]:
    """Parse the first JSON object from CLI stdout."""
    lines = output.strip().splitlines()
    for line in lines:
        line = line.strip()
        if line.startswith("{"):
            result = json.loads(line)
            assert isinstance(result, dict)
            return result
    raise AssertionError(f"no JSON found in output: {output!r}")


def test_root_help_shows_source_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "source" in result.output


def test_source_help_registers_subcommands() -> None:
    result = runner.invoke(app, ["source", "--help"])
    assert result.exit_code == 0
    assert "sync" in result.output
    assert "status" in result.output


def test_source_sync_help_documents_options() -> None:
    result = runner.invoke(app, ["source", "sync", "--help"])
    assert result.exit_code == 0
    assert "--force" in result.output
    assert "--all-reference" in result.output
    assert "--reconcile" in result.output


def test_source_status_help_documents_source_id() -> None:
    result = runner.invoke(app, ["source", "status", "--help"])
    assert result.exit_code == 0
    assert "source_id" in result.output.lower() or "SOURCE_ID" in result.output


def test_source_sync_with_reference_uuid() -> None:
    ref_id = uuid4()
    result = runner.invoke(app, ["source", "sync", str(ref_id)])
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    data = _parse_json_output(result.output)
    assert "sync_status" in data
    assert data["sync_status"] == "completed"
    assert "source_id" in data
    assert data["source_id"] == str(ref_id)


def test_source_status_with_reference_uuid() -> None:
    ref_id = uuid4()
    result = runner.invoke(app, ["source", "status", str(ref_id)])
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    data = _parse_json_output(result.output)
    assert "source_id" in data
    assert data["source_id"] == str(ref_id)
    assert "status" in data
    assert "version_seq" in data
    assert "content_digest" in data
    assert "freshness_state" in data
    assert "acl_allowed" in data
    assert "score_breakdown" in data


def test_source_sync_all_reference() -> None:
    result = runner.invoke(app, ["source", "sync", "--all-reference"])
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    data = _parse_json_output(result.output)
    assert data["all_reference"] is True
    assert data["total"] > 0
    assert data["completed"] == data["total"]
    for r in data["results"]:
        assert r["sync_status"] == "completed"
        assert "connector" in r
        assert "reference_type" in r


def test_source_sync_all_reference_with_reconcile() -> None:
    result = runner.invoke(
        app, ["source", "sync", "--all-reference", "--reconcile"]
    )
    assert result.exit_code == 0, result.output
    assert TRACEBACK_MARKER not in result.output
    data = _parse_json_output(result.output)
    assert data["all_reference"] is True
    assert data["reconcile"] is True


def test_source_sync_requires_id_or_all_reference() -> None:
    result = runner.invoke(app, ["source", "sync"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


def test_source_sync_invalid_uuid_fails() -> None:
    result = runner.invoke(app, ["source", "sync", "not-a-uuid"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


def test_source_status_invalid_uuid_fails() -> None:
    result = runner.invoke(app, ["source", "status", "not-a-uuid"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
