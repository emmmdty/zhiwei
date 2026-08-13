# S4 - Capability Hub, Connections and Tool Gateway

> Status: frozen implementation specification  
> Depends on: S3  
> Unlocks: S5

## 1. Goal

交付用户可管理的 Capability Hub，使 MCP、OpenAPI、Agent Skills、SDK provider 和 Agent-as-tool 能从外部
目录/URL/Git 进入 quarantine，经准入、Connection/鉴权、版本绑定和隔离执行成为 Agent 能力。

## 2. Required modules

```text
src/zhiwei/capabilities/
  {domain,versions,admission,repositories,connections,credential_bindings,tool_gateway,invocations,sdk}.py
  catalog/{base,mcp_registry,git,imports}.py
  mcp/{client,transport,oauth,mapping,capabilities}.py
  openapi/{importer,operations,auth}.py
  skills/{package,validator,projection,script_tool}.py
  inspection/{schema,supply_chain,network,contracts}.py
  runners/{contracts,client,remote_http,prebuilt,kubernetes}.py
src/zhiwei/secrets/vault.py
src/zhiwei/workers/capability_runner.py
src/zhiwei/api/{capabilities,connections}.py
src/zhiwei/cli/providers.py
src/zhiwei/runtime/handlers/invoke_tool.py
src/zhiwei/workflows/activities/tools.py
apps/web/src/features/capabilities/
solution-packs/reference-capabilities/
```

## 3. Resource and lifecycle

Provider/ProviderVersion、ToolDefinitionVersion、SkillVersion、WorkflowVersion、Connection、CapabilityBinding、
AdmissionRecord 使用 immutable version。状态：discovered→quarantined→inspected→tested→approved→published→
deprecated/suspended/revoked。这里的 `published` 是 CapabilityVersion 的组织目录状态，不是 Agent release。

low/medium risk 可由有权 Capability Publisher 提交并批准（仍禁止同一主体绕过组织自审策略）；high/critical
必须记录两个不同主体的决定：Capability Publisher `publisher_approved` + Security Admin
`security_approved`，两者都在且 current test/digest 未变化才可 publish。任何内容/test/risk 变化使旧批准
失效；并发批准/发布使用 expected version/CAS。

catalog discover/import、organization admission、Workspace Connection、AgentVersion binding 为四个权限动作。
upstream update/list_changed 只创建 candidate CapabilityVersion，不改变已绑定的 AgentVersion。

## 4. Provider requirements

- MCP stdio/Streamable HTTP：tools/resources/prompts/roots/elicitation/sampling/tasks 完整 capability negotiation；
  tools 映射 ToolDefinition，resources 映射 ResourceDefinition/SourceObservationProvider port；S5 才在
  Knowledge policy 下创建 DataSource/SourceVersion。sampling default off，Discover background forbidden。
- MCP OAuth 2.1：protected resource metadata、PKCE、Resource Indicator、audience/scope、refresh/revoke；禁止
  login/MCP token passthrough。
- OpenAPI 3.1：固定 source digest、限制 `$ref`、只选 operations、host 不可被模型修改、typed params；
  write operation 需 idempotency/reconcile。
- Agent Skills：上游目录/metadata 兼容；allowed-tools 只能收窄。script 冻结依赖/OCI/SBOM并注册 Tool。
- SDK Provider SPI：稳定 discovery/invoke/health/auth contract，仍走 admission/version/binding。
- sandbox/published Agent tool 都使用 typed ChildTask，不共享 transcript/credential；published 状态在 S9
  release service 之后才可对外使用。Agent-as-tool 与 S2 的 `Delegate` **共用同一委托计数和终止界**
  （[ADR-008](../docs/DECISIONS.md#adr-008)）——不得通过在两种形式间交替来绕过深度上界；绑定
  Agent-as-tool 时对委托依赖图做环检测，成环则拒绝 binding。

## 5. Connection and execution

- subject mode：user_delegated、workspace_service、service_account；provider 与 credential 分离。
- 复用 S1 local envelope SecretBackend port，新增 production Vault/KMS implementation；read API 仅
  fingerprint/status，不再创建第二套 secret store。
- invocation：intent→schema→current policy→approval→Connection→short credential→sandbox→validate/redact→
  Observation/ActionReceipt/event。
- approval 后、真正出站前重新读取 trigger principal/Membership、Agent/Capability、Policy、Connection/
  credential、approval expiry 与 input digest；任何撤销/收紧都拒绝。
- stdio/script runner 固定 OCI digest，非 root/read-only/no Docker socket/resource cap/default no-network；remote
  HTTP 精确 origin/network zone/redirect/DNS/timeout/size control。
- MCP process/session isolation key 为 org/workspace/ProviderVersion/Connection subject/Run；首版禁止跨键复用。
- Capability Runner 是独立内部服务。local-product 只调用 admission/build pipeline 预构建的 dedicated
  provider runner service；新增 executable 需生成 signed image/Compose overlay 并 redeploy。production backend
  用最小权限 Kubernetes ServiceAccount 创建 per-invocation Job/Pod；API/Agent Worker 不持 runtime/socket 权限。
  没有合格 backend 时返回 `execution_backend_unavailable`，不回退宿主 subprocess。

## 6. Web journey

Publisher 从 official MCP Registry/URL/Git/OpenAPI 搜索或导入，查看 source/version/schema/SBOM/license/
vulnerability/network/effect/risk/test，批准后创建 Workspace Connection 并 test。Builder 只能绑定
published CapabilityVersion；Security Admin 可 suspend/revoke，Run/UI 显示结构化失败和受影响版本。

## 7. Required tests

- parser/schema/ref size/depth/cycle；malformed output/stream/tool args。
- OAuth discovery/PKCE/audience/resource/refresh/revoke/token passthrough。
- SSRF、redirect、DNS rebinding、host override、header injection、response bomb。
- malicious Tool description/Skill/resource prompt injection；secret exfiltration。
- stdio/script filesystem/network/process/resource escape corpus；无足够隔离时 script 必须拒绝执行。
- capability drift/list_changed/update diff/published pin/suspend/revoke。
- high/critical Publisher+Security Admin 双主体、same-actor 拒绝、approval invalidation 和 concurrent CAS。
- user token vs service identity；Discover cannot reuse user delegated Connection。
- approval wait 后 membership/policy/connection/capability revoke；MCP session/process 跨 org/subject/run 复用拒绝。
- runner IPC identity、image digest、pull/verify、prebuilt local service、Kubernetes Job manifest/status/cleanup；
  API/Agent Worker 无 Docker socket/Kubernetes credential/host exec。
- duplicate/effect_unknown/idempotency/read-after-write。
- Runtime：InvokeTool handler 只调用 Tool Gateway Activity，intent/result/receipt 通过 canonical event 提交。

## 8. Gate

```bash
uv run pytest tests/unit/capabilities tests/contract/capabilities -q
uv run pytest tests/integration/capabilities tests/security/capabilities -q
npm --prefix apps/web run test:e2e -- capability-hub.spec.ts
uv run zhiwei provider test --all-reference --sealed
```

每类 provider 至少一个真实 reference integration；只有抽象基类或无后端 UI 不通过。
