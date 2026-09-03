"""Local token estimator implementations.

Each estimator follows the protocol: estimate(text: str) -> int

Estimators are intentionally simple — the spike validates calibration
METHODOLOGY, not estimator quality.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Protocol


class TokenEstimator(Protocol):
    @property
    def name(self) -> str: ...

    def estimate(self, text: str) -> int: ...


# ---------------------------------------------------------------------------
# Simple estimators
# ---------------------------------------------------------------------------


class CharEstimator:
    """len(text) / 4 — rough heuristic."""

    @property
    def name(self) -> str:
        return "char_estimator"

    def estimate(self, text: str) -> int:
        return max(1, math.ceil(len(text) / 4))


class WordEstimator:
    """len(text.split()) * 1.3 — word-based."""

    @property
    def name(self) -> str:
        return "word_estimator"

    def estimate(self, text: str) -> int:
        return max(1, math.ceil(len(text.split()) * 1.3))


class StructureEstimator:
    """Content-type aware: JSON gets different factor than plain text."""

    @property
    def name(self) -> str:
        return "structure_estimator"

    def _is_json(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        try:
            json.loads(stripped)
            return True
        except (json.JSONDecodeError, ValueError):
            return False

    def estimate(self, text: str) -> int:
        if self._is_json(text):
            return max(1, math.ceil(len(text) / 3.5))
        if len(text) > 500:
            return max(1, math.ceil(len(text) / 2.5))
        return max(1, math.ceil(len(text) / 3.0))


# ---------------------------------------------------------------------------
# Calibrated estimator
# ---------------------------------------------------------------------------


@dataclass
class CalibratedEstimator:
    """Wraps a base estimator with learned bias + scale correction."""

    base: TokenEstimator
    scale: float = 1.0
    bias: float = 0.0

    @property
    def name(self) -> str:
        return f"calibrated_{self.base.name}"

    def estimate(self, text: str) -> int:
        raw = self.base.estimate(text)
        corrected = self.scale * raw + self.bias
        return max(1, math.ceil(corrected))

    def calibrate(self, scale: float, bias: float) -> None:
        self.scale = scale
        self.bias = bias

    def reset_calibration(self) -> None:
        self.scale = 1.0
        self.bias = 0.0


ALL_ESTIMATORS: list[TokenEstimator] = [
    CharEstimator(),
    WordEstimator(),
    StructureEstimator(),
]


def classify_provider_error(error_type: str) -> str:
    """Map provider error types to failure categories per ADR-002."""
    if error_type == "context_length_exceeded":
        return "context_refusal"
    return "provider_failure"
