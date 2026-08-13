# P4 · RiskInsight 引擎 Spec：模式识别 + 风险假设 + 监控 + 复盘

## 1. Goal / Non-Goals
**Goal**：从内部数据做模式识别与风险推断（一次性分析 + 持续监控预警），假设强制带数据依据；事后复盘（命中/漏报/校准）。
**Non-Goals**：不做自动决策（永远给人验证动作）；不做通用预测；不做量化/时序模型（路线图）。

## 2. 契约（详见 docs/DATA_MODEL.md 风险节）
- `riskinsight/patterns.py`：六类模式规则（trend/ratio/concentration/seasonal/baseline_deviation/signal）+ LLM 语义信号（必须附证据）。
- `riskinsight/generator.py`：模式 -> RiskHypothesis（契约强制：evidence_refs>=1 / 禁模糊词 / 置信规则化 = f(证据强度,模式一致性)）。
- `riskinsight/ranker.py`：排序（置信 x 影响面）+ 用户编辑留痕（revision）。
- `riskinsight/monitor.py`：MonitorScheduler（指标/基线=历史分位/阈值/频率）-> 漂移 -> RiskEvent（去重）。
- `riskinsight/review.py`：事件判定 -> hit/miss/fp/校准分桶统计。
- `external/signals` 信号并入（source_kind=external 分区展示）。
- API：analyze / hypotheses CRUD / review / monitors / events / review-stats。

## 3. 测试计划
1. patterns：六类规则在合成数据上的检出正确性；无模式数据 -> 不产出；边界（空集/单点）。
2. generator 契约：证据空 -> 拒绝；模糊词 -> 打回；置信一致性。
3. ranker：排序稳定；编辑留痕。
4. monitor：基线计算；漂移触发；去重；频率控制。
5. review：命中/漏报/校准统计正确性（合成回放）。
6. 集成（FakeLLM + 内存库）：80 难数据 -> 模式 -> 假设（第 81 难）-> 监控 -> 复盘。
7. 历史回放（L2 前哨）：2023-2024 样例数据推断 -> 对照 2025 实际（人工标注）-> 首版数字。

## 4. 验收标准
- [ ] 六类规则 + 契约测试绿；假设 100% 带依据。
- [ ] 验收集 A4：能推断"第 81 难为渡河类"且带模式证据与置信。
- [ ] 监控调度可用（示例监控项 + 事件产出）。
- [ ] 首版复盘数字产出（无论好坏，如实记录）。

## 5. 风险
- 误报/漏报 -> 阈值可配 + 复盘驱动调参；LLM 信号仅补充。
- 数据不足 -> 明确"数据不足以推断"并列出缺口（也是一种产出）。

## 6. 工作量
约 9 个工作日。
