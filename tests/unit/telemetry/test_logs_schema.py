"""S9-T6 RED：结构化日志 schema 契约（B 档实现级测试）。

specs/s9 §6：日志只含 metadata/digest（ids、machine code、sequence、digest 字段），
正文（prompt/result/completion）默认剥离；emit 输出 canonical JSON，正文键不存在。
"""

from __future__ import annotations

import json
import logging

from zhiwei.telemetry.logs import StructuredLogRecord, build_log_record, emit_log_record

_CANARY = "sk-canary-9f2c1e7bdeadbeefcafebabe01234567"


class TestStructuredLogRecord:
    def test_schema_fields_are_metadata_only(self) -> None:
        record = build_log_record(
            run_id="r-1",
            task_id="t-1",
            event_type="task.completed",
            sequence_no=7,
            event_digest="sha256:" + "0" * 64,
            code="TOOL_ERROR",
        )
        assert isinstance(record, StructuredLogRecord)
        assert record.run_id == "r-1"
        assert record.task_id == "t-1"
        assert record.event_type == "task.completed"
        assert record.sequence_no == 7
        assert record.code == "TOOL_ERROR"

    def test_body_fields_are_stripped(self) -> None:
        # 误传正文键：构建期剥掉，而不是留给调用方自律。
        record = build_log_record(
            run_id="r-1",
            prompt=_CANARY,  # type: ignore[call-arg]
            result={"answer": "42"},  # type: ignore[call-arg]
        )
        assert "prompt" not in record.model_dump()
        assert "result" not in record.model_dump()

    def test_emit_is_canonical_json_without_bodies(self, caplog) -> None:
        record = build_log_record(
            run_id="r-1", event_type="model.call", event_digest="sha256:" + "1" * 64
        )
        with caplog.at_level(logging.INFO, logger="zhiwei.telemetry"):
            emit_log_record(record)
        assert len(caplog.records) == 1
        payload = json.loads(caplog.records[0].message)
        assert payload["run_id"] == "r-1"
        assert "prompt" not in payload
        assert "result" not in payload
        assert _CANARY not in caplog.text
