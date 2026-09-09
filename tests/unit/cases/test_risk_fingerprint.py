"""typed RiskFingerprint 去重契约（spec s8 §4，RISK_EVAL §7）。

生产去重用 typed RiskFingerprint：同一次发现的重复触发（refresh/retry）必须被识别为
DUPLICATE，不得生成第二条 hypothesis（spec §6「刷新/重试不会复制 hypothesis」）。
semantic similarity 只提出 merge candidate，绝不自动合并。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from zhiwei.cases.risk_fingerprint import (
    DedupeDecision,
    MergeCandidate,
    RiskFingerprint,
    RiskFingerprintIndex,
)


def _fingerprint(**overrides: Any) -> RiskFingerprint:
    base = RiskFingerprint(
        kind="trend",
        metric="gross_margin_rate",
        entity_dim="product_line",
        entity_value="云梯-企业版",
        window_start="2024-06",
        window_end="2025-03",
        detector_version="1",
    )
    if not overrides:
        return base
    return RiskFingerprint(**{**base.model_dump(), **overrides})


class TestRiskFingerprint:
    def test_is_frozen(self) -> None:
        fp = _fingerprint()
        with pytest.raises(ValidationError):
            fp.metric = "other"  # type: ignore[misc]

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            RiskFingerprint(**{**_fingerprint().model_dump(), "score": 0.9})  # type: ignore[call-arg]

    def test_value_is_deterministic_content_digest(self) -> None:
        assert _fingerprint().value() == _fingerprint().value()
        assert _fingerprint().value().startswith("sha256:")

    def test_value_distinguishes_window(self) -> None:
        a = _fingerprint()
        b = _fingerprint(window_end="2025-04")
        assert a.value() != b.value()

    def test_value_distinguishes_detector_version(self) -> None:
        a = _fingerprint()
        b = _fingerprint(detector_version="2")
        assert a.value() != b.value()


class TestRiskFingerprintIndex:
    def test_first_registration_is_new(self) -> None:
        index = RiskFingerprintIndex()
        decision, existing = index.register(_fingerprint())
        assert decision is DedupeDecision.NEW
        assert existing is None

    def test_refresh_retry_is_duplicate(self) -> None:
        """spec §6：刷新/重试不会复制 hypothesis。"""
        index = RiskFingerprintIndex()
        index.register(_fingerprint())
        decision, existing = index.register(_fingerprint())
        assert decision is DedupeDecision.DUPLICATE
        assert existing == _fingerprint()

    def test_different_window_is_new(self) -> None:
        index = RiskFingerprintIndex()
        index.register(_fingerprint())
        decision, _ = index.register(_fingerprint(window_end="2025-04"))
        assert decision is DedupeDecision.NEW


class TestSemanticMergeCandidates:
    def test_same_kind_metric_overlapping_window_is_merge_candidate(self) -> None:
        """kind+metric 相同且窗口重叠：语义相似，只提议合并，不判重复。"""
        index = RiskFingerprintIndex()
        index.register(_fingerprint())
        candidates = index.merge_candidates(
            _fingerprint(window_start="2024-08", window_end="2025-05")
        )
        assert len(candidates) == 1
        cand = candidates[0]
        assert isinstance(cand, MergeCandidate)
        assert cand.similarity_reason == "same_kind_metric_overlapping_window"
        assert cand.window_iou > 0

    def test_different_entity_is_merge_candidate_not_duplicate(self) -> None:
        """同一趋势泄露到另一聚合维度（region 聚合了 product 趋势）：只提议合并。"""
        index = RiskFingerprintIndex()
        index.register(_fingerprint())
        decision, _ = index.register(_fingerprint(entity_dim="region", entity_value="华东"))
        assert decision is DedupeDecision.NEW
        candidates = index.merge_candidates(
            _fingerprint(entity_dim="region", entity_value="华东")
        )
        assert len(candidates) == 1

    def test_unrelated_kind_yields_no_candidates(self) -> None:
        index = RiskFingerprintIndex()
        index.register(_fingerprint())
        other = _fingerprint(kind="baseline_deviation", metric="dso_days", entity_dim="customer",
                             entity_value="C03", window_start="2025-02", window_end="2025-12")
        assert index.merge_candidates(other) == ()

    def test_disjoint_window_same_kind_is_not_merge_candidate(self) -> None:
        index = RiskFingerprintIndex()
        index.register(_fingerprint())
        disjoint = _fingerprint(window_start="2023-01", window_end="2023-08")
        assert index.merge_candidates(disjoint) == ()

    def test_merge_candidate_never_mutates_index(self) -> None:
        index = RiskFingerprintIndex()
        index.register(_fingerprint())
        index.merge_candidates(_fingerprint(window_start="2024-08", window_end="2025-05"))
        decision, _ = index.register(_fingerprint(window_start="2024-08", window_end="2025-05"))
        assert decision is DedupeDecision.NEW, "merge candidate 只是提议，不得吞掉注册"
