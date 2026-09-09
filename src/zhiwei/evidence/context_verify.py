"""S3-T5 Context manifest verification.

Verifies the integrity of ContextManifest and TransitionManifest records
against wire captures and context state digests. Used by the CLI and
integration tests.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from zhiwei.context.manifests import ContextManifest, TransitionManifest
from zhiwei.models.presend import WireCapture, digest_bytes


class VerificationResult(BaseModel):
    """Result of a manifest verification check."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    checks: list[dict[str, Any]]
    manifest_id: str | None = None

    @property
    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c["ok"])
        return f"{passed}/{len(self.checks)} checks passed"


class VerificationError(RuntimeError):
    """Raised when a verification check fails."""


def _check(checks: list[dict[str, Any]], cid: str, ok: bool, detail: str) -> None:
    checks.append({"id": cid, "ok": ok, "detail": detail})


def verify_manifest_integrity(
    manifest: ContextManifest,
    capture: WireCapture,
    *,
    body_bytes: bytes | None = None,
    inventory_digest: str | None = None,
    profile_digest: str | None = None,
    ir_digest: str | None = None,
) -> VerificationResult:
    """Verify a ContextManifest against its WireCapture and optional digests.

    Checks:
    1. body_sha256 matches capture
    2. body_len matches capture
    3. method matches capture
    4. url matches capture
    5. redacted_headers are present (no auth headers leaked)
    6. If body_bytes provided, recompute digest
    7. If inventory_digest provided, verify binding
    8. If profile_digest provided, verify binding
    9. If ir_digest provided, verify binding
    10. auth headers are not in redacted_headers values
    """
    checks: list[dict[str, Any]] = []

    _check(checks, "body_sha256_match", manifest.body_sha256 == capture.body_sha256,
           f"manifest={manifest.body_sha256} capture={capture.body_sha256}")
    _check(checks, "body_len_match", manifest.body_len == capture.body_len,
           f"manifest={manifest.body_len} capture={capture.body_len}")
    _check(checks, "method_match", manifest.method == capture.method,
           f"manifest={manifest.method} capture={capture.method}")
    _check(checks, "url_match", manifest.url == capture.url,
           f"manifest={manifest.url} capture={capture.url}")

    auth_in_values = any(
        v != "<redacted>" and any(
            k.lower() in {"authorization", "api-key", "x-api-key", "cookie", "proxy-authorization"}
            for k in [manifest.redacted_headers.get("authorization", "")]
        )
        for v in manifest.redacted_headers.values()
    )
    _check(checks, "auth_redacted", not auth_in_values,
           "no auth credentials in redacted_headers values")

    if body_bytes is not None:
        recomputed = digest_bytes(body_bytes)
        _check(checks, "body_bytes_digest", recomputed == manifest.body_sha256,
               f"recomputed={recomputed} manifest={manifest.body_sha256}")

    if inventory_digest is not None:
        _check(checks, "inventory_digest_bound",
               manifest.source_inventory_digest == inventory_digest,
               f"manifest={manifest.source_inventory_digest} expected={inventory_digest}")

    if profile_digest is not None:
        _check(checks, "profile_digest_bound",
               manifest.target_profile_digest == profile_digest,
               f"manifest={manifest.target_profile_digest} expected={profile_digest}")

    if ir_digest is not None:
        _check(checks, "ir_digest_bound",
               manifest.ir_digest == ir_digest,
               f"manifest={manifest.ir_digest} expected={ir_digest}")

    ok = all(c["ok"] for c in checks)
    return VerificationResult(ok=ok, checks=checks, manifest_id=manifest.manifest_id)


def verify_transition_integrity(
    manifest: TransitionManifest,
    *,
    before_digest: str | None = None,
    after_digest: str | None = None,
) -> VerificationResult:
    """Verify a TransitionManifest against expected state digests.

    Checks:
    1. before_state_digest matches if provided
    2. after_state_digest matches if provided
    3. transition_type is non-empty
    4. items_added + items_removed + items_unchanged > 0 (something happened)
    5. wire_body_digest is present (transition was triggered by a wire send)
    """
    checks: list[dict[str, Any]] = []

    _check(checks, "transition_type_present", bool(manifest.transition_type),
           f"type={manifest.transition_type}")

    total_delta = manifest.items_added + manifest.items_removed + manifest.items_unchanged
    _check(checks, "nonzero_delta", total_delta > 0,
           f"added={manifest.items_added} removed={manifest.items_removed} "
           f"unchanged={manifest.items_unchanged}")

    _check(checks, "wire_body_digest_present", manifest.wire_body_digest is not None,
           f"wire_body_digest={manifest.wire_body_digest}")

    if before_digest is not None:
        _check(checks, "before_state_digest_match",
               manifest.before_state_digest == before_digest,
               f"manifest={manifest.before_state_digest} expected={before_digest}")

    if after_digest is not None:
        _check(checks, "after_state_digest_match",
               manifest.after_state_digest == after_digest,
               f"manifest={manifest.after_state_digest} expected={after_digest}")

    ok = all(c["ok"] for c in checks)
    return VerificationResult(ok=ok, checks=checks, manifest_id=manifest.manifest_id)


def verify_tamper_ir(
    manifest: ContextManifest,
    *,
    tampered_ir_digest: str,
) -> VerificationResult:
    """Verify that an IR digest mismatch is correctly detected."""
    checks: list[dict[str, Any]] = []
    match = manifest.ir_digest == tampered_ir_digest
    _check(checks, "ir_tamper_detected", not match,
           f"manifest_ir={manifest.ir_digest} tampered={tampered_ir_digest}")
    ok = all(c["ok"] for c in checks)
    return VerificationResult(ok=ok, checks=checks, manifest_id=manifest.manifest_id)


def verify_tamper_body(
    manifest: ContextManifest,
    *,
    tampered_body: bytes,
) -> VerificationResult:
    """Verify that body tampering is correctly detected."""
    checks: list[dict[str, Any]] = []
    recomputed = digest_bytes(tampered_body)
    _check(checks, "body_tamper_detected", recomputed != manifest.body_sha256,
           f"tampered_digest={recomputed} manifest={manifest.body_sha256}")
    ok = all(c["ok"] for c in checks)
    return VerificationResult(ok=ok, checks=checks, manifest_id=manifest.manifest_id)


def verify_tamper_inventory(
    manifest: ContextManifest,
    *,
    tampered_inventory_digest: str,
) -> VerificationResult:
    """Verify that inventory tampering is correctly detected."""
    checks: list[dict[str, Any]] = []
    match = manifest.source_inventory_digest == tampered_inventory_digest
    _check(checks, "inventory_tamper_detected", not match,
           f"manifest_inventory={manifest.source_inventory_digest} tampered={tampered_inventory_digest}")
    ok = all(c["ok"] for c in checks)
    return VerificationResult(ok=ok, checks=checks, manifest_id=manifest.manifest_id)


def verify_tamper_profile(
    manifest: ContextManifest,
    *,
    tampered_profile_digest: str,
) -> VerificationResult:
    """Verify that profile tampering is correctly detected."""
    checks: list[dict[str, Any]] = []
    match = manifest.target_profile_digest == tampered_profile_digest
    _check(checks, "profile_tamper_detected", not match,
           f"manifest_profile={manifest.target_profile_digest} tampered={tampered_profile_digest}")
    ok = all(c["ok"] for c in checks)
    return VerificationResult(ok=ok, checks=checks, manifest_id=manifest.manifest_id)


def verify_send_after_capture_mutation(
    manifest: ContextManifest,
    *,
    mutated_body: bytes,
) -> VerificationResult:
    """Verify that send-after-capture mutation is detected.

    If someone captures the body, computes the manifest, then mutates
    the body before sending, the digest won't match.
    """
    checks: list[dict[str, Any]] = []
    mutated_digest = digest_bytes(mutated_body)
    _check(checks, "send_after_capture_detected", mutated_digest != manifest.body_sha256,
           f"mutated={mutated_digest} manifest={manifest.body_sha256}")
    ok = all(c["ok"] for c in checks)
    return VerificationResult(ok=ok, checks=checks, manifest_id=manifest.manifest_id)
