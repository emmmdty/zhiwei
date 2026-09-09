"""S9-T6 安全契约：默认遥测输出零正文、零哨兵、零 PII（no-secret scan）。

构造真实事件形状的合成载荷集（runtime/events.py 的 TaskCompleted/TaskFailed、
ModelActivity 输出、eval.run.sealed canonical payload、cost.reserved payload、
span attributes、structured log record），在正文位注入 canary 哨兵与 PII：

1. metadata_only_view 之后：正文键消失、canary/PII 不出现在任何输出；
2. scan_no_secret 对剥离后的默认输出：零 finding（扫描本身是回归探针）；
3. scan_no_secret 对原始（未剥离）载荷：finding 必须命中，且 finding 不携带原文。

哨兵=secret 扫描的「金丝雀」：一旦默认输出含 canary，说明有正文泄漏路径。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from zhiwei.telemetry.redaction import (
    RedactionCode,
    metadata_only_view,
    scan_no_secret,
)

CANARY = "sk-canary-9f2c1e7bdeadbeefcafebabe01234567"
PII_EMAIL = "alice.parker@example-corp.com"
PII_PATTERNS = (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",)

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _synthetic_payloads() -> dict[str, dict[str, object]]:
    """真实事件形状 + 正文位 canary/PII（形状来源见模块 docstring）。"""
    return {
        # TaskCompleted（runtime/events.py）：output_values 是正文位
        "task.completed": {
            "run_id": str(uuid4()),
            "event_id": str(uuid4()),
            "timestamp": _NOW.isoformat(),
            "task_id": "synthesize",
            "output_values": {"answer": f"result {CANARY} end", "email": PII_EMAIL},
        },
        # TaskFailed（runtime/events.py）：error 是自由文本，可能夹带内容
        "task.failed": {
            "run_id": str(uuid4()),
            "event_id": str(uuid4()),
            "timestamp": _NOW.isoformat(),
            "task_id": "retrieve",
            "error": f"tool blew up with {CANARY} and mailto {PII_EMAIL}",
        },
        # ModelActivity 输出（workflows/activities/model.py）
        "model.activity": {
            "task_id": "plan",
            "attempt_id": str(uuid4()),
            "status": "completed",
            "output_values": {"plan": f"{CANARY} step"},
            "routing_decision": {"endpoint": "e-1", "reason": "cheapest"},
            "weighted_tokens": 12.0,
        },
        # eval.run.sealed canonical payload（evals/runs.py）——本身全 metadata；
        # 篡改变体把正文走私进 payload，验证扫描能发现
        "eval.sealed": {
            "eval_run_id": str(uuid4()),
            "manifest_id": str(uuid4()),
            "seal_digest": "sha256:" + "a" * 64,
            "result": f"smuggled {CANARY}",
        },
        # cost.reserved canonical payload（telemetry/costs.py）
        "cost.reserved": {
            "reservation_id": str(uuid4()),
            "run_id": str(uuid4()),
            "amount_usd": "0.0200",
            "price_source": "provider-list-2026-09",
            "price_confidence": "exact",
            "messages": [{"role": "user", "content": f"{CANARY}"}],
        },
        # span attributes（telemetry/traces.py 形状）
        "span.attributes": {
            "run_id": str(uuid4()),
            "task_id": "retrieve",
            "span.name": "zhiwei.retrieval",
            "prompt": f"user asked {CANARY}",
            "tool_args": {"query": PII_EMAIL},
        },
    }


class TestDefaultTelemetryContainsNoBodies:
    def test_metadata_view_strips_every_body_key(self) -> None:
        for shape, payload in _synthetic_payloads().items():
            view = metadata_only_view(payload)
            for body_key in ("output_values", "error", "result", "messages", "prompt", "tool_args"):
                assert body_key not in view, f"{shape}: {body_key} survived default view"

    def test_metadata_view_output_has_no_canary_or_pii(self) -> None:
        for shape, payload in _synthetic_payloads().items():
            dumped = str(metadata_only_view(payload))
            assert CANARY not in dumped, f"{shape}: canary leaked into default view"
            assert PII_EMAIL not in dumped, f"{shape}: PII leaked into default view"

    def test_metadata_view_keeps_metadata_fields(self) -> None:
        view = metadata_only_view(_synthetic_payloads()["cost.reserved"])
        assert view["price_confidence"] == "exact"
        assert view["amount_usd"] == "0.0200"
        assert view["price_source"] == "provider-list-2026-09"


class TestNoSecretScanOnDefaultOutput:
    def test_stripped_views_produce_zero_findings(self) -> None:
        for shape, payload in _synthetic_payloads().items():
            view = metadata_only_view(payload)
            findings = scan_no_secret(
                payload=view, sentinels=(CANARY,), pii_patterns=PII_PATTERNS
            )
            assert findings == (), f"{shape}: stripped view still flagged: {findings}"

    def test_raw_payloads_are_flagged_at_the_right_code(self) -> None:
        for shape, payload in _synthetic_payloads().items():
            findings = scan_no_secret(
                payload=payload, sentinels=(CANARY,), pii_patterns=PII_PATTERNS
            )
            assert findings, f"{shape}: scan missed the planted sentinel"
            codes = {finding.code for finding in findings}
            assert codes <= {RedactionCode.SECRET_SENTINEL, RedactionCode.PII_SENTINEL}

    def test_findings_never_carry_original_values(self) -> None:
        for payload in _synthetic_payloads().values():
            findings = scan_no_secret(
                payload=payload, sentinels=(CANARY,), pii_patterns=PII_PATTERNS
            )
            for finding in findings:
                dumped = finding.model_dump_json()
                assert CANARY not in dumped
                assert PII_EMAIL not in dumped
