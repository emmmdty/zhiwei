# S0-S11 能力门

> Roadmap 只表达依赖和可验证出口，不评估周期。精确契约见 `specs/s*.md`，任务步骤见
> `docs/superpowers/plans/`。

## 北极星

每一阶段都必须让某条真实 reference journey 增加可观察行为；目录、抽象、表、页面和 fixture 截图不是
能力门。任何 public claim 只能由 Gate artifact 解锁。

「可观察行为」分两类，两类都必须能由该阶段的 Gate 命令直接演示，不得以“为后续阶段做准备”为由
交付无法演示的中间层：

- **平台 journey**（S0-S2）：操作者可执行的完整动作——迁移与自检、登录与授权决策、durable Run 的
  创建/审批/取消/崩溃恢复。此时模型尚未参与，可观察对象是平台自身行为。
- **Agent journey**（S3 起）：终端用户可见的 Agent 行为——上下文编译与跨模型投影、检索、证据、
  工具动作与主动发现。

因此 S0 的 sealed empty Run、S1 的多角色授权 journey、S2 的 fixture Run 崩溃恢复都是合格能力门；
而“建好了表但没有任何动作能跑通”不是。

## 阶段

| 阶段 | 目标 | 必须产物 | Gate |
| --- | --- | --- | --- |
| S0 Foundation | 建立共享 contracts、PG/Object/outbox、最小 Eval core、配置与测试底座 | package、migration、artifact protocol、Dataset/Suite/EvalRun/sealing、现有 eval adapter、test stack | `make evals/determinism` 不回归；事务/腐损/迁移；sealed empty Run/EvalRun |
| S1 Tenancy & Policy | 建立真实多组织身份授权纵切 | OIDC BFF、Org/Workspace/Group/User/ServiceAccount、RBAC/OPA/RLS、audit、最小 Web shell | 多角色 journey；IDOR/RLS/CSRF/revoke/OPA-down fail closed |
| S2 Runtime | 运行版本化 Agent/SolutionPack/Task Graph | Temporal Workflow/Activities、fixture planner、approval/cancel/retry/SSE | crash/replay/effect_unknown；10 并发无跨租户或丢终态 |
| S3 Models & Context | provider-neutral 状态和三协议模型运行 | profiles/router/transports、reducer、Context Compiler、Transition/Context manifests | contract fixture；actual-wire tamper；完整 inventory 或 refusal |
| S4 Capability Hub | 接入可持续扩展的外部能力 | catalog/admission、MCP/OpenAPI/Skills/SDK、Connections、Tool gateway/sandbox | 每类 reference provider；OAuth/SSRF/injection/secret/drift tests |
| S5 Knowledge | 建立 source-native 企业知识 | Source Ledger、doc/table/code/GitHub/DB、OpenSearch、Context Graph、ACL/freshness | 跨源 retrieval；ACL/revoke/freshness；code/GitHub 新 suite |
| S6 Evidence & Ask | 交付第一个完整 Agent App | Evidence types/verifier、Ask SolutionPack、Workbench Ask renderer、Case creation | cross-source answer；Fact/Quote 全覆盖；tamper/partial/abstain |
| S7 Memory | 交付用户/团队/Case 长期记忆 | MemoryRecord/policy/retrieval、Memory Center、case timeline | candidate/confirm/conflict/revoke/delete/poison；Discover 无个人 memory |
| S8 Discover & Actions | 交付主动发现到处置闭环 | DiscoveryProgram、detectors/exploration、hypothesis/falsification/dedupe、Case/action | trigger→HumanResolution；approval/ActionReceipt；Risk 口径无夸大 |
| S9 Eval/Release/Telemetry | 建立同构评测、声明和运营治理 | layered suites、sealed run、Claim Registry、OTel、cost/router、canary/rollback | blind/external/fault；release 阻断无 artifact 声明 |
| S10 Studio & Third App | 交付完整构建面并证明 Core 通用 | Studio、Hub/Knowledge/Admin journeys、ChangeBrief SolutionPack | Builder build-eval-publish-use；新增 App 无 Core 专用分支 |
| S11 Production Reference | 交付可部署/恢复/测量参考 | local-product Compose、K8s reference、migration、backup/restore、load/fault/security reports | clean install；故障恢复；只发布实测 SLO/数字 |

## 依赖

```text
S0 → S1 → S2 → S3 → S4 → S5 → S6 → S7 → S8 → S9 → S10 → S11
```

可以提前制作后续 suite 数据、UI renderer 或 reference fixture，但不得冻结依赖尚未实现的 schema，也不得
以并行工作绕过前置 Gate。每阶段都使用前一阶段的真实 application path，不创建演示专用旁路。

## 通用 Definition of Done

- 行为先有失败测试，domain invariant 有 property test，adapter 有 contract test。
- 每项 schema/resource/event/manifest 有版本、canonical JSON、digest 和兼容/迁移策略。
- mutation 有 idempotency/CAS/audit；租户资源有 API policy + repository tenant key + RLS。
- 外部 I/O 有 timeout、budget、classification、structured failure、fixture/replay 和 no-secret scan。
- UI 页面必须完成用户 action、loading/error/empty/permission/state recovery，不接受空壳。
- Run/Eval 全部注册单位有 terminal status；partial 可恢复但不可 seal/release。
- live 只由操作者显式触发，CI 和 Compose startup 不调用真实 LLM。
- 文档/README 的功能状态和数字由 Claim Registry/artifact 更新，不靠人工润色升级。

## 失败处理

实施发现设计不成立时，先保存最小反例、失败环境、受影响 invariant、候选替代与数据迁移影响。允许
替换 adapter 或缩小“已验证”范围；不允许静默删掉 Tool/Memory/Multi-user 等产品类别，也不允许用
空接口保留名义完整性。
