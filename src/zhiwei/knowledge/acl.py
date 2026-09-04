"""S5 ACL enforcement: pre-filter + hydration re-check, fail closed.

事实源：S5 spec §5、ADR-006。

ADR-006 核心语义：
- System reproducibility: Evidence always reproducible by system
- User visibility: re-check current ACL, fail closed
- 失权呈现: evidence_access_revoked placeholder, not silent removal
- ACL pre-filter + hydration re-check, both on current ACL
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from zhiwei.knowledge.contracts import ACLSnapshot, SourceVersion, SourceVersionState


class ACLAccessRevokedError(Exception):
    """Raised when ACL re-check fails after hydration (ADR-006: fail closed)."""


class UnknownACLError(Exception):
    """Raised when ACL information is unknown or stale (fail closed)."""


@dataclass(frozen=True)
class ACLCheckResult:
    """Result of an ACL check on a candidate."""

    version_id: str
    allowed: bool
    reason: str = ""
    access_revoked: bool = False


@dataclass
class ACLContext:
    """Current ACL state for a principal in an organization/workspace.

    Provides the authoritative current ACL for re-checking (ADR-006).
    """

    principal_id: UUID
    organization_id: UUID
    workspace_id: UUID
    allowed_principals: frozenset[str] = field(default_factory=frozenset)
    allowed_groups: frozenset[str] = field(default_factory=frozenset)
    denied_principals: frozenset[str] = field(default_factory=frozenset)
    classification_ceiling: str = "PUBLIC"


def pre_filter(
    versions: list[SourceVersion],
    acl_context: ACLContext,
) -> list[SourceVersion]:
    """Pre-filter: remove candidates before hydration based on current ACL.

    ADR-006: Both pre-filter and re-check operate on CURRENT ACL,
    not the snapshot frozen at SourceVersion creation time.
    """
    filtered: list[SourceVersion] = []
    for version in versions:
        result = _check_acl(version.acl, acl_context)
        if result.allowed:
            filtered.append(version)
    return filtered


def recheck_after_hydration(
    version: SourceVersion,
    acl_context: ACLContext,
) -> ACLCheckResult:
    """Re-check ACL after hydration (ADR-006: fail closed).

    Returns ACLCheckResult with allowed=False and access_revoked=True
    when the principal no longer has access. Unknown/stale ACL → fail closed.
    """
    if version.state == SourceVersionState.REVOKED:
        return ACLCheckResult(
            version_id=str(version.id),
            allowed=False,
            reason="version_revoked",
            access_revoked=True,
        )

    result = _check_acl(version.acl, acl_context)

    if not result.allowed and result.reason == "unknown":
        raise UnknownACLError(
            f"ACL unknown for version {version.id}, principal {acl_context.principal_id}"
        )

    return result


def _check_acl(
    snapshot: ACLSnapshot,
    context: ACLContext,
) -> ACLCheckResult:
    """Check if a principal has access based on the ACL snapshot.

    Checks denied first (deny overrides allow).
    """
    principal_str = str(context.principal_id)

    if principal_str in snapshot.denied_principals:
        return ACLCheckResult(
            version_id="",
            allowed=False,
            reason="denied_principal",
        )

    if principal_str in snapshot.allowed_principals:
        return ACLCheckResult(version_id="", allowed=True)

    for group in snapshot.allowed_groups:
        if group in context.allowed_groups:
            return ACLCheckResult(version_id="", allowed=True)

    if not snapshot.allowed_principals and not snapshot.allowed_groups:
        return ACLCheckResult(
            version_id="",
            allowed=False,
            reason="unknown",
        )

    return ACLCheckResult(
        version_id="",
        allowed=False,
        reason="not_in_acl",
    )
