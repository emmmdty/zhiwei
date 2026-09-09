"""S8：`zhiwei risk generate` CLI 契约（specs/s8 §8 Gate，ADR-013 反例 5）。

fail closed：未知 suite / 冻结资产 digest 不符 → 明确错误退出（exit 非零、无 traceback）。
--check 模式与冻结 ground truth（植入清单）比对，输出分开 D0-D6 的口径摘要。
"""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

import zhiwei.cli.risk as risk_cli
from zhiwei.cli.main import app
from zhiwei.evals import risk_suites

runner = CliRunner()
TRACEBACK_MARKER = "Traceback (most recent call last)"


class TestRiskGenerateContract:
    def test_generate_outputs_json_summary(self) -> None:
        result = runner.invoke(app, ["risk", "generate", "--suite", "numeric-risk-v1"])
        assert result.exit_code == 0, result.output
        assert TRACEBACK_MARKER not in result.output
        import json

        payload = json.loads(result.output)
        assert payload["suite"] == "numeric-risk-v1"
        assert payload["asset_digest"].startswith("sha256:")
        assert payload["counts"]["findings"] > 0
        assert payload["falsification"]["falsification_coverage"] >= 0

    def test_check_outputs_separated_d0_d6_and_passes(self) -> None:
        result = runner.invoke(
            app, ["risk", "generate", "--suite", "numeric-risk-v1", "--check"]
        )
        assert result.exit_code == 0, result.output
        import json

        payload = json.loads(result.output)
        assert payload["check"] is True
        assert set(payload["layers"]) == {"D0", "D1", "D2", "D3", "D4", "D5", "D6"}
        assert payload["passed"] is True
        assert payload["score"]["recall"]["overall"] >= 0.7
        assert payload["score"]["recall"]["by_declared_difficulty"]["easy"] == 1.0

    def test_unknown_suite_fails_closed(self) -> None:
        result = runner.invoke(app, ["risk", "generate", "--suite", "numeric-risk-v2"])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "未知" in result.output

    def test_digest_mismatch_fails_closed(self, monkeypatch: Any) -> None:
        """冻结资产被篡改 → 明确错误退出，不得继续生成。"""

        def _broken_verify(root: Any) -> dict[str, str]:
            raise risk_cli.AssetDigestError("asset digest mismatch: evals/risk/csv/fact_revenue.csv")

        # _verify_asset_checksums 随 suite 基建迁至 evals 层——patch 目标跟随代码位置
        monkeypatch.setattr(risk_suites, "_verify_asset_checksums", _broken_verify)
        result = runner.invoke(app, ["risk", "generate", "--suite", "numeric-risk-v1"])
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
        assert "digest" in result.output

    def test_check_failure_exits_nonzero(self, monkeypatch: Any) -> None:
        """判据不满足 → exit 非零（一致性不足的机器可读信号）。"""
        monkeypatch.setattr(
            risk_cli,
            "score_against_manifest",
            lambda *a, **k: {
                "planted_count": 14,
                "matched_count": 0,
                "recall": {"overall": 0.0, "by_declared_difficulty": {"easy": 0.0, "medium": 0.0, "hard": 0.0}},
                "precision": 0.0,
                "distractor_fp_rate": 1.0,
                "evidence_validity": 0.0,
                "matched": [],
                "missed": [],
                "distractor_fps": [],
            },
        )
        result = runner.invoke(
            app, ["risk", "generate", "--suite", "numeric-risk-v1", "--check"]
        )
        assert result.exit_code != 0
        import json

        payload = json.loads(result.output)
        assert payload["passed"] is False
        assert payload["criteria_failures"]

    def test_check_on_blind_suite_is_rejected(self) -> None:
        """--check 的 ground truth 比对只对有冻结植入清单的 suite 有定义。"""
        result = runner.invoke(
            app, ["risk", "generate", "--suite", "discover-blind-v1", "--check"]
        )
        assert result.exit_code != 0
        assert TRACEBACK_MARKER not in result.output
