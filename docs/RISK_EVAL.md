# Discover 与 Numeric Risk Detector 评测

## 1. 定位

Discover 是持续风险发现到人工处置的 Agent App；RiskInsight 是它的第一个 **Numeric Risk Detector
Pack 与 reference eval**。二者不得等同：planted 数值模式能验证 detector/evidence 工程，不能验证
真实企业风险预测、主动发现效用或业务处置正确性。

借鉴 InsightBench 的 planted-insight 思路，但核心 pattern scorer 不用 LLM。生成与检测共享作者定义仍
存在结构性自证优势，因此必须同时有 blind holdout、source-delta journey、反证、人工 utility 和真实
Case/HumanResolution 指标。

## 2. Discover 分层评测

| 层 | 对象 | 主要方法 |
| --- | --- | --- |
| D0 Contract | Trigger、watermark、Signal/Hypothesis/Resolution schema | deterministic/unit |
| D1 Data quality | missing/duplicate/unit/schema/watermark | fault injection |
| D2 Detector | planted pattern、distractor、independent Evidence | frozen synthetic suite |
| D3 Discovery | change-driven/controlled exploration、falsification、dedupe | blind holdout/metamorphic |
| D4 Workflow | feed/triage/Case/approval/ActionReceipt/outcome | fixture/integration/E2E |
| D5 Utility | 是否减少分析时间、是否 actionable、误报负担 | blinded human review |
| D6 Operations | schedule/webhook/reconciliation、retry/cost/latency | fault/load report |

不能用 D2 recall 替代 D3-D6。

## 3. 当前历史资产

`【已验证】` 当前仓库有单 seed、14 planted、7 distractor 的确定性生成/validator 资产。
`【未验证】` `snr/difficulty` 仍是声明值，kind 仍含旧命名，尚无 detector/solver、multi-seed、blind holdout
或 Discover runtime。旧 manifest 保留为 migration/input contract，不写成当前产品效果。

## 4. Numeric Detector confirmatory suite

虚构云梯科技 36 个月，营收/应收/供应/现金流四张事实表。正式 suite 计划冻结 10 个 seed
`20260811..20260820`，每 seed 从同一 base 生成 clean/planted 配对 snapshot。solver 只挂载 snapshot，
不读取 planted manifest；这防无意泄漏，不是对恶意宿主读取的沙箱。

生成器、detector、scorer 不互相导入实现，只共享版本化 schema/kind/unit。scorer 必须从 snapshot
独立复算公式和 Evidence，篡改 generator manifest 不应改变 score。

## 5. realized SNR

定义 `robust_sigma(x)=max(1.4826*MAD(x),1e-9)`：

| id/kind | realized SNR |
| --- | --- |
| P1 `trend` | OLS 首尾变化绝对值 / 残差 robust sigma |
| P2 `concentration` | share OLS 首尾增量 / 残差 robust sigma |
| P3 `seasonal` | 相对历史同月中位数偏差 / 历史季节残差 robust sigma |
| P4 `baseline_deviation` | post/pre 中位差 / pre robust sigma |
| P5 `ratio_divergence` | `min(delta_revenue/sigma_revenue,-delta_cashflow/sigma_cashflow)` |
| P6 `compound_supplier_dependency` | `min((max_share-threshold)/sigma_share,-delta_on_time/sigma_on_time)` |

方向不成立时 SNR=0。难度由数据复算：hard `[0.8,1.5)`、medium `[1.5,3)`、easy `[3,+inf)`；
distractor `<0.8`。SNR 只适用于这些数值模式，不能成为所有 RiskHypothesis 的通用置信。

## 6. 资产自检

- plantability：植入后落入声明档位。
- ghost：clean 不越过 0.8。
- counterfactual：撤销植入恢复 clean 结论。
- distractor：实现后 SNR 严格低于阈值。
- dirtiness：missing/duplicate/unit change 覆盖 detector 核心字段并逐 seed/kind 报告。
- provenance：snapshot、generator/scorer version、seed、formula、entity/window/digest 完整。

ghost/counterfactual 是评测资产质量，不是生产 Discover 的功能主张。

## 7. 匹配与 Evidence

benchmark candidate edge 要求 kind、entity、metric/component set 相同且 window IoU>=0.5；以最大 cardinality、
再最大 total IoU 的确定性一对一匹配，平局按 id 排序。此规则只用于有 planted target 的 benchmark，
生产去重使用可解释 `RiskFingerprint`；semantic similarity 只提议合并。

每 kind 的 `PatternRef` verifier 从 Source Ledger snapshot 复算 entity、metric、window、rows、units、direction
和 formula。pattern 命中但 Evidence 失败时 recall 可命中、`evidence_validity=0`，不能合成一个分数。

## 8. 指标

### Detector suite

- recall（overall/easy/medium/hard）、precision、distractor FP、Evidence validity。
- clean/planted 以 seed 为 cluster paired bootstrap，并列出每 seed；小 n 不给伪精确显著性。
- confidence 只有 `n>=100` 才报告 ECE；否则给 reliability table。

### Discover product

- trigger/watermark coverage、data-quality refusal、time-to-hypothesis、dedupe/reopen correctness。
- supporting/contradicting Evidence completeness、triage disposition、Case conversion、action/result status。
- blind reviewer relevance/actionability、false-positive burden、time saved；不输出虚构“风险概率”。
- schedule/webhook/reconciliation failure、cost/latency、stale/ACL/security refusals。

description/inference quality可用 LLM judge 只作辅助，需有人类锚点、blind/randomized presentation 与可靠性
检查；不通过则不发布。

## 9. 防自证要求

- author-visible frozen suite 与 author-hidden blind holdout 分开 sealed。
- planted formula family 外加入 change-driven cases，避免 detector 重读 generator ontology。
- falsification task 明确要求寻找反证/缺失，不只生成支持叙事。
- HumanResolution 与实际 outcome 不能回写修改原 Hypothesis；用于新版本 lesson/memory candidate。
- 报告同时展示 missed、false positive、duplicate、insufficient data、abstain 和 effect_unknown。

名著“推断第 81 难”只可作定性演示，因世界知识污染不进入核心指标。
