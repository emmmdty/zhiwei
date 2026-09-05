"""Release 表面出处 attestation：provenance 与 content digest 分离、签名可复算。

draft 分离 provenance（repo/commit/工具版本）与 content_digests（表面文件摘要）；
签名是对 canonical JSON 载荷（不含签名字段本身）的 HMAC-SHA256，密钥由 operator
以文件显式提供——dry-run 只构建、永不签名/发布。签名与验签共用同一 canonical
序列化入口，内容篡改必然破坏 HMAC 比对。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.hmac import HMAC
from pydantic import BaseModel, ConfigDict

from zhiwei.contracts.canonical import canonical_json, digest_bytes

__all__ = [
    "AttestationDraft",
    "AttestationVerificationError",
    "build_attestation_draft",
    "sign_attestation",
    "verify_attestation",
]

SIGNATURE_PREFIX = "hmac-sha256:"


class AttestationVerificationError(RuntimeError):
    """验签失败，或对已签名 draft 重复签名——两者都是不可恢复的协议违例。"""


class AttestationDraft(BaseModel):
    """出处 attestation 载荷；canonical_mapping 是唯一 canonical 序列化入口。"""

    model_config = ConfigDict(frozen=True)

    provenance: Mapping[str, str]
    content_digests: Mapping[str, str]
    signature: str | None = None
    signed: bool = False

    def canonical_mapping(self) -> dict[str, Any]:
        # 签名字段只在已签名时出现：unsigned draft 的 canonical 载荷不含任何
        # 签名/密钥材料，签名覆盖的正是 provenance + content_digests 两块。
        mapping: dict[str, Any] = {
            "provenance": dict(self.provenance),
            "content_digests": dict(self.content_digests),
        }
        if self.signed:
            mapping["signature"] = self.signature
        return mapping

    def _signing_payload(self) -> bytes:
        return canonical_json(
            {
                "provenance": dict(self.provenance),
                "content_digests": dict(self.content_digests),
            }
        )


def build_attestation_draft(
    repo_root: Path,
    include_globs: Sequence[str],
    commit: str,
    generated_at: str,
    generator: str,
) -> AttestationDraft:
    """对 repo_root 内给定 glob 覆盖的文件构建 unsigned draft；不做任何写操作。

    文件集合经 glob 收集后排序，同一固定输入产生逐字节相同的 draft。
    """
    content_digests: dict[str, str] = {}
    for pattern in include_globs:
        for path in sorted(repo_root.glob(pattern)):
            if not path.is_file():
                continue
            relative = path.relative_to(repo_root).as_posix()
            content_digests[relative] = _file_digest(path)
    return AttestationDraft(
        provenance={
            "commit": commit,
            "generated_at": generated_at,
            "generator": generator,
        },
        content_digests=content_digests,
    )


def sign_attestation(draft: AttestationDraft, key: bytes) -> AttestationDraft:
    """对 draft 计算 HMAC-SHA256 签名并返回 signed 副本；重复签名拒绝。"""
    if draft.signed or draft.signature is not None:
        raise AttestationVerificationError("attestation draft is already signed")
    mac = HMAC(key, hashes.SHA256())
    mac.update(draft._signing_payload())
    signature = SIGNATURE_PREFIX + mac.finalize().hex()
    return draft.model_copy(update={"signed": True, "signature": signature})


def verify_attestation(signed: AttestationDraft, key: bytes) -> None:
    """复算 HMAC 并比对；不一致/未签名/格式未知一律 AttestationVerificationError。"""
    if not signed.signed or signed.signature is None:
        raise AttestationVerificationError("attestation is not signed")
    if not signed.signature.startswith(SIGNATURE_PREFIX):
        raise AttestationVerificationError("unknown signature format")
    try:
        expected = bytes.fromhex(signed.signature.removeprefix(SIGNATURE_PREFIX))
    except ValueError as exc:
        raise AttestationVerificationError("signature is not valid hex") from exc
    mac = HMAC(key, hashes.SHA256())
    mac.update(signed._signing_payload())
    try:
        mac.verify(expected)
    except InvalidSignature as exc:
        raise AttestationVerificationError(
            "attestation signature does not match its content"
        ) from exc


def _file_digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())
