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
from uuid import UUID

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

    def test_missing_surface_path_reported_not_fatal(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        # R2-A（T3）：required-if-exist 语义——缺失表面不构成 fatal 错误，但绝不
        # 静默：per-path 条目（missing=true, checked=0）必须出现在 JSON 输出里；
        # exit code 只由 findings 决定。
        _patch_registry(monkeypatch)
        result = runner.invoke(
            app,
            [
                "release", "check",
                "--paths", str(tmp_path / "absent.md"),
                "--db-dsn", FAKE_DSN,
            ],
        )
        assert result.exit_code == 0, result.output
        assert TRACEBACK_MARKER not in result.output
        payload = _json_payload(result.output)
        assert payload["surface"] == [
            {"path": str(tmp_path / "absent.md"), "checked": 0, "missing": True}
        ]
        assert payload["findings"] == []

    def test_default_surface_includes_demo(self, tmp_path: Path, monkeypatch: Any) -> None:
        # R2-A（T3）：默认表面在 docs 之后追加 demo；三个面都存在时逐面可见。
        _patch_registry(monkeypatch)
        monkeypatch.chdir(tmp_path)
        Path("README.md").write_text(GOOD_README, encoding="utf-8")
        Path("docs").mkdir()
        Path("docs/CLAIMS.md").write_text("claims\n", encoding="utf-8")
        Path("demo").mkdir()
        Path("demo/README.md").write_text("demo\n", encoding="utf-8")
        Path("demo/nested.md").write_text("nested\n", encoding="utf-8")
        result = runner.invoke(
            app,
            ["release", "check", "--db-dsn", FAKE_DSN, "--now", "2026-09-06"],
        )
        assert result.exit_code == 0, result.output
        payload = _json_payload(result.output)
        assert [(entry["path"], entry["checked"], entry["missing"]) for entry in payload["surface"]] == [
            ("README.md", 1, False),
            ("docs", 1, False),
            ("demo", 2, False),
        ]

    def test_missing_demo_in_default_surface_is_visible_not_fatal(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_registry(monkeypatch)
        monkeypatch.chdir(tmp_path)
        Path("README.md").write_text(GOOD_README, encoding="utf-8")
        result = runner.invoke(
            app,
            ["release", "check", "--strict", "--db-dsn", FAKE_DSN, "--now", "2026-09-06"],
        )
        assert result.exit_code == 0, result.output
        payload = _json_payload(result.output)
        assert [(entry["path"], entry["missing"]) for entry in payload["surface"]] == [
            ("README.md", False),
            ("docs", True),
            ("demo", True),
        ]
        assert payload["findings"] == []


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


# ---------------------------------------------------------------------------
# S11 followup-2 任务一：`release render`——声明块 {{claim:}} marker 渲染。
#
# 契约（docs/handoffs/s11-followup-2-publish-demo.md §1）：
# - 只允许 registry-verified 的值经 render_release_surface 填充（fail closed：
#   未知 id / 非 verified / 绑定值缺失一律拒绝且不产出任何输出）；
# - 口径标注照抄 registry scope 的护栏：块内引用 claim 的 scope 必须一致，且
#   块内每个 ISO 日期都必须等于 scope date——README 注释与 registry 漂移时拒绝；
# - 源文件永不回写（repo README 保留 marker 供 checker 警察）；输出走 stdout
#   或 --output，同输入同输出（逐字节确定）；
# - registry 读取与 check 同一 maintenance-DSN seam，DSN 缺失 fail closed。
# ---------------------------------------------------------------------------

RENDER_README = (
    "# Demo\n"
    "\n"
    "<!-- claims:start -->\n"
    "<!-- 口径：mode=offline · model=reference-fixture · environment=offline-fixture ·\n"
    "     口径日期 2026-09-06。全部为离线确定性执行，不是 live 模型效果，也不是平台总证据。 -->\n"
    "\n"
    "| 声明 | 绑定值 |\n"
    "| --- | --- |\n"
    "| 语料内回归 | {{claim:factqa-v1.accuracy}} |\n"
    "<!-- claims:end -->\n"
)


def _verified_claim(claim_id: str, *, date: str = "2026-09-06") -> Any:
    from zhiwei.agents.claims import ClaimEvidence, ClaimRecord, ClaimScope, ClaimStatus

    return ClaimRecord(
        claim_id=claim_id,
        statement="FactQA accuracy {{value}}",
        scope=ClaimScope(
            mode="offline",
            model="reference-fixture",
            version="0027",
            date=date,
            corpus="factqa-v1",
            environment="offline-fixture",
        ),
        status=ClaimStatus.OFFLINE_VERIFIED,
        evidence=ClaimEvidence(
            eval_run_id=UUID("00000000-0000-4000-8000-000000000001"),
            seal_digest="sha256:" + "a" * 64,
            artifact_manifest_id=UUID("00000000-0000-4000-8000-000000000002"),
            mode="offline",
        ),
        bound_value="FactQA accuracy 0.95",
    )


class TestRender:
    def test_release_group_registers_render(self) -> None:
        result = runner.invoke(app, ["release", "--help"])
        assert result.exit_code == 0, result.output
        assert "render" in result.output

    def test_render_replaces_verified_marker_with_bound_value(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_registry(monkeypatch, {"factqa-v1.accuracy": _verified_claim("factqa-v1.accuracy")})
        surface = tmp_path / "README.md"
        surface.write_text(RENDER_README, encoding="utf-8")
        out = tmp_path / "rendered.md"
        args = [
            "release", "render",
            "--paths", str(surface),
            "--output", str(out),
            "--db-dsn", FAKE_DSN,
        ]
        first = runner.invoke(app, args)
        assert first.exit_code == 0, first.output
        rendered = out.read_text(encoding="utf-8")
        assert "{{claim:" not in rendered
        assert "FactQA accuracy 0.95" in rendered
        # 源文件永不回写：repo README 保留 marker（checker 的事实源）
        assert surface.read_text(encoding="utf-8") == RENDER_README
        # 确定性：同输入同输出
        out.unlink()
        second = runner.invoke(app, args)
        assert second.exit_code == 0, second.output
        assert out.read_text(encoding="utf-8") == rendered

    def test_render_stdout_default_without_output_option(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_registry(monkeypatch, {"factqa-v1.accuracy": _verified_claim("factqa-v1.accuracy")})
        surface = tmp_path / "README.md"
        surface.write_text(RENDER_README, encoding="utf-8")
        result = runner.invoke(
            app,
            ["release", "render", "--paths", str(surface), "--db-dsn", FAKE_DSN],
        )
        assert result.exit_code == 0, result.output
        assert "FactQA accuracy 0.95" in result.output

    def test_render_refuses_unknown_claim_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_registry(monkeypatch)
        surface = tmp_path / "README.md"
        surface.write_text(RENDER_README, encoding="utf-8")
        out = tmp_path / "rendered.md"
        result = runner.invoke(
            app,
            [
                "release", "render",
                "--paths", str(surface),
                "--output", str(out),
                "--db-dsn", FAKE_DSN,
            ],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert not out.exists(), "拒绝路径上不得产出任何文件"

    def test_render_refuses_unverified_claim(self, tmp_path: Path, monkeypatch: Any) -> None:
        from zhiwei.agents.claims import ClaimRecord, ClaimScope, ClaimStatus

        planned = ClaimRecord(
            claim_id="factqa-v1.accuracy",
            statement="FactQA accuracy {{value}}",
            scope=ClaimScope(
                mode="offline",
                model="reference-fixture",
                version="0027",
                date="2026-09-06",
                corpus="factqa-v1",
                environment="offline-fixture",
            ),
            status=ClaimStatus.PLANNED,
        )
        _patch_registry(monkeypatch, {"factqa-v1.accuracy": planned})
        surface = tmp_path / "README.md"
        surface.write_text(RENDER_README, encoding="utf-8")
        result = runner.invoke(
            app,
            ["release", "render", "--paths", str(surface), "--db-dsn", FAKE_DSN],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output

    def test_render_requires_dsn_fail_closed(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        calls = _patch_registry(monkeypatch)
        surface = tmp_path / "README.md"
        surface.write_text(RENDER_README, encoding="utf-8")
        result = runner.invoke(app, ["release", "render", "--paths", str(surface)])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "ZHIWEI_DATABASE_URL" in result.output
        assert calls == [], "registry load must not be reached without a DSN"

    def test_render_refuses_annotation_date_drift(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        # 口径标注照抄 registry scope：README 注释日期 ≠ scope date → 拒绝渲染
        _patch_registry(monkeypatch, {"factqa-v1.accuracy": _verified_claim("factqa-v1.accuracy", date="2026-09-08")})
        surface = tmp_path / "README.md"
        surface.write_text(RENDER_README, encoding="utf-8")
        result = runner.invoke(
            app,
            ["release", "render", "--paths", str(surface), "--db-dsn", FAKE_DSN],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output

    def test_render_refuses_mixed_scope_dates(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        registry = {
            "factqa-v1.accuracy": _verified_claim("factqa-v1.accuracy"),
            "ask-v1.contract-pass": _verified_claim("ask-v1.contract-pass", date="2026-09-07"),
        }
        _patch_registry(monkeypatch, registry)
        surface = tmp_path / "README.md"
        second_row = "| Ask 契约 | {{claim:ask-v1.contract-pass}} |\n"
        surface.write_text(
            RENDER_README.replace("<!-- claims:end -->", second_row + "<!-- claims:end -->"),
            encoding="utf-8",
        )
        result = runner.invoke(
            app,
            ["release", "render", "--paths", str(surface), "--db-dsn", FAKE_DSN],
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output

    def test_render_without_claims_block_is_noop(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _patch_registry(monkeypatch)
        surface = tmp_path / "README.md"
        surface.write_text(GOOD_README, encoding="utf-8")
        result = runner.invoke(
            app,
            ["release", "render", "--paths", str(surface), "--db-dsn", FAKE_DSN],
        )
        assert result.exit_code == 0, result.output
        assert result.output == GOOD_README
