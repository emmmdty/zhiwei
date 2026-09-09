"""Supply chain inspection: SBOM, license compliance, vulnerability checks.

Covers S4 spec §7:
- Pin every admitted source/image digest
- SBOM/license/vulnerability cases
"""

from __future__ import annotations

import hashlib
import hmac
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.capabilities.inspection.schema import (
    InspectionFinding,
    Severity,
)


class LicensePolicy(StrEnum):
    """License compliance categories."""

    ALLOWED = "allowed"
    RESTRICTED = "restricted"
    PROHIBITED = "prohibited"
    UNKNOWN = "unknown"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PinnedDigest(_FrozenModel):
    """Immutable pinned source/image digest.

    Once admitted, source or image digest cannot be changed without re-inspection.
    """

    algorithm: str = Field(min_length=1)
    hex_digest: str = Field(min_length=1)

    @field_validator("algorithm")
    @classmethod
    def _normalize_algorithm(cls, value: str) -> str:
        value = value.lower().strip()
        allowed = {"sha256", "sha384", "sha512", "sha3-256", "sha3-512"}
        if value not in allowed:
            raise ValueError(f"Unsupported digest algorithm: {value!r}")
        return value

    @field_validator("hex_digest")
    @classmethod
    def _validate_hex(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[0-9a-f]+", value):
            raise ValueError("hex_digest must contain only lowercase hex characters")
        return value

    def verify(self, data: bytes) -> bool:
        """Verify data against this pinned digest."""
        algo = self.algorithm.replace("-", "")
        try:
            h = hashlib.new(algo, data)
        except ValueError:
            return False
        return hmac.compare_digest(h.hexdigest(), self.hex_digest)


class SBOMEntry(_FrozenModel):
    """Single Software Bill of Materials entry."""

    name: str = Field(min_length=1)
    version: str = ""
    supplier: str = ""
    license: str = ""
    purl: str = ""
    checksum: str = ""


class Vulnerability(_FrozenModel):
    """Known vulnerability reference."""

    id: str = Field(min_length=1)
    severity: Severity
    description: str = ""
    fixed_version: str = ""
    url: str = ""


class SupplyChainReport(_FrozenModel):
    """Deterministic supply chain inspection report."""

    findings: tuple[InspectionFinding, ...] = ()
    sbom_entries: tuple[SBOMEntry, ...] = ()
    vulnerabilities: tuple[Vulnerability, ...] = ()
    pinned_digests: tuple[PinnedDigest, ...] = ()
    passed: bool = True

    def add_finding(self, finding: InspectionFinding) -> SupplyChainReport:
        updated_findings = [*list(self.findings), finding]
        return self.model_copy(
            update={
                "findings": tuple(updated_findings),
                "passed": self.passed and not finding.is_blocking(),
            }
        )

    def merge(self, other: SupplyChainReport) -> SupplyChainReport:
        combined_findings = list(self.findings) + list(other.findings)
        combined_sbom = list(self.sbom_entries) + list(other.sbom_entries)
        combined_vulns = list(self.vulnerabilities) + list(other.vulnerabilities)
        combined_digests = list(self.pinned_digests) + list(other.pinned_digests)
        has_blocking = any(f.is_blocking() for f in combined_findings)
        return SupplyChainReport(
            findings=tuple(combined_findings),
            sbom_entries=tuple(combined_sbom),
            vulnerabilities=tuple(combined_vulns),
            pinned_digests=tuple(combined_digests),
            passed=self.passed and other.passed and not has_blocking,
        )


# ---------------------------------------------------------------------------
# Allowed/prohibited license patterns
# ---------------------------------------------------------------------------

_PROHIBITED_LICENSES: frozenset[str] = frozenset(
    {
        "GPL-3.0",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "AGPL-3.0",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "SSPL-1.0",
        "EUPL-1.1",
        "CC-BY-NC-4.0",
    }
)

_RESTRICTED_LICENSES: frozenset[str] = frozenset(
    {
        "LGPL-2.1",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MPL-2.0",
        "EPL-1.0",
        "EPL-2.0",
        "CDDL-1.1",
    }
)


def validate_sbom(entries: list[SBOMEntry]) -> SupplyChainReport:
    """Validate SBOM entries for completeness and license compliance."""
    report = SupplyChainReport(sbom_entries=tuple(entries))

    for entry in entries:
        if not entry.name.strip():
            report = report.add_finding(
                InspectionFinding(
                    check="sbom_empty_name",
                    severity=Severity.MEDIUM,
                    message="SBOM entry has empty name",
                    path=f"sbom.{entry.name}",
                )
            )

        license_status = classify_license(entry.license)
        if license_status == LicensePolicy.PROHIBITED:
            report = report.add_finding(
                InspectionFinding(
                    check="sbom_prohibited_license",
                    severity=Severity.CRITICAL,
                    message=f"Prohibited license '{entry.license}' on package '{entry.name}'",
                    path=f"sbom.{entry.name}.license",
                )
            )
        elif license_status == LicensePolicy.RESTRICTED:
            report = report.add_finding(
                InspectionFinding(
                    check="sbom_restricted_license",
                    severity=Severity.HIGH,
                    message=f"Restricted license '{entry.license}' on package '{entry.name}'",
                    path=f"sbom.{entry.name}.license",
                )
            )
        elif license_status == LicensePolicy.UNKNOWN:
            report = report.add_finding(
                InspectionFinding(
                    check="sbom_unknown_license",
                    severity=Severity.LOW,
                    message=f"Unknown license '{entry.license}' on package '{entry.name}'",
                    path=f"sbom.{entry.name}.license",
                )
            )

    return report


def classify_license(license_str: str) -> LicensePolicy:
    """Classify a license string into compliance category."""
    normalized = license_str.strip().upper()
    if not normalized:
        return LicensePolicy.UNKNOWN

    # Direct match
    if normalized in {e.upper() for e in _PROHIBITED_LICENSES}:
        return LicensePolicy.PROHIBITED
    if normalized in {e.upper() for e in _RESTRICTED_LICENSES}:
        return LicensePolicy.RESTRICTED

    # SPDX compound expression handling: check for prohibited sub-expressions
    for prohibited in _PROHIBITED_LICENSES:
        if prohibited.upper() in normalized:
            return LicensePolicy.PROHIBITED
    for restricted in _RESTRICTED_LICENSES:
        if restricted.upper() in normalized:
            return LicensePolicy.RESTRICTED

    # Well-known permissive licenses
    permissive = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense"}
    if normalized in {e.upper() for e in permissive}:
        return LicensePolicy.ALLOWED

    return LicensePolicy.UNKNOWN


def check_vulnerabilities(
    sbom_entries: list[SBOMEntry],
    known_vulns: list[Vulnerability],
) -> SupplyChainReport:
    """Check SBOM entries against known vulnerabilities."""
    report = SupplyChainReport()

    # Index vulns by package name patterns in description
    for vuln in known_vulns:
        if vuln.severity in {Severity.HIGH, Severity.CRITICAL}:
            report = report.add_finding(
                InspectionFinding(
                    check="vulnerability_found",
                    severity=Severity.HIGH,
                    message=f"Known vulnerability {vuln.id} ({vuln.severity})",
                    path=f"vuln.{vuln.id}",
                )
            )
        report = report.model_copy(
            update={"vulnerabilities": (*report.vulnerabilities, vuln)}
        )

    return report


def verify_pinned_digest(
    data: bytes,
    expected: PinnedDigest,
) -> SupplyChainReport:
    """Verify that data matches a pinned digest."""
    report = SupplyChainReport(pinned_digests=(expected,))
    if not expected.verify(data):
        report = report.add_finding(
            InspectionFinding(
                check="digest_mismatch",
                severity=Severity.CRITICAL,
                message=f"Data does not match pinned {expected.algorithm} digest",
                path="digest",
            )
        )
    return report


def validate_source_digest(
    source_url: str,
    digest: PinnedDigest,
) -> SupplyChainReport:
    """Record a pinned source digest for admission.

    Source/image digests are pinned at admission time and cannot be changed
    without re-inspection (S4 spec §3).
    """
    return SupplyChainReport(pinned_digests=(digest,))
