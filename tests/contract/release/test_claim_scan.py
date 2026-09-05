"""S9-T5 非 frozen 契约：fixture 驱动的确定性扫描与表面渲染。

fixtures/surface 下播种三类缺陷（fabricated number / fixture-as-live / stale
artifact）与两个干净表面（README 正文数字、demo 口径表），断言精确到
(code, path, line) 的确定性 findings 序列。渲染侧只验证 fail-closed 拒绝路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhiwei.agents.claims import ClaimRecord, ClaimScope, ClaimStatus
from zhiwei.release.checker import FindingCode, scan_release_surface
from zhiwei.release.templates import RenderRefused, render_release_surface

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "surface"

NOW = "2026-09-06"
STALE_AFTER_DAYS = 180


AUTO_VALUE = object()


def _claim(
    claim_id: str,
    *,
    status: ClaimStatus = ClaimStatus.OFFLINE_VERIFIED,
    environment: str = "offline-fixture",
    date: str = "2026-09-05",
    value: str | object | None = AUTO_VALUE,
) -> ClaimRecord:
    if value is AUTO_VALUE:
        value = (
            "0.95"
            if status in (ClaimStatus.OFFLINE_VERIFIED, ClaimStatus.LIVE_VERIFIED)
            else None
        )
    return ClaimRecord(
        claim_id=claim_id,
        statement="FactQA accuracy {{accuracy}}",
        scope=ClaimScope(
            mode="offline",
            model="reference-fixture",
            version="1",
            date=date,
            corpus="factqa-v1",
            environment=environment,
        ),
        status=status,
        bound_value=value,  # type: ignore[arg-type]
    )


def _registry() -> dict[str, ClaimRecord]:
    return {
        "factqa-v1.accuracy": _claim("factqa-v1.accuracy"),
        "factqa-v1.stale-accuracy": _claim(
            "factqa-v1.stale-accuracy", date="2026-01-01"
        ),
    }


def _surface_files() -> dict[str, str]:
    return {
        path.relative_to(FIXTURES).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(FIXTURES.rglob("*.md"))
    }


class TestSeededSurfaceScan:
    def test_seeded_defects_produce_exact_deterministic_findings(self) -> None:
        findings = scan_release_surface(
            _surface_files(), _registry(), now=NOW, stale_after_days=STALE_AFTER_DAYS
        )
        assert [(f.code.value, f.path, f.line) for f in findings] == [
            ("unsupported_number", "docs/CLAIMS.md", 2),
            ("fixture_live_mix", "docs/CLAIMS.md", 4),
            ("stale_artifact", "docs/CLAIMS.md", 6),
            ("unknown_claim", "docs/CLAIMS.md", 8),
        ]

    def test_findings_carry_claim_ids_but_not_surface_text(self) -> None:
        findings = scan_release_surface(
            _surface_files(), _registry(), now=NOW, stale_after_days=STALE_AFTER_DAYS
        )
        assert [f.claim_id for f in findings] == [
            None,
            "factqa-v1.accuracy",
            "factqa-v1.stale-accuracy",
            "unknown.id",
        ]
        for finding in findings:
            assert "0.99" not in finding.detail
            assert "0.5" not in finding.detail

    def test_scan_is_idempotent(self) -> None:
        assert scan_release_surface(
            _surface_files(), _registry(), now=NOW, stale_after_days=STALE_AFTER_DAYS
        ) == scan_release_surface(
            _surface_files(), _registry(), now=NOW, stale_after_days=STALE_AFTER_DAYS
        )

    def test_clean_surfaces_produce_no_findings(self) -> None:
        files = _surface_files()
        for path in ("README.md", "demo/manifest.md"):
            findings = scan_release_surface(
                {path: files[path]}, _registry(), now=NOW, stale_after_days=STALE_AFTER_DAYS
            )
            assert findings == (), f"{path} should be clean, got {findings}"

    def test_fixture_live_mix_is_case_insensitive(self) -> None:
        files = _surface_files()
        text = files["docs/CLAIMS.md"].replace("(live)", "(LIVE)")
        findings = scan_release_surface(
            {"docs/CLAIMS.md": text}, _registry(), now=NOW, stale_after_days=STALE_AFTER_DAYS
        )
        assert any(f.code is FindingCode.FIXTURE_LIVE_MIX for f in findings)


class TestAdjacentNumberBinding:
    """marker 紧邻的数字只有等于该 verified claim 的 bound_value 才被放行。

    缺口：紧邻 verified marker 的任意数字此前被无条件豁免，伪造数字可借
    「贴着已验证 claim」逃逸 UNSUPPORTED_NUMBER。
    """

    def _scan(self, body: str, registry: dict[str, ClaimRecord]) -> tuple:
        return scan_release_surface(
            {"docs/CLAIMS.md": f"<!-- claims:start -->\n{body}\n<!-- claims:end -->"},
            registry,
            now=NOW,
            stale_after_days=STALE_AFTER_DAYS,
        )

    def test_adjacent_number_matching_bound_value_passes(self) -> None:
        findings = self._scan(
            "FactQA accuracy {{claim:factqa-v1.accuracy}} 0.95", _registry()
        )
        assert findings == ()

    def test_adjacent_mismatching_number_flagged_with_claim_id(self) -> None:
        findings = self._scan(
            "FactQA accuracy {{claim:factqa-v1.accuracy}} 0.99", _registry()
        )
        assert [(f.code, f.claim_id) for f in findings] == [
            (FindingCode.UNSUPPORTED_NUMBER, "factqa-v1.accuracy")
        ]

    def test_adjacent_number_with_unbound_verified_claim_flagged(self) -> None:
        registry = {
            "factqa-v1.unbound": _claim("factqa-v1.unbound", value=None),
        }
        findings = self._scan(
            "FactQA accuracy {{claim:factqa-v1.unbound}} 0.95", registry
        )
        assert [(f.code, f.claim_id) for f in findings] == [
            (FindingCode.UNSUPPORTED_NUMBER, "factqa-v1.unbound")
        ]

    def test_zero_width_gap_is_not_adjacency(self) -> None:
        # 零宽字符不是空白：strip() 剥不掉，数字不算紧邻 marker——照常拦截，
        # 且不允许借 verified marker 的 bound_value 相等性洗白（间隔不透明）。
        registry = {
            "factqa-v1.unbound": _claim("factqa-v1.unbound", value="0.99"),
        }
        findings = self._scan(
            "FactQA accuracy {{claim:factqa-v1.unbound}}​0.99", registry
        )
        assert any(f.code is FindingCode.UNSUPPORTED_NUMBER for f in findings)


class TestRenderSurface:
    def test_verified_claim_marker_renders_bound_value(self) -> None:
        text = "FactQA accuracy {{claim:factqa-v1.accuracy}}"
        rendered = render_release_surface(text, _registry())
        assert rendered == "FactQA accuracy 0.95"

    def test_unknown_claim_refuses(self) -> None:
        with pytest.raises(RenderRefused):
            render_release_surface("{{claim:unknown.id}}", _registry())

    def test_unverified_status_refuses(self) -> None:
        registry = _registry() | {
            "factqa-v1.planned": _claim("factqa-v1.planned", status=ClaimStatus.PLANNED)
        }
        with pytest.raises(RenderRefused):
            render_release_surface("{{claim:factqa-v1.planned}}", registry)

    def test_retired_claim_refuses(self) -> None:
        registry = _registry() | {
            "factqa-v1.retired": _claim("factqa-v1.retired", status=ClaimStatus.RETIRED)
        }
        with pytest.raises(RenderRefused):
            render_release_surface("{{claim:factqa-v1.retired}}", registry)
