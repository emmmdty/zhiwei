# P6 · 基准评测 Spec：四大名著 100 题 + 基线对照 + 报告

## 1. Goal / Non-Goals
**Goal**：四大名著基准全量跑分（防污染三重机制：数据扰动/溯源强制/裸 LLM 基线对照）+ 公开基准子集（Spider/BIRD）+ RiskInsight 历史回放复盘；产出对外报告。
**Non-Goals**：不做大规模基准全量；不做模型微调。

## 2. 契约（详见 docs/BENCHMARK.md）
- `evals/novels/`：四部语料包 + perturbation_manifest.json。
- `evals/questions/`：100 题 JSONL（题型/ground truth/判分规则；每题含"裸 LLM 基线答案"预跑字段）。
- `evals/scripts/`：run_benchmark.py（判分：自动 + 溯源强制 + 基线对照）→ run_report.py（报告生成）。
- 报告：benchmark-summary.md / baseline-comparison.md / risk-inference.md；强制字段（模型/题集版本/样本量/日期/成本/局限）。

## 3. 测试计划
1. 判分器单元：数值容差/集合比对/SQL 结果比对；无溯源判错；基线分桶标签（知识/推理题）。
2. 语料包一致性：四形态同数据（SQLite/CSV/Excel/文档抽样比对）；扰动清单可复算。
3. 指标空集边界（N/A 而非 1.0）。
4. 真实跑分（nightly，预算断言）：100 题 + 基线对照 + 推断题 rubric（人工）。
5. 公开基准子集：Spider 20 题适配跑分。

## 4. 验收标准
- [ ] 100 题全量判分完成，报告入 `evals/reports/`，数字如实。
- [ ] 裸 LLM vs 系统对比表产出；知识题/推理题拆分明确。
- [ ] 验收集 A5（扰动）通过——证明测的是推理不是记忆。
- [ ] README 增"Benchmark"节对外展示。

## 5. 风险
- 题集泄露/污染 -> 扰动 + 溯源强制 + 基线对照三重机制；题集不公开。
- 成本 -> 预算断言 + 分批跑。

## 6. 工作量
约 6-7 个工作日（标注与判分为主，可并行）。
