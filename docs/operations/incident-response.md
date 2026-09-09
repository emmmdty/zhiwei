# 事故响应（specs/s11 §5；Task 5）

> 状态：机制冻结——场景注册表、区别性终态、恢复判定与密封语义以
> `src/zhiwei/operations/faults.py` 与 `tests/fault/` 契约为准。
> 执行命令：`zhiwei ops fault-run`（码表 docs/API.md §12.1：0/1/2/3）。

## 1. 故障场景矩阵

spec §5 依赖 × 故障类别（registry 以 scenario_id 为键，依赖覆盖以
`Scenario.dependency` 断言）：

| 依赖 | kill/restart | partition | slow | duplicate | corrupt | 区别性终态 |
| --- | --- | --- | --- | --- | --- | --- |
| postgres | pg_restart | partition_postgres_api | slow_postgres_probe | — | pg_corrupt_probe | recover / degrade |
| temporal | temporal_restart | — | — | — | — | recover |
| redis | redis_restart | — | — | — | — | recover |
| opensearch | opensearch_restart | — | — | — | — | recover |
| object_store | object_store_restart | — | — | — | object_corruption_fail_closed | recover / fail_closed |
| opa | opa_down_fail_closed | — | — | — | — | **fail_closed** |
| otel_collector | otel_collector_restart | — | — | — | — | recover |
| reference_tool | reference_tool_restart | — | slow_provider_timeout | duplicate_webhook_fencing | — | recover / effect_unknown |
| dispatcher | dispatcher_restart | — | — | duplicate_command_fencing | — | recover |

## 2. 必测崩溃窗口（R5 清单，plan 2026-09-08 修订）

- **#2 commit 后、dispatch 前进程死亡**：`crash_window_2_pending_over_deadline`。
  pending 且 `dispatch_deadline` 已过的命令必须被 dispatcher 发现查询重新命中
  （T3 的 dispatch_deadline 是判定锚）；恢复语义 = 租约重claim + 重 poll。
- **#11 CAN 过渡间隙 cancel/pause 信号丢失**：`crash_window_11_can_intent_recheck`。
  workflow 在 continue_as_new 之前必须执行 `run_intent_recheck` PG 意图回查
  （outbox pending 的 cancel_run/pause_run 命令是已落账真相）；有 cancel 意图 →
  本地记 cancelled 终态，不得 CAN；有 pause 意图 → 置暂停态留在本 run。

两个窗口都以 fixture backend 确定性执行（compose 时序不可固定重现）。

## 3. 区别性终态（注册期声明，运行期验证）

- **fail_closed**：OPA 不可达 → PEP authorize 走 deny 路径（永不抛、默认拒绝）；
  对象损坏 → digest 校验拦截（ArtifactVerificationError）。
- **degrade**：Redis 丢失 → SSE 降级 PG 轮询（可选加速通道语义）；搜索丢失 →
  in-proc 索引承载，search 非事实源。
- **effect_unknown**：外部效果未知是独立终态，不与 failed 混淆
  （ToolActivityOutput.status/receipt_effect 区分）。
- **recover**：kill/restart 类 → 服务恢复 healthy；超时上界 120s。

## 4. 密封（--sealed）

每个场景产出 seal JSON：`raw_events`（过程事件）、`environment`（python/platform/
时间戳）、`image_digests`（compose config --images）、`recovery_time_ms`、
`seal_digest`（sha256，sort_keys）。seal 缺任一字段 = 场景无效。

## 5. 运行手册索引

- 启动/健康：docs/operations/install.md
- 升级：docs/operations/upgrade.md
- 备份/恢复：docs/operations/backup-restore.md
- 容量与负载：docs/operations/capacity.md
- 安全事件（secret 泄露、越权）：docs/operations/security.md
