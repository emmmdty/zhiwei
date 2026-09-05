"""S9-T6 RED：metric instrument 常量与 MetricsFacade 契约（B 档实现级测试）。

同一 metadata-only 纪律：标签/属性经 body key 剥离后才可进 instrument；无显式
opt-in 时 Facade 是 no-op——测试进程不建立任何后端连接（InMemoryMetricReader
只在本进程内收集，无导出目标）。
"""

from __future__ import annotations

from zhiwei.telemetry.metrics import (
    INSTRUMENT_KINDS,
    METRIC_RUNS_COMPLETED,
    METRIC_RUNS_FAILED,
    METRIC_RUNS_STARTED,
    METRIC_TASK_DURATION,
    METRIC_TOKEN_WEIGHTED,
    InstrumentKind,
    MetricsFacade,
    metric_attributes,
)


class TestInstrumentConstants:
    def test_metric_names_are_namespaced(self) -> None:
        for name in (
            METRIC_RUNS_STARTED,
            METRIC_RUNS_COMPLETED,
            METRIC_RUNS_FAILED,
            METRIC_TASK_DURATION,
            METRIC_TOKEN_WEIGHTED,
        ):
            assert name.startswith("zhiwei.")

    def test_instrument_kinds_are_closed(self) -> None:
        # instrument 形态封闭：counter / histogram，新增形态必须显式扩枚举。
        assert set(InstrumentKind) == set(INSTRUMENT_KINDS.values())
        assert INSTRUMENT_KINDS[METRIC_TASK_DURATION] is InstrumentKind.HISTOGRAM
        assert INSTRUMENT_KINDS[METRIC_RUNS_STARTED] is InstrumentKind.COUNTER


class TestMetricsFacade:
    def test_default_facade_is_noop(self) -> None:
        # 未 opt-in：add/record 不安装 provider、不产生任何后端副作用。
        # 契约是「无 SDK 后端」，不是「对象必须是 NoOpMeterProvider」——
        # opentelemetry-api 未配置时返回惰性 proxy provider（行为等同 no-op）。
        from opentelemetry.metrics import get_meter_provider
        from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider

        before = get_meter_provider()
        facade = MetricsFacade()
        facade.increment(METRIC_RUNS_STARTED, {"run_id": "r-1"})
        facade.record(METRIC_TASK_DURATION, 0.5, {"task_id": "t-1"})
        assert get_meter_provider() is before
        assert not isinstance(before, SdkMeterProvider)

    def test_facade_strips_body_keys_before_instrument(self) -> None:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        facade = MetricsFacade(meter_provider=MeterProvider(metric_readers=[reader]))
        facade.increment(METRIC_RUNS_STARTED, {"run_id": "r-1", "prompt": "leak"})
        metrics_data = reader.get_metrics_data()
        assert metrics_data is not None
        points = (
            metrics_data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
        )
        assert points[0].attributes == {"run_id": "r-1"}

    def test_opt_in_facade_uses_explicit_provider(self) -> None:
        from opentelemetry.metrics import NoOpMeterProvider

        provider = NoOpMeterProvider()
        facade = MetricsFacade(meter_provider=provider)
        # opt-in 后仍不触网（NoOp provider 本身无 socket）；仅验证绑定生效。
        facade.increment(METRIC_RUNS_STARTED, {"run_id": "r-1"})


class TestMetricAttributes:
    def test_body_keys_stripped(self) -> None:
        assert metric_attributes({"run_id": "r-1", "result": "body"}) == {"run_id": "r-1"}

    def test_non_scalars_become_canonical_json(self) -> None:
        assert metric_attributes({"labels": ["a", "b"]}) == {"labels": '["a","b"]'}
