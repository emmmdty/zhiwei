# 容量与负载（specs/s11 §5；Task 6）

> 状态：测量口径冻结——执行入口 `zhiwei ops load-run`（码表 docs/API.md §12.1）。
> 本文档只声明测量方法；**不含任何未测量的 SLO/容量承诺**（spec §8）。

## 1. 固定 workload（生产 Runtime 路径，fixture provider）

| workload | 生产路径 | fixture 数据 |
| --- | --- | --- |
| `ask` | `build_ask_environment` + `AskRuntimeExecutor`（生产命令路径 → Temporal dev → PG 真相） | ASK_V1_UNITS 契约场景 |
| `eval` | 同执行器路径（S9 suite 生产绑定形态），eval 标签分账 | ASK_V1_UNITS 轮转 |
| `discover` | `ProgramManager` + `DiscoveryTriggerService.fire`（生产触发路径 → outbox StartRun）→ dispatcher poll 至终态 | 线性 Fixture 图（契约 registry） |
| `sync-index` | `OpenSearchPort.kill_and_rebuild` + `hybrid_search`（生产索引 port） | 20 篇确定性文档 |

## 2. 测量口径（F-R8-06 冻结；对抗审查修订 2026-09-08）

- **时延**：per-request 原始样本 → `MetricsFacade` histogram
  （`zhiwei.load.latency` / `zhiwei.load.recovery`，封闭词汇表：
  src/zhiwei/telemetry/metrics.py）；报告层对原始样本做 p50/p95（最近秩法）
  ——分位不是 instrument。
- **排队时延（queue_wait）只报告真实测得的样本**：`discover` workload 测
  outbox 行 `claimed_at - created_at`；ask/eval/sync-index **不声称排队测量**
  （执行器不暴露 claim 时点，不伪造样本）。报告级 queue_wait 分位只聚合
  测得的样本；零样本时分位为 0 且不构成声明。
- **终态 / 错误**：`zhiwei.load.terminal` / `zhiwei.load.errors` counter。
- **CPU / mem / IO**：runner 侧 OS 采集（`resource.getrusage` + `/proc/self/status`
  VmRSS + `/proc/self/io` rchar/wchar），**不是 SDK 指标**；口径 = 负载进程自身，
  不含被测组件进程。
- **并发语义**：ask/eval/sync-index 经信号量真实执行 `--concurrency` 档位；
  discover 当前串行执行（触发路径按 run 逐个 fire）——ramp 的逐档观测只对
  前三类 workload 构成并发差异，discover 档位间无执行差异（如实口径）。
- **cost-mode**：fixture 模式声明 `fixture_no_real_billing`（无真实计费）。
- **不确定性**：报告记录样本量与延迟总体标准差；先报告再提 SLO。

## 3. 收敛判据与爬坡

- `--concurrency` 固定档位；`--ramp 1,2,4,…` 逐档运行并记录每档 p95 与吞吐
  （`ramp_observations`）——瓶颈观测 = p95 随并发档位的拐点，报告层只记录事实。
- 单机 CPU-only 边界：本 runner 的全部结论仅适用于单机 fixture 形态；
  跨节点 HA/长期运行行为不在测量范围（spec §8）。

## 4. 运行环境前提

- 需要可达 PG（`ZHIWEI_DATABASE_URL`；不可达 = 退出码 3，显式登记口径）；
- Temporal dev server 由 SDK 启动（E-R1 例外在册：compose Temporal digest pin
  已交付，SDK 下载版本对齐属复执行时点事项）；
- live 模型调用：永不（fixture provider；S11 §6 live 边界不变）。
