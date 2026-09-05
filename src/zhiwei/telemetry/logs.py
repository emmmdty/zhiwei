"""S9-T6：结构化日志 schema——metadata/digest only（specs/s9 §6）。

字段面与 redaction.DEFAULT_BODY_KEYS 共享同一剥离纪律：构建期就剥掉正文键
（而不是「约定调用方别传」）；emit 输出 canonical JSON，方便 collector 侧按
字段路由而不解析自由文本。dashboard 不从日志文本猜状态——机器状态走
telemetry.failures 的 machine code，日志只承载可审计的元数据轨迹。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.telemetry.redaction import metadata_only_view

_LOGGER_NAME = "zhiwei.telemetry"


class StructuredLogRecord(BaseModel):
    """metadata-only 日志载荷；新增字段必须是 metadata（ids/枚举/digest）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    event_type: str | None = None
    sequence_no: int | None = Field(default=None, ge=0)
    event_digest: str | None = None
    code: str | None = None


def build_log_record(**fields: Any) -> StructuredLogRecord:
    """构建日志记录：正文键（prompt/result/…）在构建期剥离，多余键拒绝。

    剥离先于 extra=forbid 校验：误传正文键是被预期的操作错误，应表现为「字段
    被丢弃」而不是让调用方学会绕过；未知非正文键仍拒绝（fail closed）。
    """
    return StructuredLogRecord.model_validate(metadata_only_view(fields))


def emit_log_record(record: StructuredLogRecord, *, logger: logging.Logger | None = None) -> None:
    """以 canonical JSON 单行输出（collector 按字段路由，不解析自由文本）。"""
    target = logger or logging.getLogger(_LOGGER_NAME)
    payload = json.dumps(record.model_dump(), separators=(",", ":"), sort_keys=True)
    target.info(payload)
