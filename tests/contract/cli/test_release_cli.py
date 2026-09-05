"""S9-T5：`release check` / `release attest` CLI 契约。

覆盖：命令注册、`--strict` 下坏表面的确定性失败与干净表面的 exit 0、
DB/表面缺失 fail closed（绝不 exit 0）、dry-run 全程零写入（不创建/不改文件）、
`--sign` 的显式密钥门槛与 attestation 写出。

registry 读取通过 `_load_registry` seam 注入（与 evals CLI 的 `_settings_runtime`
sentinel 同型）；DB 连接本身由 integration 层覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

import zhiwei.cli.release as release_cli
from zhiwei.cli.main import app

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"

FAKE_DSN = "postgresql://maintenance@127.0.0.1:5/zhiwei"
COMMIT = "a" * 40
GENERATED_AT = "2026-09-06T00:00:00+00:00"

BAD_README = "# Demo\n\n<!-- claims:start -->\nfactqa accuracy 0.87\n<!-- claims:end -->\n"
GOOD_README = "# Demo\n\n历史记录里提到 accuracy 0.42，不属于声明表。\n"


def _json_payload(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON payload in output: {output!r}")


def _patch_registry(monkeypatch: Any, registry: dict[str, Any] | None = None) -> list[str]:
    calls: list[str] = []

    def _fake_load_registry(raw_dsn: str) -> dict[str, Any]:
        calls.append(raw_dsn)
        return registry or {}

    monkeypatch.setattr(release_cli, "_load_registry", _fake_load_registry)
    return calls


class TestRegistration:
    def test_release_group_registers_check_and_attest(self) -> None:
        result = runner.invoke(app, ["release", "--help"])
        assert result.exit_code == 0, result.output
        assert "check" in result.output
        assert "attest" in result.output

    def test_check_help_documents_options(self) -> None:
        result = runner.invoke(app, ["release", "check", "--help"])
        assert result.exit_code == 0, result.output
        for option in ("--strict", "--paths", "--stale-after-days"):
            assert option in result.output

    def test_attest_help_documents_options(self) -> None:
        result = runner.invoke(app, ["release", "attest", "--help"])
        assert result.exit_code == 0, result.output
        for option in ("--dry-run", "--sign", "--key-file", "--output"):
            assert option in result.output


class TestCheck:
    def test_strict_flags_fabricated_number_deterministically(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_registry(monkeypatch)
        surface = tmp_path / "README.md"
        surface.write_text(BAD_README, encoding="utf-8")
        args = [
            "release", "check", "--strict",
            "--paths", str(surface),
            "--db-dsn", FAKE_DSN,
            "--now", "2026-09-06",
        ]
        first = runner.invoke(app, args)
        second = runner.invoke(app, args)
        assert first.exit_code == 1, first.output
        assert TRACEBACK_MARKER not in first.output
        assert first.output == second.output
        payload = _json_payload(first.output)
        assert payload["findings"], "fabricated number must produce findings"
        assert payload["findings"][0]["code"] == "unsupported_number"

    def test_clean_surface_exits_zero_under_strict(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_registry(monkeypatch)
        surface = tmp_path / "README.md"
        surface.write_text(GOOD_README, encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "release", "check", "--strict",
                "--paths", str(surface),
                "--db-dsn", FAKE_DSN,
                "--now", "2026-09-06",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _json_payload(result.output)
        assert payload["findings"] == []

    def test_missing_db_fails_closed_and_never_scans(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        calls = _patch_registry(monkeypatch)
        surface = tmp_path / "README.md"
        surface.write_text(BAD_README, encoding="utf-8")
        result = runner.invoke(app, ["release", "check", "--paths", str(surface)])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "ZHIWEI_DATABASE_URL" in result.output
        assert calls == [], "registry load must not be reached without a DSN"

    def test_missing_surface_path_fails_closed(self, tmp_path: Path, monkeypatch: Any) -> None:
        _patch_registry(monkeypatch)
        result = runner.invoke(
            app,
            [
                "release", "check",
                "--paths", str(tmp_path / "absent.md"),
                "--db-dsn", FAKE_DSN,
            ],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "absent.md" in result.output


class TestAttest:
    def test_dry_run_covers_surface_and_never_mutates(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        Path("README.md").write_text(GOOD_README, encoding="utf-8")
        Path("docs").mkdir()
        Path("docs/CLAIMS.md").write_text("claims", encoding="utf-8")
        Path("artifacts").mkdir()
        Path("artifacts/report.json").write_text("{}", encoding="utf-8")

        def _snapshot() -> dict[str, bytes]:
            return {
                path.relative_to(tmp_path).as_posix(): path.read_bytes()
                for path in sorted(tmp_path.rglob("*"))
                if path.is_file()
            }

        before = _snapshot()
        result = runner.invoke(
            app,
            [
                "release", "attest", "--dry-run",
                "--commit", COMMIT,
                "--generated-at", GENERATED_AT,
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _json_payload(result.output)
        assert payload["signed"] is False
        assert "signature" not in payload
        assert payload["provenance"]["commit"] == COMMIT
        assert set(payload["content_digests"]) == {
            "README.md", "docs/CLAIMS.md", "artifacts/report.json",
        }
        assert _snapshot() == before, "dry-run must not create or change files"

    def test_dry_run_is_deterministic_for_fixed_inputs(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        Path("README.md").write_text(GOOD_README, encoding="utf-8")
        args = [
            "release", "attest", "--dry-run",
            "--commit", COMMIT,
            "--generated-at", GENERATED_AT,
        ]
        first = runner.invoke(app, args)
        second = runner.invoke(app, args)
        assert first.exit_code == 0, first.output
        assert first.output == second.output

    def test_sign_without_key_file_refuses(self) -> None:
        result = runner.invoke(app, ["release", "attest", "--sign", "--output", "out.json"])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "--key-file" in result.output

    def test_sign_with_missing_key_file_refuses(self) -> None:
        result = runner.invoke(
            app,
            [
                "release", "attest", "--sign",
                "--key-file", "absent-key.bin",
                "--output", "out.json",
            ],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output

    def test_sign_writes_verifiable_attestation(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from zhiwei.release.attestation import AttestationDraft, verify_attestation

        monkeypatch.chdir(tmp_path)
        Path("README.md").write_text(GOOD_README, encoding="utf-8")
        Path("key.bin").write_bytes(b"k" * 32)
        result = runner.invoke(
            app,
            [
                "release", "attest", "--sign",
                "--key-file", "key.bin",
                "--output", "attestation.json",
                "--commit", COMMIT,
                "--generated-at", GENERATED_AT,
            ],
        )
        assert result.exit_code == 0, result.output
        attestation = json.loads(Path("attestation.json").read_text(encoding="utf-8"))
        assert attestation["signature"]
        signed = AttestationDraft(
            provenance=attestation["provenance"],
            content_digests=attestation["content_digests"],
            signed=True,
            signature=attestation["signature"],
        )
        assert verify_attestation(signed, key=b"k" * 32) is None
