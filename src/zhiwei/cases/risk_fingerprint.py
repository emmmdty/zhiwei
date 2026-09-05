"""typed RiskFingerprint 与生产去重（spec s8 §4，docs/RISK_EVAL.md §7）。

生产去重用 typed RiskFingerprint：同一次发现的重复触发（refresh/retry）识别为
DUPLICATE，不得复制 hypothesis（spec §6「刷新/重试不会复制 hypothesis/case/action」）。
semantic similarity 只提出 merge candidate——合并必须由人决定，绝不自动执行。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from zhiwei.contracts.canonical import canonical_json, digest, digest_bytes
from zhiwei.evidence.patterns.numeric import MONTH_CALENDAR, window_iou


class DedupeDecision(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"


class RiskFingerprint(BaseModel):
    """可解释的 typed 去重键：kind/metric/entity/window/detector version 全部显式。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    metric: str
    entity_dim: str
    entity_value: str
    window_start: str
    window_end: str
    detector_version: str

    def value(self) -> str:
        """内容寻址的去重键（确定性，含 detector version——换版不算同一次发现）。"""
        return digest(
            {
                "kind": self.kind,
                "metric": self.metric,
                "entity_dim": self.entity_dim,
                "entity_value": self.entity_value,
                "window_start": self.window_start,
                "window_end": self.window_end,
                "detector_version": self.detector_version,
            }
        )


class MergeCandidate(BaseModel):
    """语义相似合并提议：只提议、不执行——生产去重永不因 similarity 自动合并。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fingerprint: RiskFingerprint
    matched_with: RiskFingerprint
    window_iou: float
    similarity_reason: str


_SAME_KIND_METRIC_OVERLAP = "same_kind_metric_overlapping_window"


class RiskFingerprintIndex:
    """注册表：register 判 NEW/DUPLICATE；merge_candidates 只读提议。"""

    def __init__(self) -> None:
        self._registered: dict[str, RiskFingerprint] = {}

    def register(self, fingerprint: RiskFingerprint) -> tuple[DedupeDecision, RiskFingerprint | None]:
        existing = self._registered.get(fingerprint.value())
        if existing is not None:
            return DedupeDecision.DUPLICATE, existing
        self._registered[fingerprint.value()] = fingerprint
        return DedupeDecision.NEW, None

    def merge_candidates(self, fingerprint: RiskFingerprint) -> tuple[MergeCandidate, ...]:
        """同 kind+metric 且窗口重叠（IoU>0）的已注册项：只提议合并，不改注册表。"""
        candidates: list[MergeCandidate] = []
        for existing in self._registered.values():
            if existing.value() == fingerprint.value():
                continue
            if existing.kind != fingerprint.kind or existing.metric != fingerprint.metric:
                continue
            iou = window_iou(
                existing.window_start,
                existing.window_end,
                fingerprint.window_start,
                fingerprint.window_end,
                MONTH_CALENDAR,
            )
            if iou <= 0:
                continue
            candidates.append(
                MergeCandidate(
                    fingerprint=fingerprint,
                    matched_with=existing,
                    window_iou=iou,
                    similarity_reason=_SAME_KIND_METRIC_OVERLAP,
                )
            )
        return tuple(candidates)

    def registered(self) -> tuple[RiskFingerprint, ...]:
        return tuple(self._registered.values())


def canonical_fingerprints(fingerprints: tuple[RiskFingerprint, ...]) -> str:
    """指纹集合的 canonical JSON digest（供 artifact 密封与确定性断言）。"""
    return digest_bytes(canonical_json(sorted(fp.value() for fp in fingerprints)))
