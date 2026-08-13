# 西游记 · 八十一难 · 语料包

> 由 `evals/scripts/build_corpus.py` 生成。**不要手工编辑本目录下的产物**——改基础数据后重跑脚本。

## 数据形态

- `sql/xiyouji.db` —— SQLite，`nan`（81 行）与 `characters`（25 行）两表
- `csv/nan.csv`、`csv/characters.csv` —— 同一数据的 CSV 形态
- `docs/xiyouji_notes.md` —— 改编节选（新创作文本）

## 字段来源分级

| 级别 | 含义 | 本包中的字段 |
| --- | --- | --- |
| canonical | 原著可考，存在于 LLM 预训练数据中 | `nan_no` / `nan_name` |
| curated | 人工整理，口径为本项目自定义，**待人工复核后冻结 v1** | `category` / `location` / `opponent` / `helper` / `chapter_hint` |
| synthetic | 本项目虚构，固定种子生成，世界知识无法作答 | `duration_days` / `difficulty_score` |

**为什么要混合 canonical 与 synthetic**：canonical 字段被扰动后，用于测量"系统是否真的查了数据"
（世界知识会给出与数据冲突的答案）；synthetic 字段任何模型都不可能知道，用于测量"纯检索与聚合"能力。
两类互补，报告中分别统计。

## 判分纪律

**ground truth 一律以本目录的实际数据 + `perturbation_manifest.json` 为准，不以原著为准。**
基础数据中若存在与原著的个别出入，不影响判分有效性；但会影响裸 LLM 基线的解释力，
故 curated 字段标记为待复核。

## 扰动

共 9 条声明式扰动，逐条记录 before/after 与理由，见 `perturbation_manifest.json`。
`validate_corpus.py` 会逐条回验扰动是否已生效。

## 受控跨源冲突

- **XY-C01**：第 36 难的发生地 —— 文档侧记为 `黑水河`，与表格侧不一致。期望行为：报告两源分歧，各自给出 QueryReplay 与 DocRef
- **XY-C02**：孙悟空首次登场的难序号 —— 文档侧记为 `5`，与表格侧不一致。期望行为：报告两源分歧（表为 8，文档为 5）
- **XY-C03**：水难类的难数 —— 文档侧记为 `表中实际值 + 1`，与表格侧不一致。期望行为：报告文档声明的总数与表中聚合结果不一致
- **XY-C04**：第 81 难的对手 —— 文档侧记为 `老鼋`，与表格侧不一致。期望行为：报告分歧。⚠️ 文档刻意保留了未扰动的世界知识值，系统若『觉得文档更合理』而擅自采信文档，即暴露了世界知识偏置

这些冲突是**故意的**。正确行为是报告分歧并给出两处出处，而不是擅自选一个。
