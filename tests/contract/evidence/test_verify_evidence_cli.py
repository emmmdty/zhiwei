"""S6 CONTRACT: `zhiwei verify evidence` CLI 与冻结 fixture bundle。

事实源：specs/s6-evidence-ask.md §3（分层验证与稳定退出码 0/2/3/4/5/6/7）、§7（Gate 命令）。

契约面：
- `verify` app 下必须有 `evidence` 子命令，接受 bundle 文件或目录；
- valid.bundle 必须 exit 0，tampered.bundle 必须 exit 6（digest/artifact 层）；
- fixture 由真实 EvidenceBundle schema 生成（canonical 重建逐字节一致），
  tampered 与 valid 的唯一差异是 copy_frozen 的 result_copy_digest；
- 载入期类型化异常 → 稳定退出码映射（claim level → 5、copy_frozen binding → 4、
  schema 类 → 2）；未知异常 fail closed 落保留码 70（不得伪装成对 bundle 的判定）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from typer.testing import CliRunner, Result

from zhiwei.cli.main import app
from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.canonical_values import (
    CanonicalValue,
    CopyFrozenMetadata,
    ReproducibilityLevel,
)
from zhiwei.evidence.claims import ClaimStatus, CodeSpan, FactClaim, InferenceClaim, QuoteClaim
from zhiwei.evidence.refs import CellRef, DocRef, QueryReplayRef

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "evidence"
VALID_BUNDLE = FIXTURE_DIR / "valid.bundle"
TAMPERED_BUNDLE = FIXTURE_DIR / "tampered.bundle"

_NS = uuid5(NAMESPACE_URL, "zhiwei:fixtures:evidence:v1")
_FROZEN_AT = datetime(2026, 9, 4, tzinfo=UTC)


def _uid(name: str):  # type: ignore[no-untyped-def]
    return uuid5(_NS, name)


def _snapshot_digest() -> str:
    from zhiwei.contracts.canonical import canonical_json, digest_bytes

    return digest_bytes(canonical_json({"snapshot": "evals/novels/shuihu@frozen-2026-09-04"}))


def _cell_copy_frozen(*, result_copy_digest: str) -> CopyFrozenMetadata:
    return CopyFrozenMetadata(
        sql="SELECT merit_count FROM liangshan WHERE rank = ?",
        typed_params={"rank": 7},
        schema_snapshot_digest=_snapshot_digest(),
        executed_at=_FROZEN_AT,
        result_copy_digest=result_copy_digest,
        row_count=1,
    )


def build_reference_bundle(*, result_copy_digest: str | None = None) -> EvidenceBundle:
    """canonical fixture bundle 的唯一构造源（generator 与测试共用）。"""

    from zhiwei.contracts.canonical import digest as _digest

    digest = result_copy_digest or "sha256:" + "3f" * 32
    replay_ref = QueryReplayRef(
        ref_id=_uid("ref-query-replay"),
        reproducibility_level=ReproducibilityLevel.REPLAYABLE,
        source_id=_uid("source-liangshan"),
        snapshot_digest=_snapshot_digest(),
        created_at=_FROZEN_AT,
        sql="SELECT name FROM liangshan WHERE rank = ?",
        params={"positional": [7]},
    )
    cell_ref = CellRef(
        ref_id=_uid("ref-cell-copy-frozen"),
        reproducibility_level=ReproducibilityLevel.COPY_FROZEN,
        source_id=_uid("source-liangshan"),
        created_at=_FROZEN_AT,
        table="liangshan",
        column="merit_count",
        row_locator={"rank": 7},
        copy_frozen=_cell_copy_frozen(result_copy_digest=digest),
    )
    doc_ref = DocRef(
        ref_id=_uid("ref-doc-reference-only"),
        reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY,
        source_id=_uid("source-zhaoan"),
        created_at=_FROZEN_AT,
        document_uri="docs/zhaoan.md",
        section_path="武松",
    )
    answer_digest = _digest({"answer": "liangshan-rank-7"})
    fact_claim = FactClaim(
        claim_id=_uid("claim-fact"),
        answer_id=_uid("answer"),
        status=ClaimStatus.FINAL,
        evidence_refs=(replay_ref, cell_ref),
        answer_digest=answer_digest,
        canonical_value=CanonicalValue.model_validate({"type": "int", "value": 45}),
        created_at=_FROZEN_AT,
        updated_at=_FROZEN_AT,
    )
    quote_claim = QuoteClaim(
        claim_id=_uid("claim-quote"),
        answer_id=_uid("answer"),
        status=ClaimStatus.FINAL,
        evidence_refs=(replay_ref,),
        answer_digest=answer_digest,
        canonical_value=CanonicalValue.model_validate({"type": "text", "value": "霹雳火"}),
        quote_text="天罡星霹雳火秦明",
        code_span=CodeSpan(file_path="docs/zhaoan.md", line_start=3, line_end=3),
        created_at=_FROZEN_AT,
        updated_at=_FROZEN_AT,
    )
    inference_claim = InferenceClaim(
        claim_id=_uid("claim-inference"),
        answer_id=_uid("answer"),
        supporting_inputs=(doc_ref,),
        contradicting_inputs=(),
        created_at=_FROZEN_AT,
        updated_at=_FROZEN_AT,
    )
    return EvidenceBundle(
        bundle_id=_uid("bundle"),
        answer_id=_uid("answer"),
        evidence_refs=(replay_ref, cell_ref, doc_ref),
        claims=(fact_claim, quote_claim, inference_claim),
        created_at=_FROZEN_AT,
        schema_version=1,
        metadata={
            "producer": "zhiwei-s6-fixture",
            "result_copy_digests": {
                str(cell_ref.ref_id): _cell_copy_frozen(
                    result_copy_digest="sha256:" + "3f" * 32
                ).result_copy_digest
            },
        },
    )


def _dump_bundle(bundle: EvidenceBundle) -> str:
    return json.dumps(
        bundle.model_dump(mode="json"), ensure_ascii=False, indent=2
    ) + "\n"


def _deep_diff(a: object, b: object, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        diffs: list[str] = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                diffs.append(f"{path}.{key}")
            else:
                diffs.extend(_deep_diff(a[key], b[key], f"{path}.{key}"))
        return diffs
    if isinstance(a, list) and isinstance(b, list):
        diffs = []
        for index in range(max(len(a), len(b))):
            if index >= len(a) or index >= len(b):
                diffs.append(f"{path}[{index}]")
            else:
                diffs.extend(_deep_diff(a[index], b[index], f"{path}[{index}]"))
        return diffs
    return [] if a == b else [path or "$"]


# --------------------------------------------------------------------------- fixtures


class TestFrozenFixtures:
    def test_fixture_files_exist(self) -> None:
        assert VALID_BUNDLE.is_file()
        assert TAMPERED_BUNDLE.is_file()

    def test_valid_fixture_parses_as_real_bundle(self) -> None:
        bundle = EvidenceBundle.model_validate_json(VALID_BUNDLE.read_bytes())
        levels = {ref.reproducibility_level for ref in bundle.evidence_refs}
        assert ReproducibilityLevel.REPLAYABLE in levels
        assert ReproducibilityLevel.COPY_FROZEN in levels
        kinds = {type(claim).__name__ for claim in bundle.claims}
        assert kinds == {"FactClaim", "QuoteClaim", "InferenceClaim"}

    def test_fixtures_are_byte_identical_to_canonical_rebuild(self) -> None:
        """fixture 必须由真实 schema 的唯一构造源生成——手写假 JSON 在此即失败。"""
        assert VALID_BUNDLE.read_text(encoding="utf-8") == _dump_bundle(
            build_reference_bundle()
        )
        tampered_digest = "sha256:" + "d1" * 32
        assert TAMPERED_BUNDLE.read_text(encoding="utf-8") == _dump_bundle(
            build_reference_bundle(result_copy_digest=tampered_digest)
        )

    def test_tampered_differs_only_in_result_copy_digest(self) -> None:
        valid = json.loads(VALID_BUNDLE.read_text(encoding="utf-8"))
        tampered = json.loads(TAMPERED_BUNDLE.read_text(encoding="utf-8"))
        # 单一逻辑篡改（copy_frozen 的 result_copy_digest）：schema 中 ref 同时
        # 内嵌于 bundle refs 与 claim refs，因此出现在两条路径上。
        assert _deep_diff(valid, tampered) == [
            ".claims[0].evidence_refs[1].copy_frozen.result_copy_digest",
            ".evidence_refs[1].copy_frozen.result_copy_digest",
        ]

    def test_tampered_fixture_still_parses(self) -> None:
        """篡改的是 digest 值本身，不是 schema——bundle 必须仍可解析，
        由 verifier 在 digest/artifact 层拒绝，而不是在 schema 层拒绝。"""
        bundle = EvidenceBundle.model_validate_json(TAMPERED_BUNDLE.read_bytes())
        assert bundle.schema_version == 1


# --------------------------------------------------------------------------- CLI contract


def _invoke(path: Path, fmt: str = "json") -> Result:
    return runner.invoke(
        app, ["verify", "evidence", str(path), "--format", fmt]
    )


class TestVerifyEvidenceCli:
    def test_verify_help_lists_evidence(self) -> None:
        result = runner.invoke(app, ["verify", "--help"])
        assert result.exit_code == 0
        assert "evidence" in result.output

    def test_evidence_help_documents_format_option(self) -> None:
        result = runner.invoke(app, ["verify", "evidence", "--help"])
        assert result.exit_code == 0
        assert "--format" in result.output

    def test_valid_bundle_exits_zero(self) -> None:
        result = _invoke(VALID_BUNDLE)
        assert result.exit_code == 0, result.output
        assert TRACEBACK_MARKER not in result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["exit_code"] == 0
        assert payload["results"][0]["ok"] is True
        check_ids = {c["check_id"] for c in payload["results"][0]["checks"]}
        assert "bundle_schema_version" in check_ids
        assert "bundle_claim_refs_exist" in check_ids

    def test_tampered_bundle_exits_six(self) -> None:
        """specs/s6 §7 Gate：`verify evidence tampered.bundle` 必须以退出码 6 失败。"""
        result = _invoke(TAMPERED_BUNDLE)
        assert result.exit_code != 0
        assert result.exit_code == 6
        assert TRACEBACK_MARKER not in result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["exit_code"] == 6
        failing = [
            c
            for c in payload["results"][0]["checks"]
            if not c["ok"]
        ]
        assert failing, "tampered bundle 必须至少有一个失败 check"
        assert all(c["exit_code"] == 6 for c in failing)
        assert any("copy_frozen_digest_match" in c["check_id"] for c in failing)

    def test_text_format_lists_checks(self) -> None:
        result = _invoke(VALID_BUNDLE, fmt="text")
        assert result.exit_code == 0
        assert "PASS" in result.output
        assert "bundle_schema_version" in result.output

    def test_missing_file_exits_two(self) -> None:
        result = _invoke(FIXTURE_DIR / "does-not-exist.bundle")
        assert result.exit_code == 2
        assert TRACEBACK_MARKER not in result.output

    def test_invalid_json_exits_two(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.bundle"
        target.write_text("not json at all", encoding="utf-8")
        result = _invoke(target)
        assert result.exit_code == 2
        assert TRACEBACK_MARKER not in result.output

    def test_schema_invalid_bundle_exits_two(self, tmp_path: Path) -> None:
        target = tmp_path / "schema-invalid.bundle"
        target.write_text('{"bundle_id": "not-a-uuid"}', encoding="utf-8")
        result = _invoke(target)
        assert result.exit_code == 2
        assert TRACEBACK_MARKER not in result.output

    def test_directory_mode_verifies_every_bundle(self, tmp_path: Path) -> None:
        target = tmp_path / "bundles"
        target.mkdir()
        (target / "a.bundle").write_text(
            VALID_BUNDLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = _invoke(target)
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert len(payload["results"]) == 1

    def test_directory_mode_worst_exit_code_wins(self, tmp_path: Path) -> None:
        target = tmp_path / "bundles"
        target.mkdir()
        (target / "a.bundle").write_text(
            VALID_BUNDLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (target / "b.bundle").write_text(
            TAMPERED_BUNDLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = _invoke(target)
        assert result.exit_code == 6
        payload = json.loads(result.stdout)
        by_name = {Path(r["path"]).name: r for r in payload["results"]}
        assert by_name["a.bundle"]["ok"] is True
        assert by_name["b.bundle"]["ok"] is False

    def test_directory_without_bundles_exits_two(self, tmp_path: Path) -> None:
        target = tmp_path / "empty"
        target.mkdir()
        result = _invoke(target)
        assert result.exit_code == 2

    def test_unexpected_failure_falls_back_to_reserved_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """未知异常 fail closed：不得伪装成 0/2-7 的任何语义判定。"""
        import zhiwei.cli.evidence as evidence_cli

        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("internal verifier explosion")

        monkeypatch.setattr(evidence_cli, "verify_bundle", _boom)
        result = _invoke(VALID_BUNDLE)
        assert result.exit_code == 70
        assert TRACEBACK_MARKER not in result.output


class TestVerifyHandlerLayeredExitCodes:
    """Runtime Verify handler 必须与 §3 稳定退出码分层一致（spec §6）。"""

    def _handler_output(self, bundle_dict: dict) -> dict:  # type: ignore[type-arg]
        from uuid import uuid4

        from zhiwei.runtime.handlers.base import TaskInput
        from zhiwei.runtime.handlers.verify import VerifyHandler

        output = VerifyHandler().execute(
            TaskInput(
                task_id="t",
                attempt_id=uuid4(),
                input_values={"bundle": bundle_dict},
            )
        )
        return output.output_values

    def test_reference_only_fact_claim_maps_to_claim_span(self) -> None:
        bundle = build_reference_bundle()
        raw = bundle.model_dump(mode="json")
        # wire 层把 reference_only 的 DocRef 挂到 Fact claim 上：模型层不可构造、
        # 只能经 wire 注入，VerifyHandler 必须在 claim/span 层（5）拒绝。
        raw["claims"][0]["evidence_refs"].append(raw["evidence_refs"][2])
        values = self._handler_output(raw)
        assert values["verification_ok"] is False
        assert values["exit_code"] == 5

    def test_missing_copy_frozen_binding_maps_to_replay_value(self) -> None:
        bundle = build_reference_bundle()
        raw = bundle.model_dump(mode="json")
        del raw["evidence_refs"][1]["copy_frozen"]
        values = self._handler_output(raw)
        assert values["verification_ok"] is False
        assert values["exit_code"] == 4
