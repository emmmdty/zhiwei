"""S11-T6 负载 runner 契约（specs/s11 §5；docs/operations/capacity.md §2 冻结）。

冻结断言面（A 档契约）：

1. **workload 注册表**：四类固定 workload（ask / discover / sync-index / eval）
   全部经生产 Runtime 执行（fixture provider，无 live 模型）——不写评测专用旁路；
2. **报告契约**：每次运行产出 report（JSON 可序列化），必须包含：
   per-workload p50/p95（报告层分位聚合）、queue_wait p50/p95、error/terminal
   计数、recovery_time_ms、资源采样（CPU/mem，runner 侧 OS 采集）、environment、
   uncertainty（样本量+标准差）、SLO 免责声明（无测量不承诺，spec §8）；
3. **有界**：max_runs/concurrency 是硬上界（bounded fixture smoke 可进 CI）；
4. **cost-mode 字段**：报告声明 fixture 模式成本口径（无真实计费）。
"""

from __future__ import annotations

import os
import socket
from collections.abc import Generator
from pathlib import Path

import pytest

from zhiwei.operations.load import (
    LOAD_WORKLOADS,
    LoadRunReport,
    run_load,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PG = "postgresql+asyncpg://zhiwei_migrator:zhiwei-dev-pg-only@127.0.0.1:55433/zhiwei"

# RED 阶段修订（2026-09-08）：bounded smoke 需要可达 PG（生产 runtime 路径），
# 环境守卫沿袭 tests/contract/cli/test_runtime_cli.py 的 skip 模式——skip 不是
# 断言放宽，无 PG 的环境跑不了生产 runtime。


@pytest.fixture(autouse=True)
def _require_pg() -> Generator[None]:
    try:
        with socket.create_connection(("127.0.0.1", 55433), timeout=1):
            pass
    except OSError:
        pytest.skip("测试 PG (55433) 不可达：负载 smoke 需要 compose 栈（环境守卫）")
    old = os.environ.get("ZHIWEI_DATABASE_URL")
    os.environ["ZHIWEI_DATABASE_URL"] = COMPOSE_PG
    yield
    if old is None:
        os.environ.pop("ZHIWEI_DATABASE_URL", None)
    else:
        os.environ["ZHIWEI_DATABASE_URL"] = old


def test_workload_registry_covers_spec_matrix() -> None:
    """spec §5 四类 workload：并发 Ask、定时 Discover、sync/index、eval。"""
    assert {"ask", "discover", "sync-index", "eval"} <= set(LOAD_WORKLOADS)


def test_bounded_fixture_smoke_produces_full_report(tmp_path: Path) -> None:
    """有界 smoke：concurrency=2、max_runs=2 → 报告字段完整（CI 可跑的子集）。"""
    report = run_load(
        workloads=["ask"],
        concurrency=2,
        max_runs=2,
        profile="local_product",
        seal_dir=tmp_path,
    )
    assert isinstance(report, LoadRunReport)
    assert report.status == "passed"
    payload = report.model_dump()
    for field in ("p50_ms", "p95_ms", "queue_wait_p50_ms", "queue_wait_p95_ms",
                  "errors", "terminal_counts", "recovery_time_ms",
                  "resource_samples", "environment", "uncertainty",
                  "cost_mode", "slo_disclaimer"):
        assert field in payload, f"报告缺 {field}"
    assert report.slo_disclaimer, "无测量不承诺 SLO（spec §8）必须显式声明"
    assert report.terminal_counts.get("completed", 0) + report.terminal_counts.get(
        "failed", 0
    ) >= 2
    assert report.environment.get("python"), "报告必须记录环境（先报告再提 SLO）"
    assert report.uncertainty.get("sample_count", 0) >= 2
    assert report.cost_mode == "fixture_no_real_billing"


def test_percentiles_are_report_layer_not_instruments() -> None:
    """p50/p95 来自报告层对原始样本的聚合；MetricsFacade 词汇表不新增分位 instrument。"""
    from zhiwei.telemetry.metrics import INSTRUMENT_KINDS

    assert not any("p50" in name or "p95" in name for name in INSTRUMENT_KINDS)


def test_all_workloads_fixture_smoke(tmp_path: Path) -> None:
    """四类 workload 的最小有界运行（每类 1 次）。"""
    report = run_load(
        workloads=["ask", "discover", "sync-index", "eval"],
        concurrency=1,
        max_runs=1,
        profile="local_product",
        seal_dir=tmp_path,
    )
    assert report.status == "passed"
    assert set(report.per_workload) == {"ask", "discover", "sync-index", "eval"}


def test_discover_workload_survives_max_runs_gt_1(tmp_path: Path) -> None:
    """max_runs≥2：discover 租户准备幂等（F-P1-7 RED——循环内重复建租户即崩）。"""
    report = run_load(
        workloads=["discover"],
        concurrency=1,
        max_runs=2,
        profile="local_product",
        seal_dir=tmp_path,
    )
    assert report.status == "passed", report.per_workload
    assert report.per_workload["discover"].terminal_counts.get("completed", 0) == 2


def test_ramp_reports_bottleneck_observation(tmp_path: Path) -> None:
    """并发爬坡：报告层给出观测（不是承诺）——每档并发的 p95 变化可追溯。"""
    report = run_load(
        workloads=["ask"],
        concurrency=2,
        max_runs=4,
        profile="local_product",
        ramp=[1, 2],
        seal_dir=tmp_path,
    )
    assert report.ramp_observations, "爬坡档位必须逐档记录"
    assert {obs.concurrency for obs in report.ramp_observations} == {1, 2}
