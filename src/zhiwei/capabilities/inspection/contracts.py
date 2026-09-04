"""Contract validation: capability drift, list_changed, update diff, published pin.

Covers S4 spec §7:
- capability drift/list_changed/update diff/published pin/suspend/revoke
- duplicate/effect_unknown/idempotency/read-after-write
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.capabilities.inspection.schema import (
    InspectionFinding,
    Severity,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContractViolation(_FrozenModel):
    """Structured contract violation detail."""

    rule: str
    severity: Severity
    message: str
    path: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class ContractReport(_FrozenModel):
    """Deterministic contract inspection report."""

    findings: tuple[InspectionFinding, ...] = ()
    violations: tuple[ContractViolation, ...] = ()
    passed: bool = True

    def add(self, finding: InspectionFinding) -> ContractReport:
        updated = [*list(self.findings), finding]
        return self.model_copy(
            update={
                "findings": tuple(updated),
                "passed": self.passed and not finding.is_blocking(),
            }
        )

    def add_violation(self, violation: ContractViolation) -> ContractReport:
        updated_violations = [*list(self.violations), violation]
        finding = InspectionFinding(
            check=violation.rule,
            severity=violation.severity,
            message=violation.message,
        )
        updated_findings = [*list(self.findings), finding]
        return self.model_copy(
            update={
                "violations": tuple(updated_violations),
                "findings": tuple(updated_findings),
                "passed": self.passed and not finding.is_blocking(),
            }
        )

    def merge(self, other: ContractReport) -> ContractReport:
        combined_findings = list(self.findings) + list(other.findings)
        combined_violations = list(self.violations) + list(other.violations)
        has_blocking = any(f.is_blocking() for f in combined_findings)
        return ContractReport(
            findings=tuple(combined_findings),
            violations=tuple(combined_violations),
            passed=self.passed and other.passed and not has_blocking,
        )


def detect_capability_drift(
    *,
    bound_content_digest: str,
    current_content_digest: str,
    bound_test_digest: str,
    current_test_digest: str,
) -> ContractReport:
    """Detect capability drift: content or test digest mismatch after binding.

    Any content/test/risk change invalidates existing approvals (S4 spec §3).
    """
    report = ContractReport()

    if bound_content_digest and current_content_digest and bound_content_digest != current_content_digest:
        report = report.add_violation(
            ContractViolation(
                rule="capability_drift_content",
                severity=Severity.HIGH,
                message="Content digest mismatch: capability has drifted since binding",
                context={
                    "bound": bound_content_digest,
                    "current": current_content_digest,
                },
            )
        )

    if bound_test_digest and current_test_digest and bound_test_digest != current_test_digest:
        report = report.add_violation(
            ContractViolation(
                rule="capability_drift_test",
                severity=Severity.HIGH,
                message="Test digest mismatch: test results have changed since binding",
                context={
                    "bound": bound_test_digest,
                    "current": current_test_digest,
                },
            )
        )

    return report


def validate_list_changed(
    previous_tools: list[str],
    current_tools: list[str],
    *,
    bound_only: bool = True,
) -> ContractReport:
    """Validate list_changed notification against bound tool set.

    If bound_only is True, only the bound tools can change (no new tools added).
    """
    report = ContractReport()

    removed = set(previous_tools) - set(current_tools)
    added = set(current_tools) - set(previous_tools)

    if removed:
        report = report.add_violation(
            ContractViolation(
                rule="list_changed_tools_removed",
                severity=Severity.HIGH,
                message=f"Tools removed from bound set: {sorted(removed)}",
                context={"removed": sorted(removed)},
            )
        )

    if added and bound_only:
        report = report.add_violation(
            ContractViolation(
                rule="list_changed_tools_added",
                severity=Severity.MEDIUM,
                message=f"New tools added: {sorted(added)}; not in bound set",
                context={"added": sorted(added)},
            )
        )

    return report


def validate_update_diff(
    previous_schema: dict[str, Any],
    current_schema: dict[str, Any],
    *,
    tool_name: str = "",
) -> ContractReport:
    """Validate that an update diff doesn't introduce breaking changes.

    Breaking changes: type changes, required field additions, removed fields.
    """
    report = ContractReport()
    path_prefix = f"tool:{tool_name}" if tool_name else "schema"

    prev_props = previous_schema.get("properties", {})
    curr_props = current_schema.get("properties", {})
    prev_required = set(previous_schema.get("required", []))
    curr_required = set(current_schema.get("required", []))

    # Removed fields
    removed_fields = set(prev_props) - set(curr_props)
    if removed_fields:
        report = report.add_violation(
            ContractViolation(
                rule="update_breaking_field_removed",
                severity=Severity.HIGH,
                message=f"Fields removed: {sorted(removed_fields)}",
                path=f"{path_prefix}.properties",
            )
        )

    # Type changes
    for field_name in set(prev_props) & set(curr_props):
        prev_type = prev_props[field_name].get("type")
        curr_type = curr_props[field_name].get("type")
        if prev_type and curr_type and prev_type != curr_type:
            report = report.add_violation(
                ContractViolation(
                    rule="update_breaking_type_change",
                    severity=Severity.HIGH,
                    message=f"Field '{field_name}' type changed from {prev_type} to {curr_type}",
                    path=f"{path_prefix}.{field_name}",
                )
            )

    # New required fields
    new_required = curr_required - prev_required
    if new_required:
        report = report.add_violation(
            ContractViolation(
                rule="update_breaking_new_required",
                severity=Severity.HIGH,
                message=f"New required fields added: {sorted(new_required)}",
                path=f"{path_prefix}.required",
            )
        )

    return report


def validate_published_pin(
    *,
    published_digest: str,
    current_digest: str,
    version_id: str = "",
) -> ContractReport:
    """Validate that a published capability's digest is still pinned.

    Published capabilities must not change without explicit re-approval.
    """
    report = ContractReport()

    if published_digest and current_digest and published_digest != current_digest:
        report = report.add_violation(
            ContractViolation(
                rule="published_pin_violation",
                severity=Severity.CRITICAL,
                message="Published capability digest has changed without re-approval",
                context={
                    "version_id": version_id,
                    "published": published_digest,
                    "current": current_digest,
                },
            )
        )

    return report


def validate_idempotency(
    *,
    request_id: str,
    existing_request_ids: frozenset[str],
) -> ContractReport:
    """Validate idempotency: detect duplicate requests."""
    report = ContractReport()

    if request_id in existing_request_ids:
        report = report.add_violation(
            ContractViolation(
                rule="duplicate_request",
                severity=Severity.MEDIUM,
                message=f"Duplicate request detected: {request_id}",
                context={"request_id": request_id},
            )
        )

    return report


def validate_effect_unknown(
    *,
    effect: str,
    allowed_effects: frozenset[str] = frozenset({"apply", "preview", "dry_run"}),
) -> ContractReport:
    """Validate that effect is in the allowed set."""
    report = ContractReport()

    if effect not in allowed_effects:
        report = report.add_violation(
            ContractViolation(
                rule="effect_unknown",
                severity=Severity.HIGH,
                message=f"Unknown effect '{effect}'; allowed: {sorted(allowed_effects)}",
                context={"effect": effect, "allowed": sorted(allowed_effects)},
            )
        )

    return report
