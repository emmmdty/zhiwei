# Eval Report — S11 Gate 密封轮（2026-09-08）

> 口径：`mode=offline`（生产 Runtime 同构执行、fixture 模型、确定性语料）。
> 每行数字由 Claim Registry 绑定（`zhiwei release check --strict` 强制），密封件
> verbatim 落盘于 `artifacts/gates/s11/<suite>.json`（seal_digest 可独立复算）。
> 本表不构成 Agent 效果声明——offline 口径证明的是**实现纪律与契约正确性**
> （specs/s11 §8；live 口径需 operator 显式密封）。

## 套件结果（11 suites / 225 units，全部 completed）

| Suite | mode | units | 终态 | seal_digest（前 20） |
| --- | --- | --- | --- | --- |
| factqa-v1 | offline | 120 | 120 completed | `sha256:d4642ff4f6dd1…` |
| numeric-risk-v1 | offline | 22 | 22 completed | `sha256:7c9488d0e6ffe…` |
| knowledge-code-github-v1 | offline | 15 | 15 completed | `sha256:2bd7e8b7932b…` |
| knowledge-doc-v1 | offline | 15 | 15 completed | `sha256:ba7ecb2edf07…` |
| knowledge-cross-source-v1 | offline | 12 | 12 completed | `sha256:62b3a701d2e94…` |
| enterprise-memory-v1 | offline | 12 | 12 completed | `sha256:6d8c907b9c48…` |
| knowledge-acl-freshness-v1 | offline | 11 | 11 completed | `sha256:b542dc4ff954…` |
| runtime-contract-v1 | offline | 7 | 7 completed | `sha256:cfc84cce4219…` |
| ask-v1 | offline | 6 | 6 completed | `sha256:3271daab149c…` |
| discover-blind-v1 | offline | 5 | 5 completed | `sha256:53f2cc0fcf5f…` |
| legacy-assets | fixture | — | 不支撑 claim（runbook 原文口径） | — |
| longmemeval-adapter | external | — | unavailable → claim 保持 **planned**（缺数据不解锁质量 claim） | — |

## 复算方式

```bash
# 密封件独立复核（eval verify --all-sealed，系统级 DSN）
ZHIWEI_DATABASE_URL=<maintenance DSN> uv run zhiwei eval verify --all-sealed
# README claims 块与 Claim Registry 的一致性（strict）
uv run zhiwei release check --strict
```

claim → suite 映射与 bound_value 渲染见 Claim Registry（`claim_registry` 表，
scope 字段为权威口径：mode/version/date/corpus/environment）。

## 无效果声明边界

- 本表**没有** accuracy/recall/质量百分比的裸数字——质量值只经 Claim Registry
  渲染到 README claims 块（seal provenance 审计链）；
- 没有 live 模型数字（成本/延迟/模型质量）——live 密封是 operator 显式动作
  （specs/s11 §6），截至本报告未发生；
- 没有 HA/SLO 声明（specs/s11 §8：没有跨节点证据不写 HA，没有测量不承诺）。
