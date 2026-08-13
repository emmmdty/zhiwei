# 系统架构

> 详细契约以[冻结总设计](superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md)为准。
> 本文给开发者提供组件边界、依赖方向和运行路径。

## 1. 总体视图

```text
Browser / API Client
        │ OIDC BFF + REST commands + SSE
        ▼
FastAPI Application Layer
  ├── Identity / Organization / Workspace / Policy / Audit
  ├── Agent Registry / Studio / Release / Eval
  ├── Knowledge / Memory / Cases / Capability Hub
  └── Run command / projection APIs
        │ transaction + outbox
        ▼
PostgreSQL ───────────────► Outbox Dispatcher ─────────► Temporal
  business truth                 │                         durable position
  RLS / canonical events         ├──► Redis/SSE
  versions / manifests           └──► OTLP
        ▲                                                   │
        │                                                   ▼
Object Store ◄──── Artifact/Evidence ───── Agent Worker / Integration Worker
Source Ledger                            ├── Task Graph / reducer / Context Compiler
        ▲                                ├── Model Gateway (3 protocols)
        │                                ├── Knowledge Planner / Memory retrieval
External sources ─ Sync/Index Worker ────┼── Capability/Tool Gateway
        │                                └── Evidence / Approval / ActionReceipt
        └──────── OpenSearch / Context Graph
```

## 2. 分层与依赖规则

```text
contracts/domain
      ↑
application services / ports
      ↑
adapters: api, persistence, temporal, model, search, tools, object store
      ↑
composition roots: api, workers, cli
```

- domain 不导入 FastAPI、Temporal、SQLAlchemy、OpenSearch、provider SDK 或前端 DTO。
- workflow 只编排 application commands/activities，不保存第二份业务规则。
- Solution Pack 依赖 Core ports；Core 禁止导入 `solution-packs/ask|discover|change-brief`。
- Eval 调用 production application interface，只替换 external bindings。
- Provider 原始 payload 在 adapter 转成 typed contract，不能跨边界扩散。

## 3. 目标仓库结构

```text
apps/web/
  src/{app,routes,features,components,api,state,renderers}/
src/zhiwei/
  api/             REST/SSE/BFF、request DTO、PEP
  identity/        OIDC/SCIM、org/workspace/membership/principal
  agents/          AgentDefinition、SolutionPack、TaskGraph contracts
  runtime/         reducer、scheduler、attempt、delegation、approval
  context/         state classes、compiler、budget、manifests
  models/          profiles、router、three transports、usage
  knowledge/       Source Ledger、connectors、planner、indexes、graph
  memory/          records、policy、candidate、retrieval、forget
  capabilities/    catalog、admission、connections、MCP/OpenAPI/Skill/SDK
  evidence/        refs、canonical values、claims、bundles、verify、receipts
  cases/           Case lifecycle、cross-App sharing、resolution
  evals/           suites、runner、scorers、statistics、sealed reports
  policy/          RBAC/OPA inputs/PEPs、classification、egress
  telemetry/       OTel、cost ledger、failure taxonomy
  persistence/     SQLAlchemy repositories、Alembic、outbox、RLS
  object_store/    POSIX/S3 manifest protocol
  secrets/         envelope/Vault-KMS secret backend；业务层仅持 opaque ref
  workflows/       Temporal workflows/activities
  workers/         composition roots
  cli/ contracts/
solution-packs/{ask,discover,change-brief}/
deploy/{compose,kubernetes,observability}/
tests/{unit,contract,integration,security,e2e,fixtures}/
```

## 4. Agent Run 路径

1. API 校验 session、principal、Workspace、AgentVersion、input schema 和 idempotency key。
2. 同一 PG 事务写 `Run(CREATED)`、initial canonical events、budget reservation 和 workflow outbox。
3. Dispatcher 以确定性 workflow id 启动 Temporal；重复消息幂等。
4. Workflow 调用 activity 读取 canonical head，Task scheduler 选择 ready nodes。
5. Retrieve/Memory/Tool/Delegate 都经过当前 policy；结果先持久化 artifact/event，再推进 task。
6. 模型节点用 Context Compiler 生成 IR，adapter 序列化后由 pre-send gate 绑定 actual wire digest。
7. 写工具先写 intent，需要时进入 Approval；执行后写 ActionReceipt 或 `effect_unknown`。
8. Synthesize 生成 typed output；Evidence Gate 验 Claim，再提交 final/partial/abstain。
9. 同事务写 terminal state、cost reconciliation、audit/stream outbox；UI 从 SSE 收增量并可从 REST 重建。

## 5. Knowledge 路径

1. Connection/SourceVersion 通过 Capability/Knowledge 准入，固定 auth scope、ACL 和 classification。
2. webhook 创建 sync intent，reconciliation 保证最终补齐；worker 按 watermark 拉取。
3. 原件写 Object Store，PG Source Ledger 提交 digest、locator、ACL、observed/valid time。
4. source-specific parser/indexer 生成文档结构、表格单元、SCIP/tree-sitter code graph、GitHub/DB schema。
5. OpenSearch 建 hybrid index；Context Graph 写 PG typed edges；两者都可从 Ledger 重建。
6. Query 时 ACL pre-filter，按 source-native signal 检索、hydration 后 re-check，返回带 snapshot ref 的候选。
7. Evidence Service 只接受冻结 Ledger ref；source 更新只标旧 Evidence stale，不改历史结果。

## 6. Context 与模型路径

```text
canonical head
 + Agent/Profile/Policy/current Task
 + authorized Knowledge + scoped Memory + Tools
 → inventory and priority
 → compression/task split/model choice
 → Context IR
 → openai_chat | openai_responses | anthropic_messages
 → capture serialized body
 → classification/policy/inventory/digest pre-send checks
 → send and normalize stream/tool/errors
```

`TransitionManifest` 证明 epoch 交接输入不变量；`ContextManifest` 证明每次调用实际发送内容。
结构性 preservation gate 与 task continuation quality 分开测。

## 7. Capability 路径

```text
catalog discover/import
 → quarantine + immutable source digest
 → parse/schema/license/SBOM/vulnerability/network/effect/idempotency tests
 → organization approval/publish
 → Workspace Connection (user-delegated or workload)
 → AgentVersion binding + release gates
 → invocation policy/approval/short credential/sandbox
 → Observation | ActionReceipt
```

MCP resource 进入 Knowledge；MCP/OpenAPI tool 进入 Tool Gateway；prompt 只能成为候选 Skill；script 只有
注册成受隔离 Tool 才可执行。Connection 与 Provider 分开，撤销 connection 不删除 provider version。

## 8. 事务与一致性边界

- PG 内：resource mutation、canonical event、projection、audit/stream/workflow outbox 同事务。
- PG ↔ Temporal：transactional outbox + deterministic workflow/signal id + activity idempotency。
- PG ↔ Object：temporary upload、digest verify、immutable object key、manifest commit；orphan reconciler。
- PG ↔ OpenSearch：index outbox + versioned document；ACL/revoke 优先；查询时二次鉴权。
- 外部写系统：intent + provider/caller idempotency；无法确认时 `effect_unknown`，不自动重放。

不使用分布式事务假装这些系统同时提交。

## 9. 安全边界

- API、runtime、retrieval、memory、model egress、tool、artifact、SSE 均为 PEP。
- 有效权限是 user/service、AgentIdentity、AgentVersion、binding、Workspace/Org policy、ACL、connection 和
  delegation 的交集。
- PostgreSQL `FORCE RLS`、per-org search/object/secret partition 是纵深防御。
- capability runner 隔离第三方代码；API/Agent Worker 不执行未知宿主命令。
- secret 仅在单次调用前解密，所有事件/trace/artifact 只保存引用。
- tool/retrieval/memory 内容是不可信输入，不能覆盖 platform/profile policy。

## 10. 进程与扩展

| 进程 | 可扩展维度 | 禁止承担 |
| --- | --- | --- |
| API/BFF | HTTP 并发 | 长 LLM/tool/index 任务 |
| Agent Worker | Run/Task throughput | 未隔离第三方代码 |
| Integration Worker | source/tool I/O | 权威 scheduler |
| Index/Sync Worker | source partition | 用户请求 session |
| Eval Worker | suite/run partition | 评测专用业务实现 |
| Capability Runner | sandbox profile | 组织控制面 mutation |
| Outbox Dispatcher | topic/partition | 业务判断 |

先保持一个代码库和稳定 contract；只有当独立扩容、故障域或信任边界已有测量证据时才拆服务。

## 11. 部署拓扑

`test` 追求反馈速度；`local-product` 必须具有真实 OIDC、多用户、PG、Temporal、OPA/RLS、OpenSearch、
Garage、Redis、OTel 和 reference integrations；`production-reference` 把有状态依赖替换为企业托管/HA
服务并提供 migration、backup/restore、network policy 与 capacity report。未通过 S11 前，拓扑图不能
转化为生产可用性声明。
