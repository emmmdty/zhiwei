"""S9-T6：metric instrument 常量与 MetricsFacade（specs/s9 §6）。

与 traces.py 同一 metadata-only 纪律：属性经 metric_attributes（body key 剥离 +
非标量降为 canonical JSON）后才进 instrument。默认 no-op：未显式传入 meter provider
时 Facade 走全局 provider（api 默认 NoOp）——测试/未部署环境零副作用。

instrument 形态封闭（counter/histogram）：指标词汇是契约，新增指标先扩常量并
声明形态，不允许运行期临时造指标名。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import Meter, MeterProvider

from zhiwei.contracts.canonical import canonical_json
from zhiwei.telemetry.redaction import metadata_only_view

_METER_INSTRUMENTATION = "zhiwei.telemetry"


class InstrumentKind(StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"


METRIC_RUNS_STARTED = "zhiwei.run.started"
METRIC_RUNS_COMPLETED = "zhiwei.run.completed"
METRIC_RUNS_FAILED = "zhiwei.run.failed"
METRIC_TASK_DURATION = "zhiwei.task.duration"
METRIC_TOKEN_WEIGHTED = "zhiwei.model.tokens.weighted"
METRIC_COST_VARIANCE = "zhiwei.cost.variance"

INSTRUMENT_KINDS: dict[str, InstrumentKind] = {
    METRIC_RUNS_STARTED: InstrumentKind.COUNTER,
    METRIC_RUNS_COMPLETED: InstrumentKind.COUNTER,
    METRIC_RUNS_FAILED: InstrumentKind.COUNTER,
    METRIC_TASK_DURATION: InstrumentKind.HISTOGRAM,
    METRIC_TOKEN_WEIGHTED: InstrumentKind.HISTOGRAM,
    METRIC_COST_VARIANCE: InstrumentKind.HISTOGRAM,
}

_SCALAR_TYPES = (bool, int, float, str)


def metric_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """metadata-only 的 metric 属性视图（与 span 属性同一剥离规则）。"""
    stripped = metadata_only_view(attributes or {})
    return {
        key: value if isinstance(value, _SCALAR_TYPES) else canonical_json(value).decode()
        for key, value in stripped.items()
    }


@dataclass(frozen=True)
class _Instrument:
    kind: InstrumentKind
    handle: Any


class MetricsFacade:
    """显式 opt-in 的 metric 门面；opt-in 前一切 add/record 是 NoOp。"""

    def __init__(self, meter_provider: MeterProvider | None = None) -> None:
        # provider=None → 全局 provider（api 默认 NoOp）；显式传入即 opt-in。
        self._meter: Meter = (meter_provider or metrics.get_meter_provider()).get_meter(
            _METER_INSTRUMENTATION
        )
        self._instruments: dict[str, _Instrument] = {}

    def increment(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        instrument = self._instrument(name)
        if instrument.kind is not InstrumentKind.COUNTER:
            raise ValueError(f"{name!r} is not a counter")
        instrument.handle.add(1, metric_attributes(attributes))

    def record(
        self, name: str, value: float, attributes: Mapping[str, Any] | None = None
    ) -> None:
        instrument = self._instrument(name)
        if instrument.kind is not InstrumentKind.HISTOGRAM:
            raise ValueError(f"{name!r} is not a histogram")
        instrument.handle.record(value, metric_attributes(attributes))

    def _instrument(self, name: str) -> _Instrument:
        # 形态由常量表决定，未知指标名拒绝（fail closed，不是「先加上再说」）。
        kind = INSTRUMENT_KINDS.get(name)
        if kind is None:
            raise ValueError(f"unknown metric: {name!r}")
        instrument = self._instruments.get(name)
        if instrument is None:
            handle = (
                self._meter.create_counter(name)
                if kind is InstrumentKind.COUNTER
                else self._meter.create_histogram(name)
            )
            instrument = _Instrument(kind=kind, handle=handle)
            self._instruments[name] = instrument
        return instrument
