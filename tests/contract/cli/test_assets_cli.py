"""S0-T6 RED: frozen asset lock CLI is check-by-default and write-explicit."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner
from zhiwei.cli.main import app

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"


def _asset_tree(root: Path, content: bytes = b"frozen\n") -> tuple[Path, Path]:
    evals = root / "evals"
    artifact = evals / "questions" / "fixture.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    checksum = evals / "CHECKSUMS.sha256"
    checksum.write_text(
        f"{hashlib.sha256(content).hexdigest()}  evals/questions/fixture.txt\n",
        encoding="utf-8",
    )
    return artifact, checksum


def test_root_help_registers_assets_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "assets" in result.output


def test_assets_help_registers_lock_command() -> None:
    result = runner.invoke(app, ["assets", "--help"])
    assert result.exit_code == 0
    assert "lock" in result.output


def test_assets_lock_defaults_to_read_only_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, checksum = _asset_tree(tmp_path)
    before = checksum.read_bytes()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["assets", "lock"])

    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    assert "check" in result.output.lower()
    assert checksum.read_bytes() == before


def test_assets_lock_check_detects_drift_without_rewriting_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, checksum = _asset_tree(tmp_path)
    before = checksum.read_bytes()
    artifact.write_bytes(b"tampered\n")

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["assets", "lock", "--check"])

    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "fixture.txt" in result.output
    assert checksum.read_bytes() == before


def test_assets_lock_write_requires_the_explicit_write_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, checksum = _asset_tree(tmp_path)
    artifact.write_bytes(b"operator replacement\n")

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["assets", "lock", "--write"])

    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert checksum.read_text(encoding="utf-8") == (
        f"{expected}  evals/questions/fixture.txt\n"
    )
    assert "write" in result.output.lower()


def test_assets_lock_rejects_check_and_write_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _asset_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["assets", "lock", "--check", "--write"])
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
