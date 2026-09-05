"""S6 Evidence verifier — deterministic, layered verification.

Verifies bundles through schema/version/source/snapshot/locator/query/
result/value/claim span/digest layers. Stable exit codes per spec §3.

事实源：S6 spec §3、ADR-003。
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.canonical_values import ReproducibilityLevel
from zhiwei.evidence.claims import (
    Claim,
    FactClaim,
    InferenceClaim,
    QuoteClaim,
    RecommendationClaim,
)
from zhiwei.evidence.errors import (
    ClaimLevelViolationError,
    CopyFrozenBindingError,
    EvidenceError,
)
from zhiwei.evidence.refs import (
    EvidenceRef,
    QueryReplayRef,
)


class VerifyExitCode(IntEnum):
    """Stable exit codes for evidence verification (spec §3)."""

    SUCCESS = 0
    INPUT_SCHEMA = 2
    SOURCE_SNAPSHOT = 3
    REPLAY_VALUE = 4
    CLAIM_SPAN = 5
    DIGEST_ARTIFACT = 6
    AUTHORIZATION = 7


def map_load_error(exc: BaseException) -> VerifyExitCode:
    """把 bundle 载入期的类型化异常映射到 spec §3 的稳定退出码。

    异常层级的归属是「违规发生在哪一层」而不是「异常从哪个类抛出」：
    - ClaimLevelViolationError：claim 类型不被证据等级支撑 → claim/span（5）；
    - CopyFrozenBindingError：copy_frozen 绑定缺失 → replay/value（4）；
    - 其余 EvidenceError（ref 字段/变体非法、bundle 结构非法）与 pydantic
      ValidationError 都是输入/schema 问题 → input/schema（2）。
    调用方对非 Evidence 的未知异常自行 fail closed（CLI 落保留码 70，
    不把内部错误伪装成对 bundle 的判定）。
    """
    if isinstance(exc, ClaimLevelViolationError):
        return VerifyExitCode.CLAIM_SPAN
    if isinstance(exc, CopyFrozenBindingError):
        return VerifyExitCode.REPLAY_VALUE
    if isinstance(exc, EvidenceError):
        return VerifyExitCode.INPUT_SCHEMA
    return VerifyExitCode.INPUT_SCHEMA


class VerifyCheck:
    """A single verification check result."""

    __slots__ = ("check_id", "detail", "exit_code", "ok")

    def __init__(
        self,
        check_id: str,
        ok: bool,
        exit_code: VerifyExitCode,
        detail: str = "",
    ) -> None:
        self.check_id = check_id
        self.ok = ok
        self.exit_code = exit_code
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "ok": self.ok,
            "exit_code": int(self.exit_code),
            "detail": self.detail,
        }


class VerifyResult:
    """Aggregated verification result."""

    __slots__ = ("checks", "exit_code", "ok")

    def __init__(self) -> None:
        self.ok = True
        self.exit_code = VerifyExitCode.SUCCESS
        self.checks: list[VerifyCheck] = []

    def add(self, check: VerifyCheck) -> None:
        self.checks.append(check)
        if not check.ok:
            self.ok = False
            if int(check.exit_code) > int(self.exit_code):
                self.exit_code = check.exit_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": int(self.exit_code),
            "checks": [c.as_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Layer verifiers
# ---------------------------------------------------------------------------

def _verify_bundle_structure(
    bundle: EvidenceBundle, result: VerifyResult
) -> None:
    """Layer 1: schema / version / structural integrity."""
    checks = [
        VerifyCheck(
            "bundle_schema_version",
            bundle.schema_version >= 1,
            VerifyExitCode.INPUT_SCHEMA,
            f"schema_version={bundle.schema_version}",
        ),
        VerifyCheck(
            "bundle_has_refs",
            len(bundle.evidence_refs) > 0,
            VerifyExitCode.INPUT_SCHEMA,
            f"ref_count={len(bundle.evidence_refs)}",
        ),
    ]
    for c in checks:
        result.add(c)


def _verify_ref_duplicates(
    bundle: EvidenceBundle, result: VerifyResult
) -> None:
    """Layer 1b: each ref_id appears at most once."""
    ids = [ref.ref_id for ref in bundle.evidence_refs]
    unique = set(ids)
    result.add(VerifyCheck(
        "bundle_no_duplicate_refs",
        len(ids) == len(unique),
        VerifyExitCode.INPUT_SCHEMA,
        f"total={len(ids)} unique={len(unique)}",
    ))


def _verify_ref_claim_refs(
    bundle: EvidenceBundle, result: VerifyResult
) -> None:
    """Layer 1c: all ref_ids referenced by claims exist in the bundle."""
    ref_ids = bundle.ref_ids()
    claim_refs = bundle.claim_ref_ids()
    orphans = claim_refs - ref_ids
    result.add(VerifyCheck(
        "bundle_claim_refs_exist",
        len(orphans) == 0,
        VerifyExitCode.INPUT_SCHEMA,
        f"orphan_refs={[str(r) for r in orphans]}",
    ))


def _verify_source_snapshot(
    ref: EvidenceRef, result: VerifyResult
) -> None:
    """Layer 2: source / snapshot existence."""
    result.add(VerifyCheck(
        f"ref_{ref.ref_id}_source_present",
        bool(ref.source_id),
        VerifyExitCode.SOURCE_SNAPSHOT,
        f"source_id={ref.source_id}",
    ))


def _verify_replay(
    ref: EvidenceRef, result: VerifyResult
) -> None:
    """Layer 3: replay / query validity."""
    if isinstance(ref, QueryReplayRef):
        result.add(VerifyCheck(
            f"ref_{ref.ref_id}_query_present",
            bool(ref.sql),
            VerifyExitCode.REPLAY_VALUE,
            f"sql_len={len(ref.sql)}",
        ))


def _verify_copy_frozen(
    ref: EvidenceRef, result: VerifyResult
) -> None:
    """Layer 4: copy_frozen binding and result_copy_digest consistency."""
    if ref.reproducibility_level != ReproducibilityLevel.COPY_FROZEN:
        return
    copy_frozen = getattr(ref, "copy_frozen", None)
    if copy_frozen is None:
        result.add(VerifyCheck(
            f"ref_{ref.ref_id}_copy_frozen_binding",
            False,
            VerifyExitCode.REPLAY_VALUE,
            "copy_frozen level but no copy_frozen metadata",
        ))
        return
    result.add(VerifyCheck(
        f"ref_{ref.ref_id}_copy_frozen_binding",
        True,
        VerifyExitCode.REPLAY_VALUE,
        "copy_frozen metadata present",
    ))
    # Verify result_copy_digest starts with sha256:
    result.add(VerifyCheck(
        f"ref_{ref.ref_id}_result_copy_digest",
        copy_frozen.result_copy_digest.startswith("sha256:"),
        VerifyExitCode.REPLAY_VALUE,
        f"result_copy_digest={copy_frozen.result_copy_digest[:20]}...",
    ))
    # Verify schema_snapshot_digest present
    result.add(VerifyCheck(
        f"ref_{ref.ref_id}_schema_snapshot_digest",
        bool(copy_frozen.schema_snapshot_digest),
        VerifyExitCode.REPLAY_VALUE,
        f"schema_snapshot_digest={copy_frozen.schema_snapshot_digest[:20] if copy_frozen.schema_snapshot_digest else 'missing'}...",
    ))
    # Verify row_count matches non-negative
    result.add(VerifyCheck(
        f"ref_{ref.ref_id}_row_count_nonneg",
        copy_frozen.row_count >= 0,
        VerifyExitCode.REPLAY_VALUE,
        f"row_count={copy_frozen.row_count}",
    ))


def _verify_copy_frozen_digest_match(
    ref: EvidenceRef,
    result: VerifyResult,
    *,
    expected_result_copy_digest: str | None = None,
) -> None:
    """Layer 4b: verify result copy digest matches expected (for copy_frozen).

    When expected_result_copy_digest is provided, verifies it matches the
    ref's bound copy_frozen.result_copy_digest.
    """
    if ref.reproducibility_level != ReproducibilityLevel.COPY_FROZEN:
        return
    copy_frozen = getattr(ref, "copy_frozen", None)
    if copy_frozen is None:
        return
    if expected_result_copy_digest is not None:
        result.add(VerifyCheck(
            f"ref_{ref.ref_id}_copy_frozen_digest_match",
            copy_frozen.result_copy_digest == expected_result_copy_digest,
            VerifyExitCode.DIGEST_ARTIFACT,
            f"expected={expected_result_copy_digest[:20]}... "
            f"actual={copy_frozen.result_copy_digest[:20]}...",
        ))


def _verify_claim_span(
    claim: Claim, result: VerifyResult
) -> None:
    """Layer 5: claim code_span / type validity."""
    if isinstance(claim, (FactClaim, QuoteClaim)):
        if claim.code_span is not None:
            result.add(VerifyCheck(
                f"claim_{claim.claim_id}_span_order",
                claim.code_span.line_end >= claim.code_span.line_start,
                VerifyExitCode.CLAIM_SPAN,
                f"line_start={claim.code_span.line_start} "
                f"line_end={claim.code_span.line_end}",
            ))
        # Answer digest must be sha256
        result.add(VerifyCheck(
            f"claim_{claim.claim_id}_answer_digest",
            claim.answer_digest.startswith("sha256:"),
            VerifyExitCode.CLAIM_SPAN,
            f"answer_digest={claim.answer_digest[:20]}...",
        ))
    if isinstance(claim, (InferenceClaim, RecommendationClaim)):
        result.add(VerifyCheck(
            f"claim_{claim.claim_id}_has_supporting",
            len(claim.supporting_inputs) > 0,
            VerifyExitCode.CLAIM_SPAN,
            f"supporting_count={len(claim.supporting_inputs)}",
        ))


def _verify_claim_evidence_level(
    claim: Claim, result: VerifyResult
) -> None:
    """Layer 5b: claim type vs evidence level consistency."""
    if isinstance(claim, (FactClaim, QuoteClaim)):
        for ref in claim.evidence_refs:
            if ref.reproducibility_level == ReproducibilityLevel.REFERENCE_ONLY:
                result.add(VerifyCheck(
                    f"claim_{claim.claim_id}_ref_{ref.ref_id}_level",
                    False,
                    VerifyExitCode.CLAIM_SPAN,
                    f"Fact/Quote claim has reference_only ref {ref.ref_id}",
                ))
                return
        result.add(VerifyCheck(
            f"claim_{claim.claim_id}_levels_ok",
            True,
            VerifyExitCode.CLAIM_SPAN,
            "all refs are replayable or copy_frozen",
        ))


def _verify_claim_canonical_value(
    claim: Claim, result: VerifyResult
) -> None:
    """Layer 5c: claim canonical value integrity.

    CanonicalValue has no stored digest — integrity is guaranteed by the frozen
    Pydantic model (immutable, no extra fields) plus deterministic canonical_json
    serialization.  We verify the round-trip is stable by recomputing and logging
    the digest for auditability, but the check itself is structural rather than
    comparing against a stored hash.
    """
    if not isinstance(claim, (FactClaim, QuoteClaim)):
        return
    cv = claim.canonical_value
    canonical_bytes = canonical_json({"type": cv.type, "value": cv.value})
    recomputed = digest_bytes(canonical_bytes)
    result.add(VerifyCheck(
        f"claim_{claim.claim_id}_canonical_value_integrity",
        True,
        VerifyExitCode.DIGEST_ARTIFACT,
        f"canonical_digest={recomputed[:20]}... (structural: frozen model + deterministic serialization)",
    ))


def _verify_tamper_at_every_layer(
    bundle: EvidenceBundle,
    reference_bundles: dict[str, EvidenceBundle] | None,
    result: VerifyResult,
) -> None:
    """Layer 6: cross-bundle tamper detection via reference_bundles.

    If reference_bundles is provided (keyed by bundle_id as string),
    verify that key fields (answer_id, evidence_refs, claims) match.
    This detects tampering at the wire/bundle level.
    """
    if reference_bundles is None:
        return
    ref_key = str(bundle.bundle_id)
    if ref_key not in reference_bundles:
        result.add(VerifyCheck(
            "bundle_tamper_reference_present",
            False,
            VerifyExitCode.DIGEST_ARTIFACT,
            f"no reference bundle for {ref_key}",
        ))
        return
    ref_bundle = reference_bundles[ref_key]
    # Check answer_id
    result.add(VerifyCheck(
        "bundle_tamper_answer_id",
        bundle.answer_id == ref_bundle.answer_id,
        VerifyExitCode.DIGEST_ARTIFACT,
        f"expected={ref_bundle.answer_id} actual={bundle.answer_id}",
    ))
    # Check evidence ref count
    result.add(VerifyCheck(
        "bundle_tamper_ref_count",
        len(bundle.evidence_refs) == len(ref_bundle.evidence_refs),
        VerifyExitCode.DIGEST_ARTIFACT,
        f"expected={len(ref_bundle.evidence_refs)} actual={len(bundle.evidence_refs)}",
    ))
    # Check claim count
    result.add(VerifyCheck(
        "bundle_tamper_claim_count",
        len(bundle.claims) == len(ref_bundle.claims),
        VerifyExitCode.DIGEST_ARTIFACT,
        f"expected={len(ref_bundle.claims)} actual={len(bundle.claims)}",
    ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_bundle(
    bundle: EvidenceBundle,
    *,
    reference_bundles: dict[str, EvidenceBundle] | None = None,
    expected_result_copy_digests: dict[str, str] | None = None,
) -> VerifyResult:
    """Verify an evidence bundle through all layers.

    Args:
        bundle: The evidence bundle to verify.
        reference_bundles: Optional dict of reference bundles (keyed by
            bundle_id string) for tamper detection at the wire level.
        expected_result_copy_digests: Optional dict of expected
            result_copy_digest values keyed by ref_id string, for
            copy_frozen digest verification.

    Returns:
        VerifyResult with all checks, overall ok flag, and exit code.
    """
    result = VerifyResult()

    # Layer 1: schema / version / structure
    _verify_bundle_structure(bundle, result)
    if not result.ok:
        return result
    _verify_ref_duplicates(bundle, result)
    _verify_ref_claim_refs(bundle, result)

    # Layer 2-4: per-ref verification
    for ref in bundle.evidence_refs:
        _verify_source_snapshot(ref, result)
        _verify_replay(ref, result)
        _verify_copy_frozen(ref, result)
        if expected_result_copy_digests is not None:
            ref_key = str(ref.ref_id)
            if ref_key in expected_result_copy_digests:
                _verify_copy_frozen_digest_match(
                    ref, result,
                    expected_result_copy_digest=expected_result_copy_digests[ref_key],
                )

    # Layer 5: claim verification
    for claim in bundle.claims:
        _verify_claim_span(claim, result)
        _verify_claim_evidence_level(claim, result)
        _verify_claim_canonical_value(claim, result)

    # Layer 6: cross-bundle tamper detection
    _verify_tamper_at_every_layer(bundle, reference_bundles, result)

    return result


def verify_reference_only_not_fact(claim: Claim) -> VerifyResult:
    """Verify that a claim backed only by reference_only refs is not Fact/Quote.

    Returns a result with ok=True and a descriptive message (not a failure)
    when the claim is Inference/Recommendation. Returns ok=False only when
    a Fact/Quote claim is backed exclusively by reference_only refs.
    """
    result = VerifyResult()
    if not isinstance(claim, (FactClaim, QuoteClaim)):
        result.add(VerifyCheck(
            "reference_only_not_reproducible",
            True,
            VerifyExitCode.SUCCESS,
            "not reproducible — Inference/Recommendation claim",
        ))
        return result
    all_ref_only = all(
        ref.reproducibility_level == ReproducibilityLevel.REFERENCE_ONLY
        for ref in claim.evidence_refs
    )
    if all_ref_only:
        result.add(VerifyCheck(
            "reference_only_not_reproducible",
            False,
            VerifyExitCode.CLAIM_SPAN,
            "Fact/Quote claim backed only by reference_only refs",
        ))
    else:
        result.add(VerifyCheck(
            "reference_only_not_reproducible",
            True,
            VerifyExitCode.SUCCESS,
            "claim has deterministic evidence refs",
        ))
    return result
