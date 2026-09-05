"""S9 冻结契约：telemetry 默认 metadata-only 与 no-secret/PII 扫描（A 档，S9-T6）。

默认遥测只含 metadata/digest，正文（prompt/result/completion）按 policy 显式开启；
canary 哨兵与 PII 模式命中必须产出 finding，且 finding 本身不携带原文。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zhiwei.telemetry.redaction import (
    RedactionCode,
    RedactionFinding,
    metadata_only_view,
    scan_no_secret,
)

CANARY = "sk-canary-9f2c1e7bdeadbeefcafebabe01234567"

PII_PATTERNS = (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",)


class TestNoSecretScan:
    def test_canary_in_nested_payload_detected(self) -> None:
        payload = {
            "run_id": "r-1",
            "attributes": {"model.output": f"answer with {CANARY} inside"},
        }
        findings = scan_no_secret(payload=payload, sentinels=(CANARY,))
        assert findings
        assert all(f.code == RedactionCode.SECRET_SENTINEL for f in findings)

    def test_clean_payload_has_no_findings(self) -> None:
        payload = {"run_id": "r-1", "tokens": 42}
        assert scan_no_secret(payload=payload, sentinels=(CANARY,)) == ()

    def test_finding_does_not_leak_original_value(self) -> None:
        payload = {"attributes": {"prompt.body": CANARY}}
        findings = scan_no_secret(payload=payload, sentinels=(CANARY,))
        assert findings
        finding: RedactionFinding = findings[0]
        # finding 只报告位置，不复制原文——扫描报告本身不得成为泄露面。
        assert CANARY not in finding.model_dump_json()

    def test_finding_is_immutable(self) -> None:
        finding = RedactionFinding(
            code=RedactionCode.SECRET_SENTINEL,
            field="attributes.prompt.body",
        )
        with pytest.raises(ValidationError):
            finding.field = "other"  # type: ignore[misc]


class TestPIIScan:
    def test_email_detected(self) -> None:
        payload = {"note": "contact alice@example.com for access"}
        findings = scan_no_secret(
            payload=payload, sentinels=(), pii_patterns=PII_PATTERNS
        )
        assert findings
        assert findings[0].code == RedactionCode.PII_SENTINEL


class TestMetadataOnlyView:
    def test_body_keys_stripped_by_default(self) -> None:
        view = metadata_only_view(
            {
                "run_id": "r-1",
                "prompt": "the prompt text",
                "messages": [{"role": "user", "content": "secret"}],
                "result": {"answer": "42"},
                "completion": "answer",
                "tool_args": {"path": "/etc/passwd"},
            }
        )
        assert view == {"run_id": "r-1"}

    def test_opt_in_body_key_requires_policy(self) -> None:
        # 正文采集必须显式声明 policy key 才保留（fail closed 的白名单式放行）。
        view = metadata_only_view(
            {"run_id": "r-1", "prompt": "text"},
            policy_enabled_bodies=("prompt",),
        )
        assert view == {"run_id": "r-1", "prompt": "text"}

    def test_metadata_keys_survive(self) -> None:
        view = metadata_only_view(
            {
                "run_id": "r-1",
                "task_id": "t-1",
                "span.name": "model.call",
                "prompt": "x",
            }
        )
        assert view == {
            "run_id": "r-1",
            "task_id": "t-1",
            "span.name": "model.call",
        }
