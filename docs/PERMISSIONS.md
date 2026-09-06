# 身份、权限与安全边界

> 本文是实现检查清单，不取代[冻结总设计](superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md)。

## 1. 租户与主体

- `Organization` 是最高业务隔离边界，`Workspace` 是协作、资源、预算和策略边界。
- 个人空间仍由 Organization 管理；不共享跨 Organization Resource/Connection/Memory。
- Principal 为 User、ServiceAccount、AgentIdentity。一次 Run 同时记录 trigger principal、Agent identity、
  AgentVersion、organization/workspace、purpose 与 delegation chain。
- 外部用户稳定键为 OIDC `(issuer, subject)`，不是 email；SCIM/JIT 生命周期不能绕过应用 Membership。

## 2. 登录与会话

- OIDC Authorization Code + PKCE；Web 使用 BFF，token 只在服务端 session。
- 浏览器 cookie 必须 `Secure + HttpOnly + SameSite`，执行 state/nonce/PKCE、CSRF、session fixation、
  logout/revoke 和 absolute/idle expiry 测试。
- local-product 使用 Keycloak；生产允许标准 OIDC/SCIM IdP。ZhiWei 不自建密码库。
- SDK/后台触发使用 service identity；用户 delegated OAuth token 不得被 Discover 等后台 Agent 复用。
- S1 即交付通用 `SecretBackend` port 与 local AES-GCM envelope 实现。OIDC access/refresh token 只以密文
  保存在 principal/session 级 `AuthSession`，AAD 绑定 session/issuer/subject/version；首次登录不要求已有
  Organization，active org 在登录后按 Membership 选择。S4 复用此 port 保存 Connection credentials，并对
  后者使用 per-org/workspace/binding/version AAD、增加 Vault/KMS adapter。

## 3. 授权模型

基础角色：Organization Owner、Security Admin、Capability Publisher、Workspace Admin、Agent Builder、
Memory Steward、Approver、Member、Auditor。产品文档中的 `Builder` 就是 `Agent Builder`，不是
`Workspace Admin` 的别名。RBAC 只表达职责，OPA 决定 resource、purpose、classification、risk、time、
connection、delegation 等上下文。

```text
effective permission = trigger principal permission
                     ∩ AgentVersion declared permission
                     ∩ CapabilityBinding narrowed scope
                     ∩ Knowledge/Memory ACL
                     ∩ Connection scopes/audience/subject mode
                     ∩ Workspace/Organization active policy
                     ∩ delegation budget/scope
```

发布时的策略版本用于解释发布，不冻结一份可绕过撤权的 allow。每次数据读取、模型出站、secret 解密、
工具调用和 approval 后实际执行前都重新判断当前 policy/membership/resource status。

### 3.1 冻结 RBAC 矩阵

`own` 表示仅本人资源，`workspace`/`org` 表示角色绑定作用域；`policy` 表示仍须 OPA/ACL/分级允许。
未列动作默认拒绝。一个主体可有多角色，角色权限先取并集，再与资源/Agent/Connection/ACL/委托权限取交集。

| 资源/动作 | Org Owner | Security Admin | Capability Publisher | Workspace Admin | Agent Builder | Memory Steward | Approver | Member | Auditor |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Org、IdP、SCIM、角色绑定 | org 管理/委派 | 配置安全策略；不可改 Owner | — | workspace 成员管理 | — | — | — | 读自身 | 读审计元数据 |
| Workspace 策略、预算、保留 | 配置；workspace 配置（Group 创建等，ADR-014） | 安全/出站 hard policy | — | 配置 workspace 非 hard policy | 读 | 读 memory policy | 读 approval policy | 读适用策略 | 读/导出受 policy 限制 |
| Knowledge Source/ACL | 无隐式正文读取；可委派 | 分类/egress/紧急 suspend | — | 创建/同步/授权/禁用 | 绑定到 draft、按 ACL 调试 | 按 ACL 读来源 | — | 仅经已发布 Agent/显式 ACL | 按 ACL 读 provenance |
| CapabilityVersion | 无隐式准入 | 高/关键风险二审、suspend/revoke | 导入/检查/测试；低中风险准入/发布 | 绑定 workspace | 绑定已发布能力到 draft | — | — | 浏览可用目录 | 读 admission/audit |
| Connection/secret | 不读明文 | revoke/安全元数据，不读明文 | 定义 credential requirement，不读明文 | 创建 workspace service Connection、rotate/revoke | 创建/撤销 own delegated Connection | — | — | 创建/撤销 own delegated Connection | 只读状态/指纹 |
| Agent draft/sandbox/eval | — | 安全拒绝/挂起 | — | 读/委派 Builder | 读（own workspace，ADR-015）；创建/编辑/运行/提交发布 | — | — | — | 读版本与 Gate |
| Agent publish/canary/rollback | — | 对 hard security Gate 否决 | — | 复核并发布/回滚 | 只能 request；不能发布本人最后编辑版本 | — | — | — | 读 manifest |
| Run/Case/Artifact | 不因角色自动读 | incident 时按 break-glass policy | — | 管理 workspace lifecycle，不自动读正文 | 运行 sandbox、按 ACL 读 | 按 ACL 读 Case memory | 只读待审批所需最小上下文 | 运行 published、管理可见 Case | 按 ACL 只读/导出 |
| Team Memory | — | 安全 quarantine/revoke | — | 配置策略，不确认内容 | 提交 candidate | confirm/correct/conflict/revoke | — | own/case candidate、读获授权团队记忆 | 按 ACL 读 provenance |
| Tool Approval | — | hard deny/紧急 revoke，不作业务批准 | — | 配置 approver group | request | — | approve/reject/replace | request | 只读记录 |

### 3.2 职责分离

- AgentVersion 的发布复核人必须不同于最后一个内容作者；Owner/Workspace Admin 若参与编辑，也不能自审。
- Tool 调用的发起人、代表其运行的 AgentIdentity 或修改 input 的人不能批准同一 `ApprovalRequest`。
- high/critical CapabilityVersion 需要 Capability Publisher + Security Admin 两个不同主体；Security Admin 只
  否决/二审安全，不因此获得业务数据或 secret 读取权。
- Audit 原始敏感正文导出需 Auditor 权限 + 当前 data ACL/purpose；break-glass 产生独立告警、过期和复核。
- 多角色不会绕过上述 separation constraint；OPA hard deny、撤权和数据 ACL 永远优先。

## 4. Policy Enforcement Points

| PEP | 必查内容 | fail-close 结果 |
| --- | --- | --- |
| API/BFF | session、org/workspace、role、resource version、CSRF | 401/403/409 |
| Run/Task | AgentVersion、purpose、预算、task capability | task/run denied |
| Knowledge | source ACL/classification/freshness、locator | 不生成候选/不 hydration |
| Memory | scope/sensitivity/profile/status/time | 不检索/不写 confirmed |
| Model egress | EndpointProfile、data class、region、redaction、inventory | context/policy refusal |
| Tool gateway | ToolVersion/effect/risk/input、Connection、approval | invocation denied |
| Artifact | owner/classification/retention/download purpose | 不发 URL/内容 |
| SSE | subscription workspace + each event resource | 断流并审计 |

OPA 不可用时，data/model/tool/artifact 等敏感路径拒绝；不能使用缓存 allow 超过明确 TTL/版本。健康检查
和公开静态内容可按单独规则降级。

## 5. PostgreSQL RLS

- 所有租户表含 `organization_id`，Workspace 数据同时含 `workspace_id`。
- 启用并 `FORCE ROW LEVEL SECURITY`；应用 role 不是 table owner、superuser 或 `BYPASSRLS`。
- tenant context 仅在事务内 `set_config(..., true)`/`SET LOCAL`；连接池 checkout/checkin 有泄漏测试。
- repository query 仍显式带 tenant key；RLS 是纵深防御，不是业务授权唯一实现。
- migration/ops/break-glass role 与应用 role 分离，所有 bypass 操作单独审计。

## 6. 存储与索引隔离

- Object Store：per-org bucket/prefix + manifest authorization；object key 不含可猜用户输入。
- OpenSearch：per-org index/alias，workspace/source ACL 在 candidate generation 前过滤，hydration 后 re-check。
- Redis：opaque tenant/run keys、短 TTL，不存 secret/权威事件。
- Temporal：opaque workflow id/task queue partition，payload 只含 refs；业务授权仍在 activity/application 层。
- SecretBackend：per-org namespace/AAD；数据库仅保存 credential ref/fingerprint。

索引 ACL stale、unknown 或 re-check 不一致时丢弃候选并告警，不返回“可能无权”的内容。

## 7. 数据分级与模型出站

分级：`PUBLIC | INTERNAL | CONFIDENTIAL | RESTRICTED`。EndpointProfile 记录 origin、provider、允许分级、
region、retention/training 条件、能力 attestation、价格来源和有效期。

Context Compiler 在 pre-send 对 authoritative state、Knowledge、Memory、Tool schema/result 和 endpoint policy
取交集；按字段 redaction 后捕获 actual wire body digest。RESTRICTED 默认不出站；未知 endpoint 条款或
attestation 过期时拒绝，不静默 fallback。

## 8. Capability 与供应链

- Provider/Tool/Skill/OpenAPI/import 先 quarantine；固定 source/OCI digest、license、SBOM、vulnerability、
  schema、network、effect/risk/idempotency 和 test evidence。
- MCP annotations、Tool description、Skill instructions、OpenAPI security 声明均为不可信元数据；组织准入
  决定实际权限。
- `list_changed` 或 upstream update 只生成待检查新版本，不改变 published AgentVersion。
- suspend/revoke 立即阻断新调用；在途调用按 effect 状态终止并审计。
- Markdown/HTML 在 UI 净化；schema/ref 有 size/depth/cycle limit，防 schema bomb。

## 9. MCP、HTTP 与 sandbox

- Streamable HTTP：TLS、精确 origin、DNS/IP 重绑定检查、approved network zone、redirect/timeout/size 限制；
  模型参数不能修改 host/scheme/port。
- MCP OAuth 2.1：protected resource metadata、authorization server discovery、PKCE、Resource Indicator、
  audience/scope/expiry/refresh/revoke；禁止把 ZhiWei login token 或 MCP token 传给其他资源。
- stdio/executable Skill：固定 OCI digest、非 root、只读 rootfs、无 Docker socket、资源/进程/时间限制、
  默认无网络、显式 mount 和 credential；未知代码不在 API/worker 宿主 subprocess 执行。
- sampling 默认 off，Discover 后台禁止；elicitation 不得读取 secret；prompt/resource content 不得覆盖 policy。

## 10. 数据库与查询工具

- 数据连接使用只读/最小权限账号；写 operation 与 DataSource query 分开建 ToolDefinition。
- SQLGlot AST 只允许受支持的 SELECT/CTE，阻断多语句、DDL/DML、危险函数；再叠加 DB 权限、timeout、
  row/byte limit 和 typed params。
- AST 安全不证明 SQL 语义正确；Evidence/scorer 单独验证结果和题意。
- API/OpenAPI 参数由 schema 构造，模型不能直接拼 URL/header；敏感 header 永不进 artifact。

## 11. 工具动作、审批与重试

- effect 为 read/create/update/delete/external_communication；risk 与 idempotency 由 Admission 确认。
- 写操作先持久化 intent；Approval 绑定 exact input digest，改参数必须新建请求。
- Approval 后实际出站前重新校验 trigger principal/Membership、Agent/Capability status、当前 Policy、
  Connection/credential revoke/expiry 和 input digest；任何撤权使已批准动作失效。
- provider key/caller key/read-after-write 支持时按契约重试；既不幂等也不可复核的写调用最多一次。
- 外部可能已成功而响应丢失时写 `effect_unknown`，要求人工复核/补偿，绝不自动重发。
- ActionReceipt 不保存 secret/完整敏感 payload，只保存规范化摘要、digest、version refs 与外部 correlation。

## 12. Prompt injection 与 Memory poisoning

信任顺序：platform policy > published Agent/Profile > admitted Skill > user > retrieved/tool/MCP content。

- 低信任内容以明确边界进入 Context，不能生成 platform instruction 或授予 Tool。
- 任何 tool call 都重过 schema/policy/connection/approval，不信任模型“我已获授权”的文本。
- Memory candidate 保存来源/Run/Agent/version；敏感、团队、推断 habit 必须确认。
- secret、hidden reasoning、工具要求记住的越权指令禁止写 memory。
- revoke source、用户删除、跨用户异常传播和 injection signature 触发 quarantine/reindex。

## 13. Secret 与日志

- local-product：`cryptography` AES-GCM envelope store，随机 DEK/nonce，AAD 绑定 org/workspace/binding/version，
  master key 来自 Docker secret/挂载文件。
- production：Vault Transit/KMS adapter；支持 rotate/revoke 和短期 credential。
- secret 不进入 PG 明文、Temporal payload、Redis、OPA decision log、OTel、SSE、Evidence/Eval artifact、前端
  storage 或 build args。
- 日志默认结构化 metadata；prompt/result 正文采集需明确 data policy。CI 执行 secret/PII scan。

## 14. 审计、隐私与删除

AuditEvent 记录 actor/effective identity、organization/workspace、action、resource/version、policy decision/
revision、result、request/trace id 和前序 digest。hash chain 用于发现应用层删改，不称为签名。

Memory/Artifact/Source 删除写 tombstone、reason、retention decision，再级联删除内容、索引和缓存；历史
Run 只保留必要 digest/redacted marker。审计保留和用户隐私删除冲突由 organization policy 显式解决，
不能静默无限保留。

## 15. 必须通过的安全验证

- 跨 org/workspace IDOR、RLS bypass、连接池 tenant 泄漏。
- OIDC state/nonce/PKCE、CSRF、session fixation、logout/revoke、SCIM disable。
- OPA unavailable/stale bundle、policy update during Run、approval digest swap。
- MCP OAuth audience/token passthrough、SSRF/redirect/DNS rebinding、stdio escape、schema bomb。
- capability description/retrieval/memory prompt injection、secret exfiltration。
- ACL revoke/index stale、artifact path/object corruption、SSE event leak。
- tool duplicate/effect_unknown/cancel、cross-tenant concurrent Runs。
- API/log/trace/artifact/image/static bundle 的 secret scan。

只有相应测试和 artifact 通过后，才允许将“设计了多租户/安全”升级为“已验证”。
