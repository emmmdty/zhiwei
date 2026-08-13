# evals 基准资产

## 已落地

```bash
uv venv
uv sync --extra evals --extra dev
make evals
make determinism
```

当前生成 120 道 FactQA（84 template + 36 manual）、14 planted risk patterns、7 distractors；
validator 报告 110 项检查，21 个发布资产两次重建逐字节一致。

题集随资产冻结三个统计字段：120 行题 = **112 个 `independence_unit_id`**（108 单轮 +
4 条 F5 chain）= **57 个 `template_id`**。它们由生成器写出，下游 loader 只校验不推断。

目录：

```text
evals/
  novels/        SQLite/CSV/XLSX/Markdown/PDF 与 perturbation manifests
  questions/     template 与 manual JSONL
  risk/          当前单 seed synthetic data 与 planted manifest
  scripts/       corpus/questions/risk generators 与 validator
  configs/       已冻结 ablation/prereg；实现期补 suite 与 runtime model 配置
  fixtures/      实现期加入三 transport 脱敏 fixtures 与 FixtureTape
  runs/          实现期加入 partial/sealed run artifacts
  reports/       只提交 release-qualified 报告
```

## Ground truth

答案值由每题 `source_sql` 在发布 snapshot 上执行派生；题面到 SQL 的语义仍是人设计的。
手工题不等于手填答案。修改资产的顺序固定为 corpus -> questions -> risk -> checksums ->
validate。

## 抗污染

发布语料本身就是 perturbed snapshot，确认性总体是全部 112 个 unit，在 live 前冻结。
naked-model `k=3` 是独立注册的 `naked-baseline` suite，只作 contamination diagnostic，
以 `template_id` 为 cluster，**只披露不改分母**。F5 多轮以 chain 为 independence unit。
F2 同时包含真实冲突和一致负例；F6 让 fabrication rate 有明确分母。

已知 L1-L4 见 `docs/BENCHMARK.md`，正式报告不得省略。

## Run 纪律

- FixtureTape：人工确定性响应，只验 transport/harness/UI，不产模型分数。
- ReplayTape：只从合格 live run 录制。
- 所有预注册 unit 必须终态才能 seal；预算/中断留下 partial 并 resume。
- run 固定 dataset/config/profile/prompt/source digests、usage、错误、data class 和限制。
- live 只走 OpenCode Go、公开/合成数据、并发 1、`Use balance=off` 与硬预算。

当前机器可读注册表：

- `evals/configs/ablation/index.yaml`：B0 + 13 variants，共 14 个唯一配置。
- `evals/configs/prereg.yaml`（schema v2）：FactQA 六比较 Holm family、Handoff 两个三比较
  family、naked-baseline 诊断，以及逐项可复算的 live 预算。执行 cell（1,680）与分析
  cell（1,568）分开登记，Handoff 按边分片，A-prefix 计入预算。

## 外部数据

BIRD 数据不入库，只提供官方下载、许可、checksum 和官方 scorer 接入。自建分数与 BIRD
不得横比。
