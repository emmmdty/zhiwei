# 抗污染 FactQA 基准与 Knowledge 评测边界

> 本文中的 120 题是已冻结的 `factqa-v1` 历史资产，不是 Agent Core 的总评测。代码/GitHub、
> 跨源检索、ACL、新鲜度、Memory、Ask 与 Discover 必须使用后续独立 suite，禁止外推。

## 1. 测量对象

基准测量系统能否在固定、受控变化的数据上生成正确且可重放的事实回答，不测模型对名著
原文的记忆。答案**值**由已发布 snapshot 上的 `source_sql` 执行派生；自然语言题面、
模板和 NL-to-SQL 语义映射仍由作者设计，不能写“零人工标注”。

## 2. 四重机制

1. **Counterfactual data**：base snapshot 经声明式 transform 改名、关系、顺序和数值。
   **发布语料就是这份 perturbed snapshot**，仓库不物化第二份 original snapshot；base 数据
   与 transform 声明一起入库，validator 用它们复现发布语料，扰动因而可审计。
2. **Evidence required**：无有效 EvidenceRef 即判错；公开 bundle 由 `zhiwei verify` 重放
   snapshot、SQL/result 与 answer claim span。
3. **Naked-model diagnostic**：独立注册的 `naked-baseline` suite，只给题面不给数据，
   `replicates=3`，分桶 `world_knowledge_stable|data_dependent|mixed`。它**只作污染披露，
   不改变确认性分母**；分桶结果留在该 sealed run 内按 `question_id` join，不回写题集资产。
4. **Execution-derived targets**：模板题与手工题的答案值都在生成时执行得到，并保留
   `source_sql`；人工仍负责问题语义。

## 3. 已知漏洞 L1-L4

| id | 漏洞 | 处理 |
| --- | --- | --- |
| L1 | 扰动不能证明绝对无污染，且会改变难度 | 只声称世界知识在目标题失效；同时报告裸模型诊断 |
| L2 | 纯词法换名可被现代模型绕过 | 同时改关系、次序、数值并构造跨源冲突 |
| L3 | 扰动题可能退化为简单查表 | F4 单列报告；难度变化作为已知代价披露，不用它换分母 |
| L4 | 部分题可凭世界知识答对 | 由 naked-baseline 量化并披露；不按模型表现事后剔题 |

这些限制必须随正式报告发布，不得因结果好看而删除。

## 4. 资产与题型

西游记：SQLite + CSV + Markdown；水浒传：XLSX + 文本 PDF。F2 植入真实冲突和一致负例，
文档为新创作改编文本。全部生成资产入库且可逐字节重建。

| 类型 | 每部 | 总数 | 核心判分 |
| --- | ---: | ---: | --- |
| F1 单源 | 16 | 32 | typed value/set + evidence replay |
| F2 跨源冲突 | 8 | 16 | 两侧事实/一致负例 + 双证据 |
| F3 聚合 | 14 | 28 | 执行结果 + evidence replay |
| F4 扰动反事实 | 12 | 24 | perturbed target + evidence replay |
| F5 多轮 | 6 | 12 | 4 条三轮 chain；约束/实体/答案 |
| F6 不可答 | 4 | 8 | 拒答/澄清；具体编造计错 |

共 120 题：84 template + 36 manual。manual 表示题面映射由人设计，不表示答案值手填。

## 5. 执行单位与分析单位

每条记录随资产冻结 `template_id`、`independence_unit_id` 与 `unit_kind`，由生成器写出，
loader 只校验不推断。两个数必须分开记账：

| 口径 | 含义 | 数量 |
| --- | --- | ---: |
| 执行单位 `sample_id` | 一行题 = 一次 solver 调用 | 120 |
| 分析单位 `independence_unit_id` | 一个确认性观测（108 单轮 + 4 条 F5 chain） | 112 |
| 聚类单位 `template_id` | contamination diagnostic 的 cluster | 57 |

14 个配置因而对应 **1,680 个执行 cell** 与 **1,568 个分析 cell**。两者不相等是正常的；
把它们混成同一个数会让 campaign 的 exactly-once 校验永远无法通过。

确认性总体是**全部 112 个 unit**，在 live 前冻结。题行上的 `targets_perturbed_field` 只表示
"这道题问的字段被扰动过"（仅 F4 为真），是题目属性而非数据集分区，禁止用它筛样本。
裸模型分桶只在 diagnostic 中以 template cluster bootstrap 分析；需要检验时用 cluster-level
sign-flip randomization，不用普通 McNemar。按模型表现事后剔题属于 post-hoc selection，
明确禁止。

## 6. 指标

- `answer_accuracy`：确认性 unit 正确率。
- `fabrication_rate`：F6 上给出无证据具体答案的比例。
- `replay_success_rate`：Evidence Bundle 独立重放通过率。
- `answer_traceability`：有效 claim binding 比例，作为 Gate 而非模型成绩。
- `cost_per_query`、`p95_latency`：仅 live，cached/replay/fixture 分开。

单比例报告 `n + Wilson 95% CI`。B0/variant 差异用 paired bootstrap + McNemar exact，
多重比较按 `docs/EXPERIMENTS.md` 的 Holm family。小分层 `n<30` 只做描述。

## 7. 外部基准

BIRD Mini-Dev 只使用官方下载说明与官方 scorer，不提交第三方数据本体。它是 external
sanity check，不与自建 120 题分数横比，也不把公开执行准确率当无误 ground truth。
没有合格 sealed run 时 README 不放 BIRD 占位分数。

## 8. 已验证与未验证

已验证：`make evals`、`make determinism`、110 项 validator、故障注入（含缺 `template_id`、
误标 `targets_perturbed_field`、残留旧字段、F5 chain 被拆成 single 四类注入均变红后恢复
全绿）和 5 题 SQL 复算；120 行 / 112 unit / 57 template 由 validator 断言。
未验证：裸模型分桶、任何系统/model 分数、handoff 与 BIRD；这些依赖 `src/` 实现和 sealed
live run。

## 9. 新平台必须新增的 suite

| suite | 测量对象 | 不能被 120 题替代的原因 |
| --- | --- | --- |
| `knowledge-doc-v1` | hierarchical locator、table/cell、hybrid retrieval | 旧题只覆盖有限自建文档/表格 |
| `knowledge-code-github-v1` | repo@commit、symbol/ref、diff/PR/issue/check | 旧题没有代码语义和版本更新 |
| `knowledge-cross-source-v1` | doc/code/GitHub/DB/API 联合查证与冲突 | 旧 F2 不覆盖企业 source planner |
| `knowledge-acl-freshness-v1` | pre-filter/post-check、撤权、删除、水位、stale Evidence | 旧资产没有多用户 ACL 与同步 |
| `ask-v1` | task decomposition、clarification、Fact/Inference、partial/abstain | 单题执行准确率不等于研究 Agent 完成度 |

新 suite 仍应采用冻结数据、source-native target、independence unit、blind holdout 与 deterministic scorer
优先原则，但不应为了沿用旧模板而把代码/GitHub 问题硬改成 Text-to-SQL。
