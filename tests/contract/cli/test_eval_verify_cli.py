"""S9-T5：`eval verify --all-sealed` 与 `eval report` CLI 契约。

verify 的退出语义在此冻结为非 frozen 契约：failures 非空 → exit 1；空密封集合
是 vacuous 成功（checked=0 如实出现在 JSON 里）；corrupt/unreadable 密封件计入
failures 而非跳过。全链路 DB 复核由 integration 层覆盖，这里通过
`_verify_all_sealed_flow` seam 驱动退出语义（CliRunner + monkeypatch 既有模式）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretStr
from typer.testing import CliRunner

import zhiwei.cli.evals as evals_cli
from zhiwei.cli.main import app

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"


def _json_payload(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON payload in output: {output!r}")


def _patch_runtime(monkeypatch: Any, flow: Any) -> list[tuple[str, Path]]:
    """替换 settings 与 verify flow：驱动退出语义，不触碰真实 DB/ObjectStore。"""
    from zhiwei.config.settings import Settings

    calls: list[tuple[str, Path]] = []

    def _fake_settings() -> Settings:
        return Settings(
            database_url=SecretStr("postgresql://maintenance@127.0.0.1:5/zhiwei"),
            object_store_root=Path("/tmp/zhiwei-test-objects"),
        )

    async def _fake_flow(database_url: str, object_root: Path) -> dict[str, Any]:
        calls.append((database_url, object_root))
        return await flow(database_url, object_root)

    monkeypatch.setattr(evals_cli, "_load_settings", _fake_settings)
    monkeypatch.setattr(evals_cli, "_verify_all_sealed_flow", _fake_flow)
    return calls


class TestVerify:
    def test_verify_command_registered_with_all_sealed(self) -> None:
        result = runner.invoke(app, ["eval", "verify", "--help"])
        assert result.exit_code == 0, result.output
        assert "--all-sealed" in result.output

    def test_verify_requires_explicit_all_sealed(self) -> None:
        result = runner.invoke(app, ["eval", "verify"])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "--all-sealed" in result.output

    def test_empty_seal_set_is_vacuous_success(self, monkeypatch: Any) -> None:
        async def _flow(database_url: str, object_root: Path) -> dict[str, Any]:
            return {"checked": 0, "verified": 0, "failures": []}

        _patch_runtime(monkeypatch, _flow)
        result = runner.invoke(app, ["eval", "verify", "--all-sealed"])
        assert result.exit_code == 0, result.output
        payload = _json_payload(result.output)
        assert payload == {"checked": 0, "verified": 0, "failures": []}

    def test_corrupt_seal_counts_as_failure(self, monkeypatch: Any) -> None:
        eval_run_id = str(uuid4())

        async def _flow(database_url: str, object_root: Path) -> dict[str, Any]:
            return {
                "checked": 1,
                "verified": 0,
                "failures": [
                    {"eval_run_id": eval_run_id, "reason": "seal digest mismatch"}
                ],
            }

        _patch_runtime(monkeypatch, _flow)
        result = runner.invoke(app, ["eval", "verify", "--all-sealed"])
        assert result.exit_code == 1, result.output
        assert TRACEBACK_MARKER not in result.output
        payload = _json_payload(result.output)
        assert payload["checked"] == 1
        assert payload["verified"] == 0
        assert payload["failures"] == [
            {"eval_run_id": eval_run_id, "reason": "seal digest mismatch"}
        ]

    def test_runtime_failure_fails_closed(self, monkeypatch: Any) -> None:
        async def _flow(database_url: str, object_root: Path) -> dict[str, Any]:
            raise RuntimeError("connection refused")

        _patch_runtime(monkeypatch, _flow)
        result = runner.invoke(app, ["eval", "verify", "--all-sealed"])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "connection refused" in result.output


class TestReport:
    def test_report_command_registered(self) -> None:
        result = runner.invoke(app, ["eval", "report", "--help"])
        assert result.exit_code == 0, result.output
        # eval_run_id 是 Argument，help 面板里不带 -- 前缀
        for option in (
            "eval_run_id", "--organization-id", "--workspace-id",
            "--seal", "--model", "--version", "--date", "--corpus", "--environment",
        ):
            assert option in result.output

    def test_report_requires_explicit_scope_labels(self) -> None:
        result = runner.invoke(
            app,
            [
                "eval", "report", str(uuid4()),
                "--organization-id", str(uuid4()),
                "--workspace-id", str(uuid4()),
            ],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
