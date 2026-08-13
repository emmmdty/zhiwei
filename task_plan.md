# ZhiWei 冻结设计与开发交接计划

## 目标

把知微定义为一个可持续扩展、可本地完整运行并可形成生产参考的企业 Agent Core。Ask 和 Discover 是
两个深度 Agent Apps，ChangeBrief 是第三个通用性证明；用户、团队、知识、工具、模型、上下文、记忆、
编排、验证、评测和组织治理必须进入同一产品闭环。

本轮只重构设计、规格和实施计划，不实现 `src/`，不修改冻结 eval 资产，不调用真实 LLM。

## 已批准决策

- Agent Core 是主产品；SolutionPack 是可版本化产品单元；Core 禁止 App 名称分支。
- Web-first 多用户 Organization/Workspace 产品，提供 Workbench、Cases、Knowledge、Studio、Capability
  Hub、Memory、Admin 和 API/SDK。
- Ask 跨文档、代码/GitHub、DB/API 与 scoped Memory；Discover 从 trigger/source delta 到 Hypothesis、
  Case、approval/ActionReceipt 和 HumanResolution；ChangeBrief 验证第三 App 可扩展。
- PostgreSQL 保存业务真相，Temporal 保存执行位置，OpenSearch/Redis 是派生状态，S3-compatible store
  保存 source/artifact；local-product 默认 Garage，不再以已 archive 的 MinIO 为默认。
- Context/Knowledge/Memory/Profile 分层；Context Compiler 绑定 actual wire body，authoritative 完整或拒绝。
- Capability Hub 支持 MCP、OpenAPI、Agent Skills、SDK、Agent-as-tool 的发现、准入、Connection/OAuth、
  sandbox、版本/更新/撤销；不是内部固定工具 Demo。
- OIDC/SCIM + Organization/Workspace/Group/User/ServiceAccount/AgentIdentity + RBAC/OPA/RLS。
- 现有 120/112/57 FactQA 与 Risk 资产保留历史边界，新建 code/GitHub/cross-source/ACL、Memory、Ask、
  Discover、安全、恢复和性能 suites；评测走生产 Runtime。
- local-product 必须走真实 PG/Temporal/OPA/RLS/Knowledge/Memory/Tool/Evidence，只替换外部系统/LLM；
  production-reference 通过实测再升级声明。

## 本轮状态

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| 产品与角色澄清 | complete | 用户逐节确认九个设计部分 |
| 技术检索与选型 | complete | 总设计 §15 记录官方/研究依据与选择边界 |
| 唯一事实源重写 | complete | 企业 Agent Core 冻结总设计 |
| 面向读者文档 | complete | README + PRODUCT/ARCHITECTURE/DATA_MODEL/API/PERMISSIONS 等已统一 |
| 阶段规格 | complete | `specs/s0` 到 `s11` 共 12 个能力门 |
| 任务级计划 | complete | 12 份 writing-plans 格式计划，包含精确文件、RED/GREEN、Gate、提交边界 |
| 静态/资产回归 | complete | 链接/阶段一致性、Ruff/Pyright、110 validator、21/21 checksum、determinism 全绿 |
| 独立实现就绪审查 | complete | 修复 Eval/Release 阶段倒置、file/package 冲突、RBAC/Secret/handler/runner/CLI 等硬阻塞 |

## 成功标准

1. README、总设计、专题文档、spec 和 plan 对产品定位、事实源、阶段与状态没有冲突。
2. 新开发 Agent 可从 S0 开始，每项 Task 明确读哪些文件、改哪些路径、先写什么失败测试、运行什么命令。
3. 所有产品类别都有至少一个 reference integration、用户 journey、安全/故障测试和可发布 artifact，
   不以空 registry/UI 保留名义完整性。
4. 旧 eval 资产不因产品扩展被篡改，也不被外推为未测能力。
5. 未验证的技术风险有 spike 和合法降级；降级不删除产品需求，也不假装安全实现。
6. 设计交接完成后直接进入开发，不继续堆叠同层文档；任何架构变更必须有最小反例和迁移影响。

## 当前下一步

冻结重构已完成，机制级决策已在 `docs/DECISIONS.md`（ADR-001 至 ADR-010）补齐并回写各阶段 spec。

由项目所有者建立并确认初始 Git baseline 后，开发 Agent 从
`docs/superpowers/plans/2026-08-12-s0-foundation.md` 逐 checkbox 执行，不跳 Gate，不自动调用 live 模型。

两个 P0 spike（wire capture 保真性、token estimator 校准）不依赖任何前置阶段，可先于 S0 执行——
其中 wire capture 的结论直接决定 S3 Context Compiler 的 capture 点设计，越早验证越省返工。
