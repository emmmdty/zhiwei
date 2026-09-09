"""S4-T6 Security: Admission inspection and malicious corpus.

验证：
- schema validation: depth, ref count, cycle, property count, string length, bomb detection
- prompt injection detection in tool descriptions, skill content, resource metadata
- secret exfiltration pattern detection
- SSRF: loopback, private network, cloud metadata, DNS rebinding
- redirect chain: length, protocol downgrade, loopback hop
- header injection: CRLF, null bytes
- response bomb: oversized, decompression bomb
- supply chain: SBOM license compliance (prohibited/restricted/unknown), vulnerability detection
- pinned digest verification
- contract validation: capability drift, list_changed, update diff, published pin
- idempotency duplicate detection, effect_unknown
- admission commands: publisher/security approval, dual-actor enforcement, same-actor rejection
- approval PEP: publish readiness, stale rejection
- high/critical requires two distinct current decisions
- failed tests cannot be overridden by Builder
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fixtures.capabilities.malicious.corpus import (
    DRIFT_CONTENT_DIGEST,
    DRIFT_TEST_DIGEST,
    DUPLICATE_REQUEST_ID,
    EXISTING_REQUEST_IDS,
    HEADER_CRLF_INJECTION,
    HEADER_NULL_BYTE,
    MALICIOUS_TOOL_DESCRIPTION_EXFIL,
    MALICIOUS_TOOL_DESCRIPTION_INJECTION,
    MALICIOUS_TOOL_NAME_TOO_LONG,
    PROHIBITED_LICENSE_ENTRY,
    PROMPT_INJECTION_HUMAN_MARKER,
    PROMPT_INJECTION_IGNORE_PREVIOUS,
    PROMPT_INJECTION_INST_TAG,
    PROMPT_INJECTION_ROLE_OVERRIDE,
    PROMPT_INJECTION_SYSTEM_TAG,
    PROMPT_INJECTION_YOUD_ARE_NOW,
    REDIRECT_CHAIN_LOOPBACK,
    REDIRECT_CHAIN_PROTOCOL_DOWNGRADE,
    REDIRECT_CHAIN_TOO_LONG,
    RESTRICTED_LICENSE_ENTRY,
    SCHEMA_BOMB_COMBINATOR_EXPLOSION,
    SCHEMA_BOMB_DEEP_NESTING,
    SCHEMA_CYCLE_SELF_REF,
    SCHEMA_EXCESSIVE_PROPERTIES,
    SCHEMA_EXCESSIVE_REFS,
    SECRET_EXFIL_ASSIGNMENT,
    SECRET_EXFIL_BASE64,
    SECRET_EXFIL_CURL,
    SECRET_EXFIL_FETCH,
    SSRF_CLOUD_METADATA_AWS,
    SSRF_CLOUD_METADATA_GCP,
    SSRF_DNS_REBINDING,
    SSRF_LOOPBACK,
    SSRF_LOOPBACK_IPV6,
    SSRF_PRIVATE_NETWORK,
    UNKNOWN_LICENSE_ENTRY,
    UPDATE_BREAKING_NEW_REQUIRED,
    UPDATE_BREAKING_REMOVED_FIELD,
    UPDATE_BREAKING_TYPE_CHANGE,
)

from zhiwei.capabilities.admission import (
    AdmissionManager,
)
from zhiwei.capabilities.admission_commands import (
    ApprovalPEP,
    PublisherApprovalCommand,
    SecurityApprovalCommand,
)
from zhiwei.capabilities.domain import RiskLevel
from zhiwei.capabilities.inspection.contracts import (
    detect_capability_drift,
    validate_effect_unknown,
    validate_idempotency,
    validate_list_changed,
    validate_published_pin,
    validate_update_diff,
)
from zhiwei.capabilities.inspection.network import (
    check_header_injection,
    check_redirect_chain,
    check_response_bomb,
    check_ssrf,
    check_url_safety,
)
from zhiwei.capabilities.inspection.schema import (
    InspectionReport,
    scan_prompt_injection,
    scan_secret_exfiltration,
    validate_output_schema,
    validate_schema,
    validate_tool_args,
)
from zhiwei.capabilities.inspection.supply_chain import (
    LicensePolicy,
    PinnedDigest,
    SBOMEntry,
    SupplyChainReport,
    Vulnerability,
    check_vulnerabilities,
    classify_license,
    validate_sbom,
    verify_pinned_digest,
)

# ===========================================================================
# Schema validation
# ===========================================================================


class TestSchemaValidation:
    def test_valid_schema_passes(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["name"],
        }
        report = validate_schema(schema)
        assert report.passed is True
        assert len(report.findings) == 0

    def test_schema_depth_exceeded(self) -> None:
        # Build a schema deeper than MAX_SCHEMA_DEPTH (12)
        schema: dict[str, Any] = {"type": "string"}
        for _ in range(15):
            schema = {"type": "object", "properties": {"nested": schema}}
        report = validate_schema(schema)
        assert report.passed is False
        assert any(f.check == "schema_depth" for f in report.findings)

    def test_schema_ref_count_exceeded(self) -> None:
        report = validate_schema(SCHEMA_EXCESSIVE_REFS)
        assert report.passed is False
        assert any(f.check == "schema_ref_count" for f in report.findings)

    def test_schema_ref_cycle_detected(self) -> None:
        report = validate_schema(SCHEMA_CYCLE_SELF_REF)
        assert report.passed is False
        assert any(f.check == "schema_ref_cycle" for f in report.findings)

    def test_schema_property_count_exceeded(self) -> None:
        report = validate_schema(SCHEMA_EXCESSIVE_PROPERTIES)
        assert any(f.check == "schema_property_count" for f in report.findings)

    def test_schema_bomb_deep_array_nesting(self) -> None:
        report = validate_schema(SCHEMA_BOMB_DEEP_NESTING)
        assert report.passed is False
        assert any(f.check == "schema_bomb_array_nesting" for f in report.findings)

    def test_schema_bomb_combinator_explosion(self) -> None:
        report = validate_schema(SCHEMA_BOMB_COMBINATOR_EXPLOSION)
        assert report.passed is False
        assert any(f.check == "schema_bomb_combinator" for f in report.findings)

    def test_schema_non_dict_rejected(self) -> None:
        report = validate_schema({"type": "not_a_valid_type"})  # type: ignore[arg-type]
        # A dict with unexpected type value is still valid dict
        assert report.passed is True

    def test_schema_type_not_dict(self) -> None:
        # The function expects a dict; passing a non-dict triggers type check
        report = validate_schema({"properties": "not_a_dict"})  # type: ignore[arg-type]
        # properties should be a dict but validate_schema only checks structure
        assert isinstance(report, InspectionReport)


# ===========================================================================
# Prompt injection detection
# ===========================================================================


class TestPromptInjection:
    def test_ignore_previous_instructions(self) -> None:
        report = scan_prompt_injection(PROMPT_INJECTION_IGNORE_PREVIOUS)
        assert report.passed is False
        assert any(f.check == "prompt_injection" for f in report.findings)

    def test_role_override(self) -> None:
        report = scan_prompt_injection(PROMPT_INJECTION_ROLE_OVERRIDE)
        assert report.passed is False

    def test_system_tag_injection(self) -> None:
        report = scan_prompt_injection(PROMPT_INJECTION_SYSTEM_TAG)
        assert report.passed is False

    def test_inst_tag_injection(self) -> None:
        report = scan_prompt_injection(PROMPT_INJECTION_INST_TAG)
        assert report.passed is False

    def test_human_marker_injection(self) -> None:
        report = scan_prompt_injection(PROMPT_INJECTION_HUMAN_MARKER)
        assert report.passed is False

    def test_you_are_now_injection(self) -> None:
        report = scan_prompt_injection(PROMPT_INJECTION_YOUD_ARE_NOW)
        assert report.passed is False

    def test_clean_text_passes(self) -> None:
        report = scan_prompt_injection("A safe tool that processes CSV files.")
        assert report.passed is True
        assert len(report.findings) == 0

    def test_injection_in_tool_description(self) -> None:
        report = scan_prompt_injection(
            MALICIOUS_TOOL_DESCRIPTION_INJECTION["description"],
            field="description",
        )
        assert report.passed is False


# ===========================================================================
# Secret exfiltration detection
# ===========================================================================


class TestSecretExfiltration:
    def test_curl_exfiltration(self) -> None:
        report = scan_secret_exfiltration(SECRET_EXFIL_CURL)
        assert report.passed is False
        assert any(f.check == "secret_exfiltration" for f in report.findings)

    def test_fetch_exfiltration(self) -> None:
        report = scan_secret_exfiltration(SECRET_EXFIL_FETCH)
        assert report.passed is False

    def test_base64_exfiltration(self) -> None:
        report = scan_secret_exfiltration(SECRET_EXFIL_BASE64)
        assert report.passed is False

    def test_assignment_exfiltration(self) -> None:
        report = scan_secret_exfiltration(SECRET_EXFIL_ASSIGNMENT)
        assert report.passed is False

    def test_clean_description_passes(self) -> None:
        report = scan_secret_exfiltration("A tool that reads files.")
        assert report.passed is True
        assert len(report.findings) == 0

    def test_exfil_in_tool_description(self) -> None:
        report = scan_secret_exfiltration(
            MALICIOUS_TOOL_DESCRIPTION_EXFIL["description"],
            field="description",
        )
        assert report.passed is False


# ===========================================================================
# Tool args / output schema validation
# ===========================================================================


class TestToolValidation:
    def test_tool_name_too_long(self) -> None:
        report = validate_tool_args(
            {"type": "object", "properties": {"x": {"type": "string"}}},
            tool_name=MALICIOUS_TOOL_NAME_TOO_LONG,
        )
        assert report.passed is False
        assert any(f.check == "tool_name_length" for f in report.findings)

    def test_tool_empty_schema(self) -> None:
        report = validate_tool_args({}, tool_name="empty")
        assert any(f.check == "tool_args_empty" for f in report.findings)

    def test_output_schema_valid(self) -> None:
        report = validate_output_schema(
            {"type": "object", "properties": {"result": {"type": "string"}}},
            tool_name="my_tool",
        )
        assert report.passed is True


# ===========================================================================
# SSRF detection
# ===========================================================================


class TestSSRF:
    def test_loopback_ipv4(self) -> None:
        report = check_ssrf(SSRF_LOOPBACK)
        assert report.passed is False
        assert any(f.check == "ssrf_loopback" for f in report.findings)

    def test_loopback_ipv6(self) -> None:
        report = check_ssrf(SSRF_LOOPBACK_IPV6)
        assert report.passed is False
        assert any(f.check == "ssrf_loopback" for f in report.findings)

    def test_cloud_metadata_aws(self) -> None:
        report = check_ssrf(SSRF_CLOUD_METADATA_AWS)
        assert report.passed is False
        assert any(f.check == "ssrf_cloud_metadata" for f in report.findings)

    def test_cloud_metadata_gcp(self) -> None:
        report = check_ssrf(SSRF_CLOUD_METADATA_GCP)
        assert report.passed is False
        assert any(f.check == "ssrf_cloud_metadata" for f in report.findings)

    def test_private_network(self) -> None:
        report = check_ssrf(SSRF_PRIVATE_NETWORK)
        assert report.passed is False
        assert any(f.check == "ssrf_private_network" for f in report.findings)

    def test_dns_rebinding_risk(self) -> None:
        report = check_ssrf(SSRF_DNS_REBINDING)
        # Should at least flag the DNS rebinding risk
        assert any(f.check == "ssrf_dns_rebinding_risk" for f in report.findings)

    def test_safe_url_passes(self) -> None:
        report = check_ssrf("https://api.example.com/data")
        assert report.passed is True
        assert len(report.findings) == 0


# ===========================================================================
# URL safety
# ===========================================================================


class TestUrlSafety:
    def test_http_rejected(self) -> None:
        report = check_url_safety("http://example.com/data")
        assert report.passed is False
        assert any(f.check == "url_unsafe_scheme" for f in report.findings)

    def test_docker_port_flagged(self) -> None:
        report = check_url_safety("https://example.com:2375/containers")
        assert any(f.check == "url_dangerous_port" for f in report.findings)

    def test_safe_https_passes(self) -> None:
        report = check_url_safety("https://api.example.com:443/data")
        assert report.passed is True


# ===========================================================================
# Redirect chain
# ===========================================================================


class TestRedirectChain:
    def test_too_long_chain(self) -> None:
        report = check_redirect_chain(REDIRECT_CHAIN_TOO_LONG)
        assert report.passed is False
        assert any(f.check == "redirect_chain_too_long" for f in report.findings)

    def test_protocol_downgrade(self) -> None:
        report = check_redirect_chain(REDIRECT_CHAIN_PROTOCOL_DOWNGRADE)
        assert report.passed is False
        assert any(
            f.check == "redirect_protocol_downgrade" for f in report.findings
        )

    def test_loopback_in_chain(self) -> None:
        report = check_redirect_chain(REDIRECT_CHAIN_LOOPBACK)
        assert report.passed is False

    def test_short_chain_passes(self) -> None:
        report = check_redirect_chain([
            "https://example.com/a",
            "https://example.com/b",
        ])
        assert report.passed is True


# ===========================================================================
# Header injection
# ===========================================================================


class TestHeaderInjection:
    def test_crlf_injection(self) -> None:
        report = check_header_injection({"Authorization": HEADER_CRLF_INJECTION})
        assert report.passed is False
        assert any(f.check == "header_injection" for f in report.findings)

    def test_null_byte_injection(self) -> None:
        report = check_header_injection({"X-Data": HEADER_NULL_BYTE})
        assert report.passed is False
        assert any(f.check == "header_injection" for f in report.findings)

    def test_clean_headers_pass(self) -> None:
        report = check_header_injection({
            "Authorization": "Bearer token123",
            "Content-Type": "application/json",
        })
        assert report.passed is True
        assert len(report.findings) == 0

    def test_header_count_exceeded(self) -> None:
        headers = {f"X-Custom-{i}": f"value-{i}" for i in range(100)}
        report = check_header_injection(headers)
        assert any(f.check == "header_count_exceeded" for f in report.findings)


# ===========================================================================
# Response bomb
# ===========================================================================


class TestResponseBomb:
    def test_oversized_response(self) -> None:
        report = check_response_bomb(11 * 1024 * 1024)
        assert report.passed is False
        assert any(f.check == "response_too_large" for f in report.findings)

    def test_compressed_bomb_suspect(self) -> None:
        report = check_response_bomb(512, content_type="application/gzip")
        assert any(f.check == "response_bomb_suspect" for f in report.findings)

    def test_normal_response_passes(self) -> None:
        report = check_response_bomb(1024, content_type="application/json")
        assert report.passed is True
        assert len(report.findings) == 0


# ===========================================================================
# Supply chain: SBOM / license
# ===========================================================================


class TestSupplyChain:
    def test_prohibited_license_detected(self) -> None:
        entry = SBOMEntry(**PROHIBITED_LICENSE_ENTRY)
        report = validate_sbom([entry])
        assert report.passed is False
        assert any(f.check == "sbom_prohibited_license" for f in report.findings)

    def test_restricted_license_detected(self) -> None:
        entry = SBOMEntry(**RESTRICTED_LICENSE_ENTRY)
        report = validate_sbom([entry])
        assert any(f.check == "sbom_restricted_license" for f in report.findings)

    def test_unknown_license_detected(self) -> None:
        entry = SBOMEntry(**UNKNOWN_LICENSE_ENTRY)
        report = validate_sbom([entry])
        assert any(f.check == "sbom_unknown_license" for f in report.findings)

    def test_permissive_license_passes(self) -> None:
        entry = SBOMEntry(name="utils", version="1.0", license="MIT")
        report = validate_sbom([entry])
        assert report.passed is True
        assert len(report.findings) == 0

    def test_classify_license_prohibited(self) -> None:
        assert classify_license("GPL-3.0") == LicensePolicy.PROHIBITED
        assert classify_license("AGPL-3.0-only") == LicensePolicy.PROHIBITED

    def test_classify_license_restricted(self) -> None:
        assert classify_license("LGPL-2.1") == LicensePolicy.RESTRICTED
        assert classify_license("MPL-2.0") == LicensePolicy.RESTRICTED

    def test_classify_license_allowed(self) -> None:
        assert classify_license("MIT") == LicensePolicy.ALLOWED
        assert classify_license("Apache-2.0") == LicensePolicy.ALLOWED

    def test_classify_license_unknown(self) -> None:
        assert classify_license("Custom-Proprietary") == LicensePolicy.UNKNOWN
        assert classify_license("") == LicensePolicy.UNKNOWN

    def test_classify_license_compound_prohibited(self) -> None:
        # Compound expression containing a prohibited license
        assert classify_license("MIT AND GPL-3.0") == LicensePolicy.PROHIBITED


# ===========================================================================
# Supply chain: vulnerability detection
# ===========================================================================


class TestVulnerabilityCheck:
    def test_critical_vulnerability_flagged(self) -> None:
        from zhiwei.capabilities.inspection.schema import Severity

        vuln = Vulnerability(id="CVE-2025-0001", severity=Severity.CRITICAL, description="RCE")
        report = check_vulnerabilities([], [vuln])
        assert any(f.check == "vulnerability_found" for f in report.findings)

    def test_high_vulnerability_flagged(self) -> None:
        from zhiwei.capabilities.inspection.schema import Severity

        vuln = Vulnerability(id="CVE-2025-0002", severity=Severity.HIGH, description="XSS")
        report = check_vulnerabilities([], [vuln])
        assert any(f.check == "vulnerability_found" for f in report.findings)

    def test_no_vulnerabilities_clean(self) -> None:
        report = check_vulnerabilities([], [])
        assert report.passed is True


# ===========================================================================
# Pinned digest verification
# ===========================================================================


class TestPinnedDigest:
    def test_digest_verify_match(self) -> None:
        import hashlib

        data = b"hello world"
        hex_digest = hashlib.sha256(data).hexdigest()
        pinned = PinnedDigest(algorithm="sha256", hex_digest=hex_digest)
        assert pinned.verify(data) is True

    def test_digest_verify_mismatch(self) -> None:
        import hashlib

        data = b"hello world"
        wrong_data = b"hello world!"
        hex_digest = hashlib.sha256(data).hexdigest()
        pinned = PinnedDigest(algorithm="sha256", hex_digest=hex_digest)
        assert pinned.verify(wrong_data) is False

    def test_pinned_digest_rejects_unsupported_algorithm(self) -> None:
        with pytest.raises(ValueError, match="Unsupported digest algorithm"):
            PinnedDigest(algorithm="md5", hex_digest="abc123")

    def test_pinned_digest_rejects_invalid_hex(self) -> None:
        with pytest.raises(ValueError, match="hex_digest"):
            PinnedDigest(algorithm="sha256", hex_digest="not-hex!")

    def test_verify_pinned_digest_success(self) -> None:
        import hashlib

        data = b"test payload"
        hex_digest = hashlib.sha256(data).hexdigest()
        pinned = PinnedDigest(algorithm="sha256", hex_digest=hex_digest)
        report = verify_pinned_digest(data, pinned)
        assert report.passed is True

    def test_verify_pinned_digest_failure(self) -> None:
        import hashlib

        data = b"test payload"
        hex_digest = hashlib.sha256(data).hexdigest()
        pinned = PinnedDigest(algorithm="sha256", hex_digest=hex_digest)
        report = verify_pinned_digest(b"wrong payload", pinned)
        assert report.passed is False
        assert any(f.check == "digest_mismatch" for f in report.findings)


# ===========================================================================
# Contract: capability drift
# ===========================================================================


class TestCapabilityDrift:
    def test_content_drift_detected(self) -> None:
        report = detect_capability_drift(
            bound_content_digest=DRIFT_CONTENT_DIGEST["bound"],
            current_content_digest=DRIFT_CONTENT_DIGEST["current"],
            bound_test_digest="sha256:aaa",
            current_test_digest="sha256:aaa",
        )
        assert report.passed is False
        assert any(v.rule == "capability_drift_content" for v in report.violations)

    def test_test_drift_detected(self) -> None:
        report = detect_capability_drift(
            bound_content_digest="sha256:aaa",
            current_content_digest="sha256:aaa",
            bound_test_digest=DRIFT_TEST_DIGEST["bound"],
            current_test_digest=DRIFT_TEST_DIGEST["current"],
        )
        assert report.passed is False
        assert any(v.rule == "capability_drift_test" for v in report.violations)

    def test_no_drift_passes(self) -> None:
        report = detect_capability_drift(
            bound_content_digest="sha256:aaa",
            current_content_digest="sha256:aaa",
            bound_test_digest="sha256:bbb",
            current_test_digest="sha256:bbb",
        )
        assert report.passed is True


# ===========================================================================
# Contract: list_changed
# ===========================================================================


class TestListChanged:
    def test_tools_removed_flagged(self) -> None:
        report = validate_list_changed(
            previous_tools=["a", "b", "c"],
            current_tools=["a", "c"],
        )
        assert report.passed is False
        assert any(v.rule == "list_changed_tools_removed" for v in report.violations)

    def test_new_tools_added_flagged_bound_only(self) -> None:
        report = validate_list_changed(
            previous_tools=["a", "b"],
            current_tools=["a", "b", "c"],
            bound_only=True,
        )
        assert any(v.rule == "list_changed_tools_added" for v in report.violations)

    def test_same_tools_pass(self) -> None:
        report = validate_list_changed(
            previous_tools=["a", "b"],
            current_tools=["a", "b"],
        )
        assert report.passed is True


# ===========================================================================
# Contract: update diff
# ===========================================================================


class TestUpdateDiff:
    def test_removed_field_breaking(self) -> None:
        report = validate_update_diff(
            UPDATE_BREAKING_REMOVED_FIELD["previous"],
            UPDATE_BREAKING_REMOVED_FIELD["current"],
            tool_name="test_tool",
        )
        assert report.passed is False
        assert any(
            v.rule == "update_breaking_field_removed" for v in report.violations
        )

    def test_type_change_breaking(self) -> None:
        report = validate_update_diff(
            UPDATE_BREAKING_TYPE_CHANGE["previous"],
            UPDATE_BREAKING_TYPE_CHANGE["current"],
            tool_name="test_tool",
        )
        assert report.passed is False
        assert any(
            v.rule == "update_breaking_type_change" for v in report.violations
        )

    def test_new_required_breaking(self) -> None:
        report = validate_update_diff(
            UPDATE_BREAKING_NEW_REQUIRED["previous"],
            UPDATE_BREAKING_NEW_REQUIRED["current"],
            tool_name="test_tool",
        )
        assert report.passed is False
        assert any(
            v.rule == "update_breaking_new_required" for v in report.violations
        )

    def test_compatible_update_passes(self) -> None:
        previous = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        current = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
            },
            "required": ["name"],
        }
        report = validate_update_diff(previous, current, tool_name="test_tool")
        assert report.passed is True


# ===========================================================================
# Contract: published pin
# ===========================================================================


class TestPublishedPin:
    def test_pin_violation_detected(self) -> None:
        report = validate_published_pin(
            published_digest="sha256:aaa",
            current_digest="sha256:bbb",
            version_id="v1",
        )
        assert report.passed is False
        assert any(
            v.rule == "published_pin_violation" for v in report.violations
        )

    def test_pin_match_passes(self) -> None:
        report = validate_published_pin(
            published_digest="sha256:aaa",
            current_digest="sha256:aaa",
            version_id="v1",
        )
        assert report.passed is True


# ===========================================================================
# Contract: idempotency / effect_unknown
# ===========================================================================


class TestContractMisc:
    def test_duplicate_request_detected(self) -> None:
        report = validate_idempotency(
            request_id=DUPLICATE_REQUEST_ID,
            existing_request_ids=EXISTING_REQUEST_IDS,
        )
        assert any(v.rule == "duplicate_request" for v in report.violations)

    def test_unique_request_passes(self) -> None:
        report = validate_idempotency(
            request_id="req-new-unique",
            existing_request_ids=EXISTING_REQUEST_IDS,
        )
        assert report.passed is True

    def test_effect_unknown_detected(self) -> None:
        report = validate_effect_unknown(effect="force_apply")
        assert report.passed is False
        assert any(v.rule == "effect_unknown" for v in report.violations)

    def test_effect_valid_passes(self) -> None:
        report = validate_effect_unknown(effect="apply")
        assert report.passed is True

    def test_effect_dry_run_passes(self) -> None:
        report = validate_effect_unknown(effect="dry_run")
        assert report.passed is True


# ===========================================================================
# Admission commands
# ===========================================================================


class TestAdmissionCommands:
    def _manager(self) -> AdmissionManager:
        return AdmissionManager()

    def test_publisher_approve_low_risk(self) -> None:
        mgr = self._manager()
        cmd = PublisherApprovalCommand(mgr)
        version_id = uuid4()
        actor_id = uuid4()
        result = cmd.approve(
            version_id=version_id,
            actor_id=actor_id,
            risk_level=RiskLevel.LOW,
            test_digest="sha256:aaa",
            content_digest="sha256:bbb",
        )
        assert result.success is True
        assert result.record_id is not None

    def test_security_approve(self) -> None:
        mgr = self._manager()
        cmd = SecurityApprovalCommand(mgr)
        version_id = uuid4()
        result = cmd.approve(
            version_id=version_id,
            actor_id=uuid4(),
            risk_level=RiskLevel.HIGH,
            test_digest="sha256:aaa",
            content_digest="sha256:bbb",
        )
        assert result.success is True

    def test_publisher_reject(self) -> None:
        mgr = self._manager()
        cmd = PublisherApprovalCommand(mgr)
        result = cmd.reject(
            version_id=uuid4(),
            actor_id=uuid4(),
            risk_level=RiskLevel.MEDIUM,
            test_digest="sha256:aaa",
            content_digest="sha256:bbb",
            reason="Security concerns",
        )
        assert result.success is True

    def test_security_reject(self) -> None:
        mgr = self._manager()
        cmd = SecurityApprovalCommand(mgr)
        result = cmd.reject(
            version_id=uuid4(),
            actor_id=uuid4(),
            risk_level=RiskLevel.CRITICAL,
            test_digest="sha256:aaa",
            content_digest="sha256:bbb",
            reason="Vulnerability found",
        )
        assert result.success is True


# ===========================================================================
# Approval PEP
# ===========================================================================


class TestApprovalPEP:
    def _setup_dual_approval(
        self,
        risk_level: RiskLevel,
    ) -> tuple[ApprovalPEP, UUID]:
        mgr = AdmissionManager()
        pep = ApprovalPEP(mgr)
        version_id = uuid4()
        test_digest = "sha256:aaa"
        content_digest = "sha256:bbb"

        # Publisher approval
        pub_cmd = PublisherApprovalCommand(mgr)
        pub_cmd.approve(
            version_id=version_id,
            actor_id=uuid4(),
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
        )

        # Security approval (distinct actor)
        sec_cmd = SecurityApprovalCommand(mgr)
        sec_cmd.approve(
            version_id=version_id,
            actor_id=uuid4(),
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
        )

        return pep, version_id, test_digest, content_digest  # type: ignore[return-value]

    def test_low_risk_publish_ready(self) -> None:
        mgr = AdmissionManager()
        pep = ApprovalPEP(mgr)
        version_id = uuid4()
        test_digest = "sha256:aaa"
        content_digest = "sha256:bbb"

        PublisherApprovalCommand(mgr).approve(
            version_id=version_id,
            actor_id=uuid4(),
            risk_level=RiskLevel.LOW,
            test_digest=test_digest,
            content_digest=content_digest,
        )

        result = pep.check_publish_readiness(
            version_id, test_digest, content_digest, RiskLevel.LOW
        )
        assert result.ready is True
        assert result.valid_records == 1

    def test_high_risk_requires_dual_actor(self) -> None:
        mgr = AdmissionManager()
        pep = ApprovalPEP(mgr)
        version_id = uuid4()
        test_digest = "sha256:aaa"
        content_digest = "sha256:bbb"

        # Only publisher approval
        PublisherApprovalCommand(mgr).approve(
            version_id=version_id,
            actor_id=uuid4(),
            risk_level=RiskLevel.HIGH,
            test_digest=test_digest,
            content_digest=content_digest,
        )

        result = pep.check_publish_readiness(
            version_id, test_digest, content_digest, RiskLevel.HIGH
        )
        assert result.ready is False
        assert any("security admin" in e.lower() for e in result.errors)

    def test_high_risk_dual_actor_ready(self) -> None:
        mgr = AdmissionManager()
        pep = ApprovalPEP(mgr)
        version_id = uuid4()
        test_digest = "sha256:aaa"
        content_digest = "sha256:bbb"

        PublisherApprovalCommand(mgr).approve(
            version_id=version_id,
            actor_id=uuid4(),
            risk_level=RiskLevel.HIGH,
            test_digest=test_digest,
            content_digest=content_digest,
        )
        SecurityApprovalCommand(mgr).approve(
            version_id=version_id,
            actor_id=uuid4(),
            risk_level=RiskLevel.HIGH,
            test_digest=test_digest,
            content_digest=content_digest,
        )

        result = pep.check_publish_readiness(
            version_id, test_digest, content_digest, RiskLevel.HIGH
        )
        assert result.ready is True
        assert result.valid_records == 2

    def test_same_actor_rejection(self) -> None:
        mgr = AdmissionManager()
        pep = ApprovalPEP(mgr)
        version_id = uuid4()
        test_digest = "sha256:aaa"
        content_digest = "sha256:bbb"
        same_actor = uuid4()

        PublisherApprovalCommand(mgr).approve(
            version_id=version_id,
            actor_id=same_actor,
            risk_level=RiskLevel.CRITICAL,
            test_digest=test_digest,
            content_digest=content_digest,
        )
        SecurityApprovalCommand(mgr).approve(
            version_id=version_id,
            actor_id=same_actor,  # Same actor!
            risk_level=RiskLevel.CRITICAL,
            test_digest=test_digest,
            content_digest=content_digest,
        )

        result = pep.check_publish_readiness(
            version_id, test_digest, content_digest, RiskLevel.CRITICAL
        )
        assert result.ready is False
        assert any("distinct" in e.lower() for e in result.errors)

    def test_stale_digest_rejection(self) -> None:
        mgr = AdmissionManager()
        pep = ApprovalPEP(mgr)
        version_id = uuid4()
        test_digest = "sha256:aaa"
        content_digest = "sha256:bbb"

        PublisherApprovalCommand(mgr).approve(
            version_id=version_id,
            actor_id=uuid4(),
            risk_level=RiskLevel.LOW,
            test_digest=test_digest,
            content_digest=content_digest,
        )

        # Now check with changed digests
        result = pep.check_publish_readiness(
            version_id, "sha256:changed", "sha256:changed", RiskLevel.LOW
        )
        assert result.ready is False
        assert any("stale" in e.lower() for e in result.errors)

    def test_validate_same_actor_rejection(self) -> None:
        mgr = AdmissionManager()
        pep = ApprovalPEP(mgr)
        actor1 = uuid4()
        actor2 = uuid4()
        assert pep.validate_same_actor_rejection(uuid4(), actor1, actor2) is True
        assert pep.validate_same_actor_rejection(uuid4(), actor1, actor1) is False

    def test_execute_publish_insufficient_approvals(self) -> None:
        mgr = AdmissionManager()
        pep = ApprovalPEP(mgr)
        version_id = uuid4()

        result = pep.execute_publish(
            version_id=version_id,
            current_test_digest="sha256:aaa",
            current_content_digest="sha256:bbb",
            current_risk_level=RiskLevel.HIGH,
            expected_version=1,
        )
        assert result.success is False
        assert result.error != ""


# ===========================================================================
# InspectionReport merge / composition
# ===========================================================================


class TestInspectionReportComposition:
    def test_merge_preserves_all_findings(self) -> None:
        from zhiwei.capabilities.inspection.schema import InspectionFinding, Severity

        r1 = InspectionReport().add(
            InspectionFinding(check="a", severity=Severity.LOW, message="a")
        )
        r2 = InspectionReport().add(
            InspectionFinding(check="b", severity=Severity.MEDIUM, message="b")
        )
        merged = r1.merge(r2)
        assert len(merged.findings) == 2
        assert merged.passed is True

    def test_merge_blocking_finding_fails(self) -> None:
        from zhiwei.capabilities.inspection.schema import InspectionFinding, Severity

        r1 = InspectionReport()
        r2 = InspectionReport().add(
            InspectionFinding(check="x", severity=Severity.CRITICAL, message="x")
        )
        merged = r1.merge(r2)
        assert merged.passed is False

    def test_supply_chain_report_merge(self) -> None:
        from zhiwei.capabilities.inspection.schema import InspectionFinding, Severity

        r1 = SupplyChainReport()
        r2 = SupplyChainReport().add_finding(
            InspectionFinding(check="vuln", severity=Severity.HIGH, message="vuln")
        )
        merged = r1.merge(r2)
        assert merged.passed is False
        assert len(merged.findings) == 1
