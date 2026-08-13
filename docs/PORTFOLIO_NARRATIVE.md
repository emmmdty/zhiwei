# 对外叙事与声明注册表

> 目标是准确展示一个可持续扩展的企业 Agent Core，而不是为了求职把产品重新裁成小 Demo。当前
> `src/` 未实现，所有未来态文案只能作为模板。

## 1. 正确叙事顺序

1. **完整产品闭环**：多用户 Organization/Workspace 中，Builder 能导入 Knowledge/Tool/MCP/Skill，
   构建、评测、发布 Agent；用户在 Web 使用，审批和审计可追踪。
2. **Agent Core 状态模型**：Task Graph + durable Run；Context/Knowledge/Memory/Profile 分层；canonical
   state 跨模型编译且绑定 actual wire body。
3. **可验证输出**：Fact/Quote 到 Source Ledger/Evidence，写动作到 ActionReceipt；同一 runtime 生成评测、
   trace、成本和发布声明。
4. **两个深 App + 一个通用性证明**：Ask 展示跨文档/代码/GitHub/DB 研究，Discover 展示主动发现到
   HumanResolution，ChangeBrief 证明 Core 可扩展。

不要再以“120 题 + Evidence Contract + RiskInsight”开场。那会让面试官先形成 RAG/Text-to-SQL 套壳
判断，再把平台能力看成后补架构。

## 2. 三分钟演示主线（完整实现后）

```text
00:00-00:20  登录组织 Workbench，展示 Ask/Discover/ChangeBrief、Cases 与当前真实 release status
00:20-00:55  Ask 回答跨代码 PR + 规范文档 + 业务 query 的问题，点一个 Fact 直达 Evidence locator
00:55-01:15  切换模型，展开 ContextManifest actual-wire digest；篡改 bundle 后 verify 明确失败
01:15-01:55  Discover 因 source delta 生成带支持/反证的 hypothesis，人工 triage 建 Case
01:55-02:20  审批 MCP/OpenAPI 工单动作，显示 input digest、Connection/Policy 和 ActionReceipt
02:20-02:45  Studio/Capability Hub 展示同一 Agent 的 Knowledge/Memory/Tools/Evals/Release 绑定
02:45-03:00  ChangeBrief 使用第三个 Pack；Claim Registry 标明哪些是 live/offline/未验证
```

真正产生“这不是 Demo”的镜头是：同一个 Discover hypothesis 进入 Case，Ask 补跨源证据，高风险工具
经审批执行并生成 ActionReceipt；Studio 中能看到这些能力来自版本化公共绑定，而不是硬编码按钮。

## 3. 声明注册表

| 最低 Gate | 可以声明 | 仍禁止声明 |
| --- | --- | --- |
| 当前资产 | 120/112/57、110 validator、21 资产确定性、四类故障注入 | Agent 可用、模型/检索/risk 效果、多租户已实现 |
| S0 | PG/Object/outbox 基础和 sealed empty Run 有本地测试 | 企业平台可用 |
| S1 | 指定版本下 OIDC、多组织、RBAC/OPA/RLS 安全 suite 通过 | 生产级多租户、安全无漏洞 |
| S2 | fixture runtime 可恢复地完成 typed Task Graph/approval | 真实模型 Agent 效果 |
| S3 | 三协议 contract 和指定 live attestation；handoff pilot 结果 | 任意模型无损切换、hidden reasoning 迁移 |
| S4 | 指定 MCP/OpenAPI/Skill reference 的准入/鉴权/隔离证据 | 任意生态能力安全兼容、公共无审核市场 |
| S5 | 指定 doc/code/GitHub/DB suite 的 retrieval/ACL/freshness 结果 | 任意企业知识全覆盖 |
| S6 | 指定 Ask suite/run 的 cross-source quality 和 Evidence validity | verifier 证明推理正确、所有问题可答 |
| S7 | memory suite 的确认/冲突/撤销/删除/隔离结果 | 自动学习所有用户习惯、永不污染 |
| S8 | Discover planted/blind/human 分开报告，动作闭环可审计 | 真实经营风险预测准确率、自动决策正确 |
| S9 | sealed eval、成本/延迟/失败和 Claim Registry artifact | fixture/replay 冒充 live |
| S10 | 第三个 App 未改 Core 的 architecture test 与完整 Builder journey | “无限扩展” |
| S11 | 固定环境的 install/load/fault/backup/restore/security report | 未测环境的 HA/SLO/production-ready |

## 4. 简历模板

以下只在相应 artifact 出现后替换方括号：

> **知微 ZhiWei — 可验证企业 Agent Core（Python/FastAPI/Temporal/PostgreSQL/React）** 设计并实现
> 多组织 Agent 平台，将 Task Graph、Knowledge、user/team/case Memory、MCP/OpenAPI/Skills、三协议模型
> 与 RBAC/OPA/RLS 纳入同一 durable Run；在 `[fault/security/concurrency artifact]` 中完成 `[结果]`。
> 以 Ask/Discover/ChangeBrief 三个 Solution Pack 验证通用性：Ask 跨文档/代码/GitHub/DB 输出 claim-level
> Evidence，Discover 从 source delta 到审批 ActionReceipt；`[sealed eval]` 报告 `[质量/成本/延迟/失败]`。

如果只能写两行，保留“第三个 App 不改 Core”“跨源 Evidence”“Discover 到 ActionReceipt”及对应数字，
删除空泛技术栈罗列和旧资产规模。

## 5. 45 分钟技术深聊

- 0-8 分钟：为什么 Agent Core 而不是 Ask/Discover 硬编码；SolutionPack 与第三 App architecture test。
- 8-18 分钟：PostgreSQL canonical truth、Temporal durable shell、Task Graph、outbox、effect_unknown。
- 18-28 分钟：Context Compiler、四类状态、跨模型 manifest、实际 wire binding、结构门与效果指标分离。
- 28-36 分钟：Source-native Knowledge、代码/GitHub/DB、ACL/freshness、Knowledge/Memory 边界。
- 36-42 分钟：Capability Hub 的 MCP OAuth、Skill script sandbox、Connection 与权限交集。
- 42-45 分钟：Evidence/Action 与同构评测、现有 120 题/Risk 资产的局限。

## 6. 终面压力题与必须展示的证据

1. **“这和 LangGraph/LiteLLM/RAG 平台差在哪？”** 展示 PostgreSQL/Temporal truth boundary、Context
   actual-wire binding、source-native Evidence 和 Capability governance，而不是说“自己实现”。
2. **“新增 Agent App 会不会改 Core？”** 展示 ChangeBrief 只新增 SolutionPack/View、architecture import
   test 和发布 artifact。
3. **“MCP/Skill 从哪里来，谁授权？”** 展示 discover→quarantine→admission→Connection→binding→tool intent
   全链与 user-delegated/workload token 隔离。
4. **“记忆如何避免污染和泄露？”** 展示 candidate/confirm/provenance/conflict/revoke/delete、profile scope 和
   Discover 无个人 memory 的拒绝测试。
5. **“Risk 是不是六条规则自建自测？”** 明确 RiskInsight 只是 Numeric Detector Pack，展示 source-delta
   Discover、反证、blind/human eval 和真实 Case/Resolution，不拿 planted recall 冒充价值。
6. **“hash 能证明答案正确吗？”** 必须答不能；展示 typed value/locator/claim span 只证明复算链，语义和
   inference 由独立 scorer/人评承担。
7. **“authoritative 100% 是不是造指标？”** 说明它是投影成功/拒绝的结构 invariant；展示 task continuation
   和 handoff quality 是独立 suite，12-chain 只作 pilot。
8. **“生产上线的证据是什么？”** 只展示固定环境的 install/load/fault/restore/security artifact；未通过
   S11 时明确回答尚未验证。

## 7. README 首屏顺序

1. Agent Core 一句话定位与当前真实状态。
2. Ask/Discover/ChangeBrief 的产品闭环图。
3. 三个硬证据入口：cross-source Evidence、approved ActionReceipt、Context actual-wire manifest。
4. Capability Hub/Studio 的真实扩展路径。
5. sealed quality/cost/latency/failure 表；没有 live 就不放数字。
6. 本地完整产品启动和 fixture/live 标签。
7. 局限、架构和历史评测资产下沉。

## 8. 当前状态的对外处理

当前仓库只有设计和基准资产。公开时应写“architecture/specification + frozen eval assets，implementation
not started”；不要把总设计的篇幅当作完成度——文档行数不是 Gate artifact，因此也不应出现在任何对外
材料里。设计重构结束后应直接进入 S0 开发，因为下一条有价值的证据只能来自真实 vertical slice、
测试和 artifact，继续扩写同层设计不会提升可信度。
