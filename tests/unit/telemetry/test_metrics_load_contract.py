"""S11-T6 指标契约扩展（plan 2026-09-08 修订 F-R8-06 的 RED 前置）。

冻结断言面（A 档契约）：

1. 负载测量的 instrument 先扩封闭词汇表并声明形态——禁止 runner 运行期临时造名；
2. 新增词汇（形态全部显式）：
   - zhiwei.load.latency      histogram（每请求端到端时延，ms）
   - zhiwei.load.queue_wait   histogram（提交→开始执行的排队时延，ms）
   - zhiwei.load.recovery     histogram（故障/重启后恢复耗时，ms）
   - zhiwei.load.terminal     counter（终态计数，含 outcome 维度）
   - zhiwei.load.errors       counter（错误计数，含 error kind 维度）
3. p50/p95 属报告层聚合（对原始样本分位），不是新 instrument——histogram 把
   原始值交给后端，分位在 load runner 报告层计算；
4. CPU/mem/IO 采样是 runner 侧 OS 采集（/proc + resource），不进 MetricsFacade
   ——形态写进 docs/operations/capacity.md。
"""

from __future__ import annotations

import pytest

from zhiwei.telemetry.metrics import (
    INSTRUMENT_KINDS,
    METRIC_LOAD_ERRORS,
    METRIC_LOAD_LATENCY,
    METRIC_LOAD_QUEUE_WAIT,
    METRIC_LOAD_RECOVERY,
    METRIC_LOAD_TERMINAL,
    InstrumentKind,
    MetricsFacade,
)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        (METRIC_LOAD_LATENCY, InstrumentKind.HISTOGRAM),
        (METRIC_LOAD_QUEUE_WAIT, InstrumentKind.HISTOGRAM),
        (METRIC_LOAD_RECOVERY, InstrumentKind.HISTOGRAM),
        (METRIC_LOAD_TERMINAL, InstrumentKind.COUNTER),
        (METRIC_LOAD_ERRORS, InstrumentKind.COUNTER),
    ],
)
def test_load_instruments_declared_in_closed_vocabulary(name: str, kind: InstrumentKind) -> None:
    assert INSTRUMENT_KINDS.get(name) is kind, f"{name} 未声明或形态不符"


def test_facade_rejects_undeclared_metric_names() -> None:
    """封闭词汇表纪律沿用：未登记的名字一律拒绝（不因 S11 放宽）。"""
    facade = MetricsFacade()
    with pytest.raises(ValueError, match="unknown"):
        facade.increment("zhiwei.load.made_up_metric")


def test_existing_vocabulary_unchanged() -> None:
    """S9 六个既有 instrument 不得漂移。"""
    for name in (
        "zhiwei.run.started",
        "zhiwei.run.completed",
        "zhiwei.run.failed",
        "zhiwei.task.duration",
        "zhiwei.model.tokens.weighted",
        "zhiwei.cost.variance",
    ):
        assert name in INSTRUMENT_KINDS
