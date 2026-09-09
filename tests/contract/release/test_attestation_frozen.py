"""S9 冻结契约：出处 attestation 与签名分离（A 档，S9-T5）。

attestation draft 分离 provenance（repo/commit/工具版本）与 content digest；
dry-run 只构建、永不签名/发布；签名后内容篡改必须可检出。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhiwei.release.attestation import (
    AttestationDraft,
    AttestationVerificationError,
    build_attestation_draft,
    sign_attestation,
    verify_attestation,
)


def _materialize(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "CLAIMS.md").write_text("claims", encoding="utf-8")
    (root / "README.md").write_text("readme", encoding="utf-8")


def _draft(root: Path) -> AttestationDraft:
    return build_attestation_draft(
        repo_root=root,
        include_globs=("README.md", "docs/*.md"),
        commit="0123456789abcdef" * 2,
        generated_at="2026-09-05T00:00:00Z",
        generator="zhiwei-release-check",
    )


class TestDraftSemantics:
    def test_deterministic_for_fixed_inputs(self, tmp_path: Path) -> None:
        _materialize(tmp_path)
        assert _draft(tmp_path) == _draft(tmp_path)

    def test_provenance_separate_from_content(self, tmp_path: Path) -> None:
        _materialize(tmp_path)
        draft = _draft(tmp_path)
        assert "commit" in draft.provenance
        assert "generated_at" in draft.provenance
        assert set(draft.content_digests) == {"README.md", "docs/CLAIMS.md"}
        # provenance 与 content 不得混在同一命名空间。
        assert not (set(draft.provenance) & set(draft.content_digests))

    def test_dry_run_is_unsigned(self, tmp_path: Path) -> None:
        _materialize(tmp_path)
        draft = _draft(tmp_path)
        assert draft.signed is False
        # dry-run 产物不包含签名字段，也不应包含任何私钥材料。
        assert "signature" not in draft.canonical_mapping()

    def test_draft_does_not_write_files(self, tmp_path: Path) -> None:
        _materialize(tmp_path)
        before = sorted(p.name for p in tmp_path.rglob("*"))
        _draft(tmp_path)
        after = sorted(p.name for p in tmp_path.rglob("*"))
        assert before == after


class TestSigning:
    def test_sign_and_verify(self, tmp_path: Path) -> None:
        _materialize(tmp_path)
        signed = sign_attestation(_draft(tmp_path), key=b"k" * 32)
        assert signed.signed is True
        assert verify_attestation(signed, key=b"k" * 32) is None

    def test_wrong_key_fails(self, tmp_path: Path) -> None:
        _materialize(tmp_path)
        signed = sign_attestation(_draft(tmp_path), key=b"k" * 32)
        with pytest.raises(AttestationVerificationError):
            verify_attestation(signed, key=b"j" * 32)

    def test_content_tamper_detected(self, tmp_path: Path) -> None:
        _materialize(tmp_path)
        signed = sign_attestation(_draft(tmp_path), key=b"k" * 32)
        tampered = signed.model_copy(
            update={"content_digests": {"README.md": "sha256:" + "0" * 64}}
        )
        with pytest.raises(AttestationVerificationError):
            verify_attestation(tampered, key=b"k" * 32)

    def test_draft_cannot_be_signed_twice(self, tmp_path: Path) -> None:
        _materialize(tmp_path)
        signed = sign_attestation(_draft(tmp_path), key=b"k" * 32)
        with pytest.raises(AttestationVerificationError):
            sign_attestation(signed, key=b"k" * 32)
