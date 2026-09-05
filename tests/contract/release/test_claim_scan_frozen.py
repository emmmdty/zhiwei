"""S9 冻结契约：strict release checker（A 档，S9-T5）。

release 表面（README/docs/demo）上的公开数字必须经由 claim marker 绑定 Claim Registry；
无 artifact 支撑的数字、fixture/live 混写、过期 claim 必须被确定性拦截。
扫描结果确定性：同输入同输出，排序按 (path, line, code)。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zhiwei.agents.claims import ClaimRecord, ClaimScope, ClaimStatus
from zhiwei.release.checker import Finding, FindingCode, scan_release_surface


def _claim(
    *,
    claim_id: str = "factqa-v1.accuracy",
    status: ClaimStatus = ClaimStatus.OFFLINE_VERIFIED,
    environment: str = "offline-fixture",
    date: str = "2026-09-05",
    value: str = "0.95",
) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        statement="accuracy {{accuracy}}",
        scope=ClaimScope(
            mode="offline",
            model="reference-fixture",
            version="1",
            date=date,
            corpus="factqa-v1",
            environment=environment,
        ),
        status=status,
        bound_value=value,
    )


def _scan(text: str, **kwargs: object) -> tuple[Finding, ...]:
    registry = {"factqa-v1.accuracy": _claim(**kwargs)}  # type: ignore[arg-type]
    return scan_release_surface(
        files={"docs/CLAIMS.md": text},
        registry=registry,
        now="2026-09-05",
        stale_after_days=180,
    )


CLAIMS_BLOCK = "<!-- claims:start -->\n{body}\n<!-- claims:end -->"


class TestDeterministicScan:
    def test_same_input_same_findings(self) -> None:
        text = CLAIMS_BLOCK.format(
            body="FactQA accuracy 0.95（offline fixture）"
        )
        assert _scan(text) == _scan(text)

    def test_findings_sorted_by_path_line_code(self) -> None:
        text = CLAIMS_BLOCK.format(
            body="accuracy 0.99\n\naccuracy 0.90\n\n{{claim:unknown.id}} 0.5"
        )
        findings = _scan(text)
        keys = [(f.path, f.line, f.code.value) for f in findings]
        assert keys == sorted(keys)


class TestFabricatedNumber:
    def test_unmarked_number_in_claims_block_flagged(self) -> None:
        text = CLAIMS_BLOCK.format(body="FactQA accuracy 0.99")
        findings = _scan(text)
        codes = {f.code for f in findings}
        assert FindingCode.UNSUPPORTED_NUMBER in codes

    def test_marked_verified_number_passes(self) -> None:
        text = CLAIMS_BLOCK.format(body="FactQA accuracy {{claim:factqa-v1.accuracy}}")
        assert _scan(text) == ()

    def test_planned_claim_reference_flagged(self) -> None:
        # planned/implemented 的 claim 没有 artifact 支撑，引用即拦截。
        text = CLAIMS_BLOCK.format(body="FactQA accuracy {{claim:factqa-v1.accuracy}}")
        findings = _scan(text, status=ClaimStatus.PLANNED)
        assert any(f.code == FindingCode.UNSUPPORTED_NUMBER for f in findings)

    def test_unknown_claim_reference_flagged(self) -> None:
        text = CLAIMS_BLOCK.format(body="accuracy {{claim:unknown.id}} 0.5")
        findings = _scan(text)
        assert any(f.code == FindingCode.UNKNOWN_CLAIM for f in findings)

    def test_numbers_outside_claims_block_not_scanned(self) -> None:
        # 只有声明表内的数字受本检查约束；正文叙述不属于 release 表面。
        text = "历史记录里提到 accuracy 0.42，不属于声明表。"
        assert _scan(text) == ()


class TestFixtureLiveMix:
    def test_fixture_claim_presented_as_live_flagged(self) -> None:
        text = CLAIMS_BLOCK.format(
            body="FactQA accuracy {{claim:factqa-v1.accuracy}} (live)"
        )
        findings = _scan(text)
        assert any(f.code == FindingCode.FIXTURE_LIVE_MIX for f in findings)

    def test_live_claim_with_live_scope_passes(self) -> None:
        text = CLAIMS_BLOCK.format(
            body="FactQA accuracy {{claim:factqa-v1.accuracy}} (live)"
        )
        findings = _scan(text, environment="live-production")
        assert not any(f.code == FindingCode.FIXTURE_LIVE_MIX for f in findings)


class TestStaleClaim:
    def test_stale_scope_date_flagged(self) -> None:
        text = CLAIMS_BLOCK.format(body="FactQA accuracy {{claim:factqa-v1.accuracy}}")
        findings = _scan(text, date="2026-01-01")
        assert any(f.code == FindingCode.STALE_ARTIFACT for f in findings)

    def test_fresh_claim_not_flagged(self) -> None:
        text = CLAIMS_BLOCK.format(body="FactQA accuracy {{claim:factqa-v1.accuracy}}")
        assert _scan(text, date="2026-09-05") == ()


class TestFindingShape:
    def test_finding_carries_location_and_no_content_leak(self) -> None:
        text = CLAIMS_BLOCK.format(body="FactQA accuracy 0.99")
        findings = _scan(text)
        assert findings
        finding: Finding = findings[0]
        assert finding.path == "docs/CLAIMS.md"
        assert finding.line >= 1
        # finding 不携带被扫描文本原文（避免把表面内容复制进报告）。
        assert "0.99" not in finding.model_dump_json()

    def test_finding_is_immutable(self) -> None:
        finding = Finding(
            code=FindingCode.UNKNOWN_CLAIM,
            path="a.md",
            line=1,
            detail="x",
            claim_id=None,
        )
        with pytest.raises(ValidationError):
            finding.line = 2  # type: ignore[misc]
