"""Calibration engine for token estimators.

Collects (estimated, actual) pairs, computes error metrics, learns
calibration parameters, and determines safe margins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CalibrationSample:
    estimated: int
    actual: int

    @property
    def absolute_error(self) -> int:
        return abs(self.estimated - self.actual)

    @property
    def signed_error(self) -> int:
        return self.estimated - self.actual

    @property
    def relative_error(self) -> float:
        if self.actual == 0:
            return float("inf") if self.estimated != 0 else 0.0
        return abs(self.estimated - self.actual) / self.actual


@dataclass
class ErrorMetrics:
    mae: float
    mape: float
    max_error: int
    mean_error: float
    stddev_error: float
    percentile_95: int
    percentile_99: int
    n: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mae": round(self.mae, 4),
            "mape": round(self.mape, 4),
            "max_error": self.max_error,
            "mean_error": round(self.mean_error, 4),
            "stddev_error": round(self.stddev_error, 4),
            "percentile_95": self.percentile_95,
            "percentile_99": self.percentile_99,
            "n": self.n,
        }


@dataclass
class CalibrationResult:
    scale: float
    bias: float
    margin: int
    pre_calib: ErrorMetrics
    post_calib: ErrorMetrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": round(self.scale, 6),
            "bias": round(self.bias, 4),
            "margin": self.margin,
            "pre_calib": self.pre_calib.to_dict(),
            "post_calib": self.post_calib.to_dict(),
        }


@dataclass
class Calibrator:
    samples: list[CalibrationSample] = field(default_factory=list)

    def add(self, estimated: int, actual: int) -> None:
        self.samples.append(CalibrationSample(estimated=estimated, actual=actual))

    def add_batch(self, pairs: list[tuple[int, int]]) -> None:
        for est, act in pairs:
            self.add(est, act)

    @property
    def n(self) -> int:
        return len(self.samples)

    def compute_errors(self) -> ErrorMetrics:
        if not self.samples:
            return ErrorMetrics(0, 0, 0, 0, 0, 0, 0, 0)

        errors = [s.absolute_error for s in self.samples]
        signed = [s.signed_error for s in self.samples]
        rel_errors = [s.relative_error for s in self.samples if s.relative_error != float("inf")]

        n = len(errors)
        mae = sum(errors) / n
        mape = (sum(rel_errors) / len(rel_errors) * 100) if rel_errors else 0.0
        max_err = max(errors)
        mean_err = sum(signed) / n
        variance = sum((e - mean_err) ** 2 for e in signed) / n
        stddev = math.sqrt(variance)

        sorted_abs = sorted(errors)
        p95_idx = min(math.ceil(0.95 * n) - 1, n - 1)
        p99_idx = min(math.ceil(0.99 * n) - 1, n - 1)

        return ErrorMetrics(
            mae=mae,
            mape=mape,
            max_error=max_err,
            mean_error=mean_err,
            stddev_error=stddev,
            percentile_95=sorted_abs[p95_idx],
            percentile_99=sorted_abs[p99_idx],
            n=n,
        )

    def learn_calibration(self) -> CalibrationResult:
        pre = self.compute_errors()

        if self.n < 2:
            return CalibrationResult(
                scale=1.0,
                bias=0.0,
                margin=pre.percentile_99,
                pre_calib=pre,
                post_calib=pre,
            )

        # least-squares: actual = scale * estimated + bias
        ests = [s.estimated for s in self.samples]
        acts = [s.actual for s in self.samples]

        mean_e = sum(ests) / self.n
        mean_a = sum(acts) / self.n

        cov = sum((e - mean_e) * (a - mean_a) for e, a in zip(ests, acts, strict=True)) / self.n
        var_e = sum((e - mean_e) ** 2 for e in ests) / self.n

        if var_e == 0:
            scale = 1.0
            bias = mean_a - mean_e
        else:
            scale = cov / var_e
            bias = mean_a - scale * mean_e

        # apply calibration and recompute errors
        calibrated = [max(1, math.ceil(scale * s.estimated + bias)) for s in self.samples]
        cal_samples = [
            CalibrationSample(estimated=c, actual=s.actual)
            for c, s in zip(calibrated, self.samples, strict=True)
        ]

        cal_errors = [s.absolute_error for s in cal_samples]
        cal_signed = [s.signed_error for s in cal_samples]
        cal_rel = [s.relative_error for s in cal_samples if s.relative_error != float("inf")]

        cal_n = len(cal_errors)
        cal_mae = sum(cal_errors) / cal_n
        cal_mape = (sum(cal_rel) / len(cal_rel) * 100) if cal_rel else 0.0
        cal_max = max(cal_errors)
        cal_mean = sum(cal_signed) / cal_n
        cal_var = sum((e - cal_mean) ** 2 for e in cal_signed) / cal_n
        cal_std = math.sqrt(cal_var)

        sorted_cal = sorted(cal_errors)
        cal_p95 = sorted_cal[min(math.ceil(0.95 * cal_n) - 1, cal_n - 1)]
        cal_p99 = sorted_cal[min(math.ceil(0.99 * cal_n) - 1, cal_n - 1)]

        post = ErrorMetrics(
            mae=cal_mae,
            mape=cal_mape,
            max_error=cal_max,
            mean_error=cal_mean,
            stddev_error=cal_std,
            percentile_95=cal_p95,
            percentile_99=cal_p99,
            n=cal_n,
        )

        return CalibrationResult(
            scale=scale,
            bias=bias,
            margin=cal_p99,
            pre_calib=pre,
            post_calib=post,
        )
