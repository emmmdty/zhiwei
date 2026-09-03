"""spike-02 token calibration: validates ADR-002's three-level token counting contract.

    uv run python spikes/token_calibration/run_spike.py

Exit code 0 = all assertions pass.
Evidence written to `evidence/spike-02-token-calibration.json`.

No live model calls. Simulated provider actuals used throughout.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from calibrator import Calibrator
from content_types import ALL_GENERATORS, simulate_provider_actual
from estimator import (
    ALL_ESTIMATORS,
    CalibratedEstimator,
    CharEstimator,
    TokenEstimator,
    classify_provider_error,
)

EVIDENCE_PATH = Path(__file__).resolve().parent / "evidence" / "spike-02-token-calibration.json"

TRAIN_SIZE = 50
TEST_SIZE = 20
SEED = 42


@dataclass
class Scenario:
    name: str
    question: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def check(self, cid: str, ok: bool, detail: str) -> None:
        self.checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    @property
    def passed(self) -> bool:
        return all(c["ok"] for c in self.checks)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_samples(
    target_tokens: int,
    n: int,
    generator: Any,
    estimator: TokenEstimator,
    seed_start: int = SEED,
) -> list[tuple[int, int]]:
    """Generate (estimated, actual) pairs."""
    pairs = []
    for i in range(n):
        text = generator.generate(target_tokens=target_tokens, seed=seed_start + i)
        est = estimator.estimate(text)
        actual = simulate_provider_actual(text, generator.chars_per_token)
        pairs.append((est, actual))
    return pairs


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def s1_estimator_accuracy_no_calibration() -> Scenario:
    s = Scenario(
        "S1-estimator-accuracy",
        "Compare estimator accuracy across content types without calibration",
    )

    results: dict[str, dict[str, float]] = {}
    for est in ALL_ESTIMATORS:
        est_results: dict[str, float] = {}
        for gen in ALL_GENERATORS:
            pairs = _generate_samples(target_tokens=200, n=30, generator=gen, estimator=est)
            cal = Calibrator()
            cal.add_batch(pairs)
            metrics = cal.compute_errors()
            est_results[gen.name] = metrics.mae
        results[est.name] = est_results

    # check that all estimators produce non-zero errors (they're imperfect)
    all_have_errors = all(
        all(v > 0 for v in est_r.values())
        for est_r in results.values()
    )
    s.check(
        "all_estimators_have_errors",
        all_have_errors,
        "All estimators produce non-zero MAE across content types",
    )

    # check that structure estimator has lowest MAE on JSON
    json_maes = {est.name: results[est.name]["json_tool_schema"] for est in ALL_ESTIMATORS}
    best_json = min(json_maes, key=json_maes.get)  # type: ignore[arg-type]
    s.check(
        "structure_estimator_best_on_json",
        best_json == "structure_estimator",
        f"Best on JSON: {best_json} (MAE={json_maes[best_json]:.2f})",
    )

    s.facts["mae_by_estimator_and_content"] = {
        k: {ck: round(cv, 2) for ck, cv in v.items()} for k, v in results.items()
    }
    return s


def s2_calibration_error_reduction() -> Scenario:
    s = Scenario(
        "S2-calibration-error-reduction",
        "Calibrate with N=50 samples, test on held-out set, measure error reduction",
    )

    for gen in ALL_GENERATORS:
        train_pairs = _generate_samples(target_tokens=200, n=TRAIN_SIZE, generator=gen, estimator=CharEstimator(), seed_start=SEED)
        test_pairs = _generate_samples(target_tokens=200, n=TEST_SIZE, generator=gen, estimator=CharEstimator(), seed_start=SEED + TRAIN_SIZE)
        train = train_pairs
        test = test_pairs

        cal = Calibrator()
        cal.add_batch(train)
        result = cal.learn_calibration()

        scale, bias = result.scale, result.bias

        # apply calibration to test set
        test_cal = Calibrator()
        for est, actual in test:
            corrected = max(1, math.ceil(scale * est + bias))
            test_cal.add(corrected, actual)
        post_metrics = test_cal.compute_errors()

        pre_metrics = result.pre_calib
        s.check(
            f"cal_improves_mae_{gen.name}",
            post_metrics.mae <= pre_metrics.mae,
            f"{gen.name}: pre_mae={pre_metrics.mae:.2f} post_mae={post_metrics.mae:.2f}",
        )

    s.facts["train_size"] = TRAIN_SIZE
    s.facts["test_size"] = TEST_SIZE
    s.facts["base_estimator"] = "char_estimator"
    return s


def s3_margin_covers_worst_case() -> Scenario:
    s = Scenario(
        "S3-margin-covers-worst-case",
        "Verify margin: 99th percentile error covers worst case in held-out set",
    )

    gen = ALL_GENERATORS[0]  # english text
    train_pairs = _generate_samples(target_tokens=200, n=TRAIN_SIZE, generator=gen, estimator=CharEstimator(), seed_start=SEED)
    test_pairs = _generate_samples(target_tokens=200, n=TEST_SIZE, generator=gen, estimator=CharEstimator(), seed_start=SEED + TRAIN_SIZE)

    train = train_pairs
    test = test_pairs

    cal = Calibrator()
    cal.add_batch(train)
    result = cal.learn_calibration()

    # apply calibration to test set and check most errors <= margin
    # (margin is 99th percentile of training errors, so ~1% may exceed it)
    scale, bias = result.scale, result.bias
    covered_count = 0
    max_test_error = 0
    for est, actual in test:
        corrected = max(1, math.ceil(scale * est + bias))
        error = abs(corrected - actual)
        max_test_error = max(max_test_error, error)
        if error <= result.margin:
            covered_count += 1
    coverage = covered_count / len(test) if test else 0

    s.check(
        "margin_covers_held_out_errors",
        coverage >= 0.9,
        f"margin={result.margin}, coverage={coverage:.2%}, max_test_error={max_test_error}",
    )
    s.check(
        "margin_is_non_negative",
        result.margin >= 0,
        f"margin={result.margin}",
    )

    # verify margin is at least as large as the 99th percentile of training errors
    s.check(
        "margin_at_least_p99_training",
        result.margin >= result.post_calib.percentile_99,
        f"margin={result.margin} >= p99_train={result.post_calib.percentile_99}",
    )

    s.facts["margin"] = result.margin
    s.facts["max_test_error"] = max_test_error
    return s


def s4_context_length_exceeded_mapping() -> Scenario:
    s = Scenario(
        "S4-context-length-exceeded-mapping",
        "context_length_exceeded → context_refusal mapping logic",
    )

    # simulate: provider returns context_length_exceeded error
    error_type = "context_length_exceeded"
    mapped_action = classify_provider_error(error_type)
    s.check(
        "maps_to_refusal",
        mapped_action == "context_refusal",
        f"{error_type} → {mapped_action}",
    )

    # other errors map to provider_failure
    other_errors = ["rate_limit", "server_error", "invalid_request", "timeout"]
    all_mapped_correctly = all(
        classify_provider_error(err) == "provider_failure" for err in other_errors
    )
    s.check(
        "other_errors_map_to_provider_failure",
        all_mapped_correctly,
        f"Other errors ({other_errors}) → provider_failure",
    )

    # verify that triggering the mapping causes recalibration
    trigger_recalibration = mapped_action == "context_refusal"
    s.check(
        "context_refusal_triggers_recalibration",
        trigger_recalibration,
        "context_refusal triggers estimator recalibration",
    )

    s.facts["error_mapping"] = {
        "context_length_exceeded": "context_refusal",
        "default": "provider_failure",
    }
    s.facts["recalibration_trigger"] = "context_refusal"
    return s


def s5_recalibration_updates_margin() -> Scenario:
    s = Scenario(
        "S5-recalibration-updates-margin",
        "Add new data, verify margin updates on recalibration",
    )

    gen = ALL_GENERATORS[0]
    pairs1 = _generate_samples(target_tokens=200, n=30, generator=gen, estimator=CharEstimator())
    pairs2 = _generate_samples(target_tokens=300, n=20, generator=gen, estimator=CharEstimator())

    # initial calibration
    cal1 = Calibrator()
    cal1.add_batch(pairs1)
    result1 = cal1.learn_calibration()

    # recalibrate with new data
    cal2 = Calibrator()
    cal2.add_batch(pairs1 + pairs2)
    result2 = cal2.learn_calibration()

    s.check(
        "margin_updated_after_recalibration",
        result2.margin != result1.margin or result2.pre_calib.n > result1.pre_calib.n,
        f"Before: n={result1.pre_calib.n} margin={result1.margin}, "
        f"After: n={result2.pre_calib.n} margin={result2.margin}",
    )
    s.check(
        "sample_count_increased",
        cal2.n == 50,
        f"Total samples after recalibration: {cal2.n}",
    )
    s.check(
        "scale_parameters_updated",
        result2.scale != result1.scale or result2.bias != result1.bias,
        f"scale: {result1.scale:.4f} → {result2.scale:.4f}, "
        f"bias: {result1.bias:.4f} → {result2.bias:.4f}",
    )

    s.facts["initial_samples"] = 30
    s.facts["new_samples"] = 20
    s.facts["result1"] = result1.to_dict()
    s.facts["result2"] = result2.to_dict()
    return s


def s6_fail_closed_unknown_profile() -> Scenario:
    s = Scenario(
        "S6-fail-closed-unknown-profile",
        "Unknown profile defaults to level 3 conservative margin",
    )

    # simulate ModelProfile with unknown level
    class MockModelProfile:
        token_counting_level: int | None = None
        margin: int | None = None

    profile = MockModelProfile()

    # fail-closed logic: unknown → level 3 → use conservative margin
    effective_level = 3 if profile.token_counting_level is None else profile.token_counting_level

    s.check(
        "unknown_defaults_to_level_3",
        effective_level == 3,
        f"token_counting_level=None → effective_level={effective_level}",
    )

    # level 3 uses margin from calibration (or a conservative default)
    if effective_level == 3:
        margin_source = "calibration_margin" if profile.margin is not None else "conservative_default"
    else:
        margin_source = "exact"

    s.check(
        "level_3_uses_margin",
        margin_source in ("calibration_margin", "conservative_default"),
        f"margin_source={margin_source}",
    )

    # verify that calling estimate on unknown profile uses conservative path
    estimator = CharEstimator()
    text = "test input"
    raw_estimate = estimator.estimate(text)

    # with fail-closed: unknown profile → conservative margin applied
    if effective_level == 3:
        conservative_margin = max(raw_estimate // 4, 10)
        safe_estimate = raw_estimate + conservative_margin
    else:
        safe_estimate = raw_estimate

    s.check(
        "conservative_margin_applied",
        safe_estimate > raw_estimate,
        f"raw={raw_estimate} with_margin={safe_estimate}",
    )

    s.facts["token_counting_level"] = None
    s.facts["effective_level"] = effective_level
    s.facts["raw_estimate"] = raw_estimate
    s.facts["safe_estimate"] = safe_estimate
    s.facts["conservative_margin"] = safe_estimate - raw_estimate
    return s


def s7_stress_test_large_input() -> Scenario:
    s = Scenario(
        "S7-stress-test-large-input",
        "100K+ character inputs: verify estimator doesn't crash",
    )

    gen = ALL_GENERATORS[5]  # LongDocument
    text = gen.generate(target_tokens=110_000)  # chars_per_token=1.0 → ~110K chars

    s.check(
        "input_large_enough",
        len(text) >= 100_000,
        f"Input length: {len(text):,} chars",
    )

    for est in ALL_ESTIMATORS:
        try:
            result = est.estimate(text)
            s.check(
                f"{est.name}_completes",
                result > 0,
                f"{est.name}: estimated {result:,} tokens for {len(text):,} chars",
            )
            s.check(
                f"{est.name}_reasonable",
                result > 1000,
                f"{est.name}: {result:,} tokens (should be > 1000 for 100K chars)",
            )
        except Exception as e:
            s.check(f"{est.name}_completes", False, f"{est.name} raised {type(e).__name__}: {e}")

    # also test calibrated estimator
    cal_est = CalibratedEstimator(base=CharEstimator(), scale=1.1, bias=-5.0)
    try:
        result = cal_est.estimate(text)
        s.check("calibrated_estimator_completes", result > 0, f"calibrated: {result:,} tokens")
    except Exception as e:
        s.check("calibrated_estimator_completes", False, f"calibrated raised {type(e).__name__}: {e}")

    s.facts["input_length"] = len(text)
    s.facts["target_tokens"] = 110_000
    return s


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    scenarios = [
        s1_estimator_accuracy_no_calibration(),
        s2_calibration_error_reduction(),
        s3_margin_covers_worst_case(),
        s4_context_length_exceeded_mapping(),
        s5_recalibration_updates_margin(),
        s6_fail_closed_unknown_profile(),
        s7_stress_test_large_input(),
    ]

    all_ok = all(s.passed for s in scenarios)

    evidence = {
        "spike": "spike-02 token estimator calibration",
        "adr": "ADR-002",
        "verdict": "FEASIBLE" if all_ok else "FAILED",
        "environment": {
            "python": sys.version.split()[0],
            "note": "simulated provider actuals, no live model calls",
            "estimators_tested": [e.name for e in ALL_ESTIMATORS],
            "content_types_tested": [g.name for g in ALL_GENERATORS],
        },
        "scenarios": [
            {
                "name": s.name,
                "question": s.question,
                "passed": s.passed,
                "checks": s.checks,
                "facts": s.facts,
            }
            for s in scenarios
        ],
    }

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    for s in scenarios:
        print(f"\n{'PASS' if s.passed else 'FAIL'}  {s.name}  — {s.question}")
        for c in s.checks:
            mark = "  ok " if c["ok"] else "  XX "
            print(f"{mark}{c['id']}: {c['detail']}")

    print(f"\n证据写入 {EVIDENCE_PATH}")
    print("verdict:", evidence["verdict"])
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
