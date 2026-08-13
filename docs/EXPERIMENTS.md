# 消融、Handoff 与失效分析

## 1. 角色

实验是证明 Agent Core 和 Agent App 工程决策的工具，不是产品本体。旧 FactQA 消融只回答特定
schema/prompt/retrieval/model 配置的影响，不能证明多租户、Knowledge、Memory、Capability Hub 或
Discover。所有配置、primary outcome、independence unit 和 multiplicity family 必须在 live 调用前冻结。

## 2. B0 与 14 个唯一配置

B0：`schema=table+column+comments`、`few_shot=fixed_3`、`result_binding=on`、
`rewrite_max=2`、`retrieval=dense`、`model_tier=medium`。

| id | 相对 B0 唯一变化 |
| --- | --- |
| B0 | 无 |
| A1-0 | schema none |
| A1-1 | table + column |
| A1-3 | comments + 3 samples + range |
| A2-0 | zero-shot |
| A2-2 | retrieved 3-shot |
| A3-1 | result binding off |
| A4-0 | rewrite 0 |
| A4-1 | rewrite 1 |
| A5-0 | BM25 |
| A5-2 | hybrid RRF |
| A5-3 | hybrid RRF + reranker |
| A6-0 | small = qwen3.6-plus |
| A6-2 | large = qwen3.7-max |

B0 medium = qwen3.7-plus。A6 是同 lineage 部署档位对照，不声称纯参数量因果效应。A5
dense 与 reranker 固定总设计中的 BGE revisions，以 CPU 为正式基线。

注册器必须证明 id 唯一、共 14 项、每个 variant 与 B0 恰差一装配因子。handoff 是独立
实验族，不能混进六维后继续声称单变量。

## 3. FactQA 统计

- confirmatory 总体是全部 **112 个 `independence_unit_id`**，每 unit 一个 binary outcome；
  F5 chain 先聚合为全轮均正确。发布语料本身就是 perturbed snapshot，没有"选分区"这一步。
- 10,000 次固定 seed paired bootstrap delta CI + 双侧 McNemar exact，`alpha=0.05`。
- core Holm family：`A1-0/A2-0/A3-1/A4-0/A5-0/A6-0 vs B0` 六个 p 值。
- 其余 variant exploratory，只给 effect/CI，不写确认性显著。
- 成本给 paired mean/median delta CI；live latency 给 p95 bootstrap。retry 全部计入。
- 裸模型污染诊断由独立的 `naked-baseline` sealed run 提供，按 `template_id`（57 个）
  cluster bootstrap，**只披露不改分母**；按模型表现事后剔题是 post-hoc selection。

## 4. Handoff estimands

每条 chain 的 switch turn 前只执行一次，冻结同一个 A-prefix：

1. switch effect：canonical A->B vs canonical A->A。
2. method effect：canonical A->B vs transcript-only A->B。

旧预注册 pilot 边：DeepSeek Flash -> Luna、DeepSeek Flash -> Qwen Plus、Qwen Plus -> Luna，覆盖 chat、
responses、messages，**每边 12 条 chain、每边一个 child run**。两个 Holm family 各含三条 edge 上唯一
primary `handoff_answer_accuracy`。constraint/entity/evidence/replay/compaction/cost/latency 全是
secondary，以 chain cluster bootstrap 给 CI，不作显著性主张。

预算：A-prefix 是真实 live 调用，按每 chain 4 次 attempt 计入；逐边最坏值
`$3.5536896` / `$1.4893056` / `$4.2270720`，合计 `$9.2700672`。三边合计超过单 run `$10`
上限，因此必须分成三个 child run，不能塞进一条命令。

检验力：每边 12 条 chain 只够 pilot effect estimation，报告必须写明“未达显著 ≠ 两种交接方式等价”。
正式 confirmatory handoff 先定义最小有意义效应，以 pilot variance/power analysis 冻结样本；若预算只允许
pilot，就明确停在 pilot，不得把零结果反向宣称 transcript-only 足够好。

`authoritative_state_preservation_rate` 是 100% 或拒绝的结构 Gate，不冒充模型理解能力。

## 5. E1-E7

| id | 判据 | 关联实验 |
| --- | --- | --- |
| E1 | 表/列语义选择错误 | A1 |
| E2 | 时间、单位、去重、实体口径错误 | constraint 设计缺口 |
| E3 | GROUP BY、分母、排序或窗口逻辑错误 | A2 |
| E4 | claim 数值不在结果/quote | A3 |
| E5 | ref 缺失或 bundle/claim replay 失败 | Evidence Gate |
| E6 | snapshot 有答案但拒答 | A4 |
| E7 | gold chunk 未召回 | A5 |

允许 primary + secondary，不强迫单一因果。能确定性分类的自动标，剩余错题作者标注；单作者
标签只作探索分析，不报告虚假的一致率。

Handoff 另用 H1 transport mismatch、H2 projection loss、H3 entity/constraint loss、H4
compaction loss、H5 overflow/refusal。

## 6. Run 纪律

- 正式 `temperature=0`、每 unit/treatment 一次；非确定性作为限制。
- exploratory robustness 才做 3 replicate，并按 unit/chain cluster；不得称 epoch。
- 网络错误、拒绝、context refusal 在分母内；partial 只能 resume。
- 质量、quota value、vendor cost estimate、incremental charge、latency 与失败逐 sample 保存。
- FixtureTape 只验证 harness；不能产生模型效果表。
- FactQA live 不是一个 `$28.48` 的单 run。冻结 campaign manifest 枚举
  `14 x 120 = 1,680` 个**执行 cell**（对应 `14 x 112 = 1,568` 个分析 cell）：13 个配置各一个
  120 行 child，A6-2 按 `sample_id` 字典序稳定拆成两个 60 行 child（同一条 F5 chain 的三行
  必须留在同一 child），共 15 个互斥 child runs。单 child worst-case <= `$4.3776`，
  cap `$10`、80% 调度阈值 `$8`；全部 child sealed、输入 digest 相同且执行 cell 恰好一次
  覆盖后才允许配对分析。
- S4 注册总额 `$43.0231552` = FactQA `$28.47744` + Handoff `$9.2700672` +
  BIRD `$4.096` + naked-baseline `$1.179648`。它超过 `$30/week`，至少跨两个自然周；
  调度由 `config/budgets/opencode-go.yaml` 的 `rolling_window_ledger` 做窗口预检，
  避免几个 child 挤进同一个 `$12/5h` 窗口后被服务端中途截断。

## 7. 平台级实验族

后续实验分开注册，不拼成一个“总分”：Knowledge（doc/code/GitHub/cross-source/ACL/freshness）、Memory
（write/read/conflict/forget/poison）、Ask（task/evidence/abstain）、Discover（detector/blind/human utility）、
security、recovery/concurrency/latency/cost。不同 suite 的 unit、scorer 和证据边界不同；外部 LongMemEval、
LoCoMo、BIRD、Promptfoo 只作其覆盖面的诊断。

所有 Eval 运行生产 Runtime，仅替换 binding 为 fixture/replay/live/shadow。确定性项目不用 LLM judge；
Inference/utility 使用校准人评/judge 时必须有盲化、rubric、顺序扰动和一致性报告。
