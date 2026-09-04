# S3 - Models, Canonical Context and Handoff

> Status: frozen implementation specification  
> Depends on: S2  
> Unlocks: S4

## 1. Goal

交付三协议薄 adapter、Endpoint/Model/Profile/Attestation、Model Router、Canonical reducer、Context Compiler、
压缩与跨模型两类 manifest。每次实际 model request 必须绑定 canonical source 和实际 wire body。

## 2. Required modules

```text
src/zhiwei/models/{contracts,profiles,attestations,router,usage}.py
src/zhiwei/models/transports/{base,openai_chat,openai_responses,anthropic_messages}.py
src/zhiwei/context/{types,reducer,inventory,budget,compiler,compression,transition,manifests}.py
src/zhiwei/workflows/activities/model.py
src/zhiwei/runtime/handlers/model_actions.py
tests/{contract/models,unit/context,integration/context}/
```

## 3. Context contract

- authoritative：objective/constraints/tasks/entities/decisions/conflicts/evidence/actions/approvals/budget/obligations。
- conversational 可有 deterministic summary + source event refs；recoverable 只保留 artifact ref；opaque 不持久化。
- compiler 输入和优先级由 `docs/MODELS.md` 固定，输出 provider-neutral ContextIR。
- serializer 生成 body 后，发送层 capture bytes/normalized semantic body，计算 digest 并执行 classification/
  inventory/policy gate；只有通过才能网络发送。
- **capture 位置**（[ADR-001](../docs/DECISIONS.md#adr-001)）：pre-send capture 必须实现在自定义 httpx
  transport 的 `handle_async_request` 中，不得挂在 SDK 调用层。provider SDK 一律 `max_retries=0`，重试
  上移为显式新 Attempt + 新 ContextManifest；capture 与 send 在同一 transport 调用内完成，digest 计算
  失败即拒绝发送。该 transport 是唯一出网路径。
- ContextManifest/TransitionManifest schema、version、canonical JSON、verify CLI 与 tamper errors 固定。
- context overflow 先缩 recoverable/conversation，再 task split/allowed model；authoritative 不完整则拒绝。

### 3.1 MemoryPort Protocol

Context Compiler 需要从 S7 Memory 服务检索和提交记忆片段。以下协议定义 S3 对 S7 的依赖接口：

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MemoryFragment:
    """A single memory retrieval result."""
    fragment_id: str
    content: str
    source: str  # workspace | entity | lesson
    relevance_score: float
    classification: str  # DataClassification value
    token_count: int


@dataclass(frozen=True)
class MemoryCandidate:
    """A memory submission for dedup/merge evaluation."""
    content: str
    source_run_id: str
    entity_refs: list[str]
    classification: str
    ttl: timedelta | None = None


class MemoryPort(Protocol):
    """Interface that S3 Context Compiler requires from S7 Memory."""

    async def get_memory_tokens(
        self, workspace_id: str, query: str, budget: int
    ) -> list[MemoryFragment]:
        """Retrieve memory fragments within the given token budget."""
        ...

    async def propose_candidate(self, candidate: MemoryCandidate) -> str:
        """Submit a memory candidate for dedup/merge; returns candidate ID."""
        ...
```

调用约束：Context Compiler 在 Retrieve primitive 期间调用 `get_memory_tokens`，budget 来自
TaskNode 的 token budget 减去已分配 authoritative/conversational 份额后剩余值；返回 fragment
的 classification 不得高于当前 Run 的 data classification ceiling，否则 fail closed 拒绝加载。
- **压缩上界与恢复路径**（[ADR-007](../docs/DECISIONS.md#adr-007)）：降级链每级最多 `max_compaction_attempts`
  （默认 3）次，达上界即 refusal，不允许循环压缩；尝试记录写入 manifest。refusal 有两条留痕出口：
  显式授权降级（展示将丢弃的 authoritative 清单 → 确认 → 新 Attempt 标 `authoritative_waived` → 该
  Attempt 的 claim 强制降级为 Inference）与 epoch 回退（改选更大 context profile，生成新 TransitionManifest）。
- **context fit 计数**（[ADR-002](../docs/DECISIONS.md#adr-002)）：每个 ModelProfile 声明
  `authoritative_count | verified_local_count | calibrated_estimate` 三级之一，未知即 fail closed 走最保守
  档。`context_length_exceeded` 类错误强制映射为 `context_refusal`，不计入 provider failure，并触发该
  profile 的 estimator 重标定。

## 4. Model contract

- 三 transport 正常化 tool calls、structured output、stream delta、finish/error/usage。
- profile claim 与 attestation 分离；未知 required capability 拒绝。
- Router 依次做 compliance/capability/context/quality/[optional spend guard]/latency，并记录每级
  candidate/rejection reason。前四级是硬门禁；spend guard 默认关闭——token 支出的默认定位是 ROI 指标
  而非阻断条件。
- **token ROI 指标**（[ADR-002](../docs/DECISIONS.md#adr-002)）：按 Run/trajectory 归集 `weighted_tokens`、
  `authoritative_token_share`、`evidence_per_kilotoken`、`recoverable_reload_waste`、`context_utilization`、
  `compression_ratio`、`cost_per_completed_task`，进入 S9 sealed artifact 并作为 Context Compiler 消融的因变量。
- **provider 中立**（[ADR-010](../docs/DECISIONS.md#adr-010)）：新增 endpoint 只能通过新增 EndpointProfile +
  attestation 分级完成，不改 Core 代码；architecture test 断言 Core 与 transport 均不含任何具体 endpoint
  名称分支。
- **分级信任与运行时注册**（[ADR-011](../docs/DECISIONS.md#adr-011)）：`endpoints.yaml` 是已审查档案库
  而非允许清单。`OPENAI_BASE_URL/MODEL/API_KEY` 是部署期 override，优先级高于配置文件；未登记
  endpoint 按 `runtime_registration_floor` 落入 `unverified` 档（能用，但 classification_ceiling 降至
  PUBLIC、能力全 unknown、不可支撑对外声明），并在首次使用时写 canonical event + audit。
  数据门禁为 `context 实际分类 ≤ endpoint classification_ceiling`，其中 ceiling 由
  `network_zone`（internal/external/unknown）决定——内网自部署应获得高于公网第三方的许可。
- **两类热切换**：同 endpoint 换 model 走新 ModelProfile + 新 Attempt，egress 策略不变；跨 endpoint
  换 model 必须重新 egress 检查（目标 ceiling 低于当前上下文实际分类则拒绝）、重新 attestation，
  两类跨 epoch 时都生成 `TransitionManifest` 并记录 `cache_invalidated` 与预估重建成本。
- fallback 默认 off；启用时新 Attempt/new ContextManifest 并在 UI/Audit 显示。
- live Connection 与 S1/S4 Connection interface 对齐；本阶段可先提供 secure injected reference connection，
  S4 完成用户/工作负载 OAuth 和 secret management。

## 5. Required tests

- 每 transport：golden request/response/stream/tool/error fixtures、schema corruption、429/5xx/timeout/cancel。
- actual-wire：logic IR/body/header-redaction/tamper；捕获前不能生成 success manifest。
- **wire tamper corpus**：在 adapter 与 transport 之间注入四类篡改——追加隐藏 system message、静默截断
  tool schema 字段、重试时替换 body、传输层截断——断言 pre-send gate 全部拒绝发送，且不存在无对应
  manifest 的发送。流式与非流式路径各跑一遍。
- token counter：三级计数契约；`context_length_exceeded` 映射为 refusal 而非 provider failure；用回传
  usage 回归本地估算器后误差分布收敛。
- compaction：注入超大 tool 输出，断言达尝试上界后进入 refusal 而非循环；断言 `authoritative_waived`
  路径的 claim 无法标记为 Fact；断言 epoch 回退生成新 TransitionManifest 而非复用旧 manifest。
- architecture：Core/transport 无具体 endpoint 名称分支。
- endpoint 信任档：未登记 base_url 可正常接入且落入 `unverified`（断言 ceiling=PUBLIC、能力 unknown、
  不可支撑对外声明）；`internal` zone 的 endpoint 获得高于 `external` 的 ceiling；提升 floor 只能经
  Security Admin 显式动作且写 audit；env override 优先级高于 `default_endpoint_id`。
- 热切换：同 endpoint 换 model 不重做 egress；跨 endpoint 换 model 在目标 ceiling 低于当前上下文实际
  分类时**拒绝切换**；两类跨 epoch 均生成 TransitionManifest 且记录 `cache_invalidated`。
- reducer/context property：arbitrary event rebuild、inventory preservation、compression order、refusal。
- transition：two-phase commit、old epoch remains on failure、A-prefix once、directional edge identity。
- router：unknown capability/expired attestation/data classification/budget/no silent fallback。
- hidden reasoning：正文不出现在 PG/Object/Temporal/Redis/log/trace。
- Runtime：Plan/Analyze/Synthesize formal handlers 调用 model Activity，结果经 Attempt/canonical event 提交；
  handler version/missing/cancel/error 走 S2 registry。

## 6. Eval and Gate

旧 12-chain/edge 只作为 `handoff-pilot`，不做显著/等价结论。正式 handoff suite 在 pilot 后做 power
analysis 再冻结；结构 preservation gate 与 continuation quality 分开。

```bash
uv run pytest tests/contract/models tests/unit/context -q
uv run pytest tests/integration/context tests/security/model_egress -q
uv run zhiwei verify context tests/fixtures/context --all
uv run zhiwei models attest --mode fixture --all
# live probe only by explicit operator after preflight:
uv run zhiwei models attest --mode live --endpoint opencode-go --model <id>
```

## 7. Claim boundary

可声明具体 transport/profile/date 的 fixture 或 live attestation、manifest tamper 检测和 context refusal。
禁止声明任意模型兼容、hidden reasoning 迁移、无感切换或用结构 100% 代替 handoff 效果。
