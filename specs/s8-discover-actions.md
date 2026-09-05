# S8 - Discover, Cases and Governed Actions

> Status: frozen implementation specification  
> Depends on: S7  
> Unlocks: S9

## 1. Goal

交付从 schedule/webhook/source delta 到 RiskHypothesis、人工 triage、Case、approved action 和
HumanResolution 的 Discover App。旧 RiskInsight 作为 Numeric Detector Pack 接入，不让它定义整个产品。

## 2. Required modules

```text
solution-packs/discover/{pack.yaml,agent.yaml,task_graph.yaml,skills,schemas,views,evals}/
src/zhiwei/cases/{signals,hypotheses,resolutions,risk_fingerprint,actions}.py
src/zhiwei/runtime/triggers/{schedule,webhook,source_delta}.py
src/zhiwei/evidence/patterns/
apps/web/src/features/{discover,cases,actions}/
tests/{unit/discover,contract/discover,integration/discover,e2e/discover}/
```

## 3. DiscoveryProgram

ProgramVersion 固定 risk charter、sources/entities、exclusions、triggers、detector packs、evidence/falsification
standard、recipients、budget、approval/action policy 和 service identity。activate/deactivate/version change 有
audit；后台 run 不继承创建者 session/token/personal memory。

### 3.1 Trigger → Runtime integration

DiscoveryProgram 的所有 trigger（schedule、webhook、source delta）**必须**通过 S2 Runtime 的
`StartRun` 命令发起执行，不得绕过。这确保：

- **Canonical event tracking**：每次 trigger 引发的执行都产生完整的 Run event 序列，
  可被 SSE/REST projection 消费。
- **Approval enforcement**：涉及副作用的 action 必须经审批路径，绕过 Runtime 无法执行审批门禁。
- **Evidence contract compliance**：Evidence 生成和验证必须绑定到具体 Run/Attempt，
  无 Run 的 evidence 无法追溯来源。
- **Service identity inheritance**：后台 run 使用 DiscoveryProgram 的 service identity，
  不继承触发者的 session/token/personal memory。

## 4. Pipeline contracts

```text
Trigger -> watermark/snapshot -> DataQualityResult
-> Signal -> RiskHypothesis -> EvidenceSet + FalsificationResult
-> RiskFingerprint/dedupe -> Feed/Triage -> Case
-> Approval/ActionReceipt -> HumanResolution -> lesson candidate
```

Signal、Hypothesis、Resolution immutable linked versions。Hypothesis 包含支持/反证/缺失、affected entities、
source watermark、detector/analysis version、建议验证动作、owner/status；启发式 score 不称 probability。

**序贯证伪机制**（[ADR-004](../docs/DECISIONS.md#adr-004)，方法锚点为 POPPER 的 agentic sequential
falsification）：`FalsificationResult` 不是一段自由文本，而是一组 typed `NegativeProbe` 的执行结果。

```text
RiskHypothesis
  → 生成 N 个 typed NegativeProbe：「若此假设为假，应观察到 X」
  → X 归约为 {metric, entity_scope, window, comparator, threshold} 之类可机器求值的结构
  → 逐个执行，每个 probe 结果作为独立 EvidenceRef 附加
  → 序贯累积证据并控制 Type-I error
  → 未被推翻且证据充分 → human triage；被推翻 → 终止并保留完整证伪轨迹
```

### 4.1 NegativeProbe model

```python
from dataclasses import dataclass
from enum import Enum


class Comparator(str, Enum):
    LT = "lt"
    GT = "gt"
    EQ = "eq"
    NEQ = "neq"
    GTE = "gte"
    LTE = "lte"


@dataclass(frozen=True)
class NegativeProbe:
    """A structured falsification probe against a RiskHypothesis."""
    metric: str  # what is being measured
    entity_scope: EntityScope  # what entities are covered
    window: TimeWindow  # temporal bounds for observation
    comparator: Comparator  # comparison operator
    threshold: float  # threshold value
    expected_outcome: str  # what would disprove the hypothesis
```

Probe 的求值必须由确定性组件完成（见 §4 三条硬约束之「模型只提出、不判定」）。
每个 probe 结果作为独立 EvidenceRef 附加到 Hypothesis，不可合并或省略。

三条硬约束：

- **职责分离**：probe 的生成与求值由独立 task node 承担，不复用产生该 hypothesis 的 detector/exploration
  上下文，避免 confirmation bias 沿上下文传导。
- **模型只提出、不判定**：模型仅负责提出候选 probe，求值一律由确定性组件完成——与「确定性可判项
  不用 LLM judge」的既有纪律一致。
- **准入门槛**：hypothesis 只有在「至少 N 个 negative probe 已执行且未推翻」时才能进入 triage 队列，
  否则停留在 Signal 状态。N 由 ProgramVersion 的 falsification standard 声明。

生产去重用 typed RiskFingerprint；semantic similarity 只提出 merge candidate。reopen/new version/dismiss/false
positive/accepted/mitigated 等 resolution 不改写原 detector output。

## 5. Detector paths

- deterministic known-pattern：Numeric Risk Detector Pack，PatternRef 独立复算。
- change-driven：source diff/watermark 生成 typed comparison tasks。
- controlled exploration：模型只能提出 AnalysisSpec，由 allowed analysis tool 执行；不得自由读全库或写脚本。

所有路径必须经过 data quality、Evidence/falsification、dedupe 和人类 triage。

## 6. Workbench journey

Discover feed 展示 status/owner/severity rationale/supporting/contradicting/freshness/dedupe。用户能 triage、
创建 Case、让 Ask 补证、请求 tool action、审批并记录 Resolution。刷新/重试不会复制 hypothesis/case/action。

## 7. Evaluation

- 旧 manifest 作 migration regression；新 10-seed clean/planted 按 `docs/RISK_EVAL.md` 独立生成/评分。
- blind change-driven holdout 与 planted suite 分开；human utility 盲化评相关性/actionability/误报负担。
- fault：missing/duplicate/unit/schema/watermark、late webhook/reconcile、ACL revoke、stale source。
- outcome：Hypothesis→triage→Case→Action→Resolution linkage，不以 Action 数量当成功。
- falsification：`falsification_coverage`（已执行 probe / 应执行 probe）与 `hypothesis_refutation_rate`
  是一等指标。**refutation_rate 恒为 0 是危险信号**——说明证伪机制没有真正在工作，等同于项目对校验器
  的既有纪律「一个从不失败的校验器等于没有」。注入必然可被推翻的假设，断言其确实被推翻。

## 8. Gate

```bash
uv run pytest tests/unit/discover tests/contract/discover -q
uv run pytest tests/integration/discover tests/security/discover_identity -q
npm --prefix apps/web run test:e2e -- discover-case-action.spec.ts
uv run zhiwei risk generate --suite numeric-risk-v1 --check
uv run zhiwei eval run --suite numeric-risk-v1 --mode offline --seal
uv run zhiwei eval run --suite discover-blind-v1 --mode offline --seal
```

Gate 报告分开 D0-D6；不能用 planted recall 解锁“真实风险预测”声明。

## 9. Explicit non-goals

不自动决定业务真相，不用伪概率，不默认执行高风险动作，不读取用户 personal memory。未获得真实人评/
outcome 前只声明 reference corpus 上的发现与工作流结果。
