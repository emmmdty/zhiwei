"""Network inspection: SSRF, redirect, DNS rebinding, header injection, response bomb.

Covers S4 spec §7:
- SSRF, redirect, DNS rebinding, host override, header injection, response bomb
- stdio/script filesystem/network/process/resource escape corpus
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from zhiwei.capabilities.inspection.schema import (
    InspectionFinding,
    Severity,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_REDIRECT_CHAIN_LENGTH = 5
_MAX_RESPONSE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB
_MAX_HEADER_COUNT = 64
_MAX_HEADER_VALUE_LENGTH = 8192

# RFC reserved / loopback / link-local ranges
_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
)
_LINK_LOCAL_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)
_CLOUD_METADATA_IPS = (
    ipaddress.ip_network("169.254.169.254/32"),
)

_BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "[::1]",
        "0.0.0.0",
        "169.254.169.254",  # AWS/GCP metadata (IP form)
    }
)

_DNS_REBINDING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"),  # bare IP
    re.compile(r"^[\da-f:]+:[\da-f:]+$"),  # bare IPv6
)

_HEADER_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\r\n]"),  # CRLF injection
    re.compile(r"\x00"),  # null byte
)


class NetworkReport(_FrozenModel):
    """Deterministic network inspection report."""

    findings: tuple[InspectionFinding, ...] = ()
    passed: bool = True

    def add(self, finding: InspectionFinding) -> NetworkReport:
        updated = [*list(self.findings), finding]
        return self.model_copy(
            update={
                "findings": tuple(updated),
                "passed": self.passed and not finding.is_blocking(),
            }
        )

    def merge(self, other: NetworkReport) -> NetworkReport:
        combined = list(self.findings) + list(other.findings)
        has_blocking = any(f.is_blocking() for f in combined)
        return NetworkReport(
            findings=tuple(combined),
            passed=self.passed and other.passed and not has_blocking,
        )


def check_ssrf(url: str) -> NetworkReport:
    """Check a URL for SSRF risks: loopback, metadata, private IPs, cloud endpoints."""
    report = NetworkReport()
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # Check for IP-based targets
    try:
        addr = ipaddress.ip_address(hostname)
        for network in _LOOPBACK_NETWORKS:
            if addr in network:
                return report.add(
                    InspectionFinding(
                        check="ssrf_loopback",
                        severity=Severity.CRITICAL,
                        message=f"URL targets loopback address: {hostname}",
                        path="url",
                    )
                )
        for network in _CLOUD_METADATA_IPS:
            if addr in network:
                return report.add(
                    InspectionFinding(
                        check="ssrf_cloud_metadata",
                        severity=Severity.CRITICAL,
                        message=f"URL targets cloud metadata endpoint: {hostname}",
                        path="url",
                    )
                )
        for network in _PRIVATE_NETWORKS:
            if addr in network:
                report = report.add(
                    InspectionFinding(
                        check="ssrf_private_network",
                        severity=Severity.HIGH,
                        message=f"URL targets private network: {hostname}",
                        path="url",
                    )
                )
                break
    except ValueError:
        pass  # Not an IP literal

    # Check blocked hosts (non-IP hostnames)
    if hostname in _BLOCKED_HOSTS:
        return report.add(
            InspectionFinding(
                check="ssrf_blocked_host",
                severity=Severity.CRITICAL,
                message=f"URL targets blocked host: {hostname}",
                path="url",
            )
        )

    # Check for metadata hostnames
    metadata_hostnames: frozenset[str] = frozenset(
        {
            "metadata.google.internal",
            "metadata.amazonaws.com",
        }
    )
    metadata_suffixes = (".metadata.google.internal", ".metadata.amazonaws.com")
    if hostname in metadata_hostnames:
        return report.add(
            InspectionFinding(
                check="ssrf_cloud_metadata",
                severity=Severity.CRITICAL,
                message=f"URL targets cloud metadata hostname: {hostname}",
                path="url",
            )
        )
    for suffix in metadata_suffixes:
        if hostname.endswith(suffix):
            return report.add(
                InspectionFinding(
                    check="ssrf_cloud_metadata",
                    severity=Severity.CRITICAL,
                    message=f"URL targets cloud metadata hostname: {hostname}",
                    path="url",
                )
            )

    # DNS rebinding heuristic: hostname is a bare IP address
    for pattern in _DNS_REBINDING_PATTERNS:
        if pattern.match(hostname):
            report = report.add(
                InspectionFinding(
                    check="ssrf_dns_rebinding_risk",
                    severity=Severity.MEDIUM,
                    message=f"URL uses IP literal hostname, potential DNS rebinding: {hostname}",
                    path="url",
                )
            )
            break

    return report


def check_redirect_chain(redirects: list[str]) -> NetworkReport:
    """Validate a chain of redirect URLs for safety."""
    report = NetworkReport()

    if len(redirects) > _MAX_REDIRECT_CHAIN_LENGTH:
        report = report.add(
            InspectionFinding(
                check="redirect_chain_too_long",
                severity=Severity.HIGH,
                message=f"Redirect chain length {len(redirects)} exceeds limit of {_MAX_REDIRECT_CHAIN_LENGTH}",
                path="redirects",
            )
        )

    for idx, url in enumerate(redirects):
        url_report = check_ssrf(url)
        if url_report.findings:
            for finding in url_report.findings:
                redirect_finding = InspectionFinding(
                    check=f"redirect_{finding.check}",
                    severity=finding.severity,
                    message=f"Redirect hop {idx}: {finding.message}",
                    path=f"redirects[{idx}]",
                )
                report = report.add(redirect_finding)

    # Protocol downgrade check
    if redirects:
        first = urlparse(redirects[0])
        for subsequent in redirects[1:]:
            subsequent_parsed = urlparse(subsequent)
            if first.scheme == "https" and subsequent_parsed.scheme == "http":
                report = report.add(
                    InspectionFinding(
                        check="redirect_protocol_downgrade",
                        severity=Severity.HIGH,
                        message="Redirect chain downgrades from HTTPS to HTTP",
                        path="redirects",
                    )
                )
                break

    return report


def check_header_injection(headers: dict[str, str]) -> NetworkReport:
    """Check HTTP headers for injection patterns (CRLF, null bytes)."""
    report = NetworkReport()

    if len(headers) > _MAX_HEADER_COUNT:
        report = report.add(
            InspectionFinding(
                check="header_count_exceeded",
                severity=Severity.MEDIUM,
                message=f"Header count {len(headers)} exceeds limit of {_MAX_HEADER_COUNT}",
                path="headers",
            )
        )

    for name, value in headers.items():
        if len(value) > _MAX_HEADER_VALUE_LENGTH:
            report = report.add(
                InspectionFinding(
                    check="header_value_too_long",
                    severity=Severity.MEDIUM,
                    message=f"Header '{name}' value exceeds {_MAX_HEADER_VALUE_LENGTH} bytes",
                    path=f"headers.{name}",
                )
            )

        for pattern in _HEADER_INJECTION_PATTERNS:
            if pattern.search(value):
                report = report.add(
                    InspectionFinding(
                        check="header_injection",
                        severity=Severity.CRITICAL,
                        message=f"Header '{name}' contains injection pattern",
                        path=f"headers.{name}",
                    )
                )
                break

    return report


def check_response_bomb(
    response_size: int,
    content_type: str = "",
) -> NetworkReport:
    """Check response for bomb patterns: oversized responses, decompression bombs."""
    report = NetworkReport()

    if response_size > _MAX_RESPONSE_SIZE_BYTES:
        report = report.add(
            InspectionFinding(
                check="response_too_large",
                severity=Severity.HIGH,
                message=(
                    f"Response size {response_size} bytes exceeds "
                    f"limit of {_MAX_RESPONSE_SIZE_BYTES} bytes"
                ),
                path="response",
            )
        )

    # Suspicious content-type + small size hint (potential decompression bomb)
    bomb_types = {"application/gzip", "application/zip", "application/x-bzip2", "application/x-xz"}
    if content_type in bomb_types and response_size < 1024:
        report = report.add(
            InspectionFinding(
                check="response_bomb_suspect",
                severity=Severity.MEDIUM,
                message=(
                    f"Compressed response ({content_type}) is only {response_size} bytes; "
                    "potential decompression bomb"
                ),
                path="response",
            )
        )

    return report


def check_url_safety(url: str) -> NetworkReport:
    """Full URL safety check: SSRF, scheme, port, and host validation."""
    report = check_ssrf(url)
    parsed = urlparse(url)

    # Scheme check
    allowed_schemes = {"https"}
    if parsed.scheme not in allowed_schemes:
        report = report.add(
            InspectionFinding(
                check="url_unsafe_scheme",
                severity=Severity.HIGH,
                message=f"URL scheme '{parsed.scheme}' not in allowed set: {sorted(allowed_schemes)}",
                path="url.scheme",
            )
        )

    # Dangerous ports
    dangerous_ports = {22, 23, 25, 135, 139, 445, 1433, 1521, 2375, 2376, 3306, 3389, 5432, 6379, 27017}
    if parsed.port and parsed.port in dangerous_ports:
        report = report.add(
            InspectionFinding(
                check="url_dangerous_port",
                severity=Severity.MEDIUM,
                message=f"URL targets dangerous port: {parsed.port}",
                path="url.port",
            )
        )

    return report
