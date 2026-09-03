# spike-02：Token 估计器校准方法论

对应 [ADR-002](../../docs/DECISIONS.md#adr-002) 与总设计 token counting 三级合约。

**要回答的问题**：通过本地估计器 + 学习校准参数 + 保守余量的组合，能否在不调用 provider
count_tokens API 的情况下，将 token 估计误差控制在安全范围内？

**结论**：可行，`verdict: FEASIBLE`。校准后 MAE 显著下降，99th percentile 余量能覆盖
held-out 集最坏情况。

## 怎么跑

```bash
uv run python spikes/token_calibration/run_spike.py
```

退出码 0 = 全部断言通过；证据写入 `evidence/spike-02-token-calibration.json`。

不发真实网络请求，不使用 tiktoken 等外部 tokenizer 库。使用模拟 provider actual 值
（基于真实 provider 的 chars-per-token 比例）。

## 三级合约

| Level | 来源 | 语义 |
| --- | --- | --- |
| 1 | provider `count_tokens` API | authoritative_count |
| 2 | 官方 tokenizer 本地实现 | verified_local_count |
| 3 | 校准估计器 + 保守余量 | calibrated_estimate |

未知 ModelProfile → fail closed → level 3 margin。

## 场景

| 场景 | 问题 | 结果 |
| --- | --- | --- |
| S1 估计器精度 | 不同内容类型下各估计器的 MAE？ | 结构感知估计器在 JSON 上最优 |
| S2 校准误差降低 | N=50 校准后 held-out 集误差降低？ | 校准后 MAE 降低 |
| S3 余量覆盖最坏情况 | 99th percentile 余量覆盖 held-out 最坏误差？ | 全覆盖 |
| S4 context_length_exceeded 映射 | 错误类型映射 + 触发重校准？ | 映射正确，触发重校准 |
| S5 重校准更新余量 | 新数据后余量更新？ | 样本数增加，参数更新 |
| S6 未知 profile fail-closed | 未知 token_counting_level 默认 level 3？ | 默认 level 3，应用保守余量 |
| S7 压力测试 | 100K+ 字符输入不崩溃？ | 全部估计器完成 |

## 关键发现

### 1. 估计器选择影响初始精度，但校准能弥补

简单 char_estimator 在所有内容类型上都有较大误差，但结构感知估计器在 JSON 上明显更优。
校准方法对基础估计器的质量要求不高——即使初始误差大，线性校准 (actual = scale × estimated + bias)
也能显著降低 MAE。

### 2. 99th percentile 余量提供安全边界

训练集的 99th percentile 余量能覆盖 held-out 集的最坏情况误差。这意味着在已知数据分布下，
余量策略是保守且安全的。

### 3. context_length_exceeded 是重校准触发器

将 `context_length_exceeded` 映射为 `context_refusal`（而非 provider failure），并触发
估计器重校准。这确保系统在遇到上下文溢出时主动学习，而不是被动失败。

### 4. 未知 profile 的 fail-closed 是安全默认

当 ModelProfile 的 token_counting_level 为 None 时，系统默认使用 level 3 的保守余量，
而不是猜测或使用可能不安全的默认值。

## 未覆盖（明确标注为「未验证」）

- 使用模拟 provider actual，未验证与真实 tokenizer（tiktoken 等）的差异。
- 未测试非线性校准方法（多项式回归、神经网络等）。
- 未测试跨 provider 的校准迁移性。
- 未测试动态余量（根据输入分布自适应调整）。
- 未测试并发重校准的线程安全性。

## 文件

- `estimator.py`：四种本地 token 估计器实现。
- `calibrator.py`：校准引擎——误差度量、参数学习、余量计算。
- `content_types.py`：测试内容生成器 + 模拟 provider actual。
- `run_spike.py`：场景与断言，写出证据文件。
- `evidence/spike-02-token-calibration.json`：证据。

本目录不进 `src/`，也不进 `tests/`：它是一次性的可行性验证，不是产品代码，也不参与任何 Gate。
