# 核心数据与 Artifact 契约

> 字段在实现时由 Pydantic schema + Alembic migration 固化。本文定义聚合、身份、不可变性和引用规则；
> [冻结总设计](superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md)优先。

## 1. 通用约束

- 所有组织数据含 `organization_id`；Workspace-scoped 资源同时含 `workspace_id`。
- 外部 API 使用不透明 UUID/ULID，不使用自增 ID 暴露租户数量。
- 可发布资源由 stable id + immutable version + mutable lifecycle/alias 组成。
- 每条事件/版本/artifact 含 `schema_version`、`created_at`、actor/source 与 content digest。
- 时间为 UTC aware datetime；业务有效时间与系统观察时间分开。
- secret、access token、hidden reasoning 和未脱敏原始 provider payload 不进入业务表或 artifact。

## 2. 身份与组织

```text
Organization(id, status, policy_ref, retention_policy)
Workspace(id, organization_id, name, classification_ceiling, budget_policy)
Principal(id, kind=user|service_account|agent_identity, status)
ExternalIdentity(principal_id, issuer, subject)
AuthSession(id, principal_id, issuer, subject, encrypted_token_ref, expires_at, revoked_at, version)
Membership(principal_id, organization_id, role_bindings)
WorkspaceMembership(principal_id, workspace_id, role_bindings)
Group / GroupMember
```

`AgentIdentity` 由 published AgentVersion 派生，不能登录。每次 Run 记录 trigger principal、effective
AgentIdentity、delegation chain 和 purpose；SCIM disable 使新请求立即拒绝，并由策略决定在途任务。
`AuthSession` 是 principal/session 级，不绑定 Organization；首次 callback 时用户可能尚未创建/加入组织，
同一 Principal 也可属于多个组织。`encrypted_token_ref` 指向通用 SecretBackend envelope；AAD 绑定
session/issuer/subject/version。active Organization/Workspace 在登录后由 Membership + request context 选择，
不进入身份 token。Connection secrets 仍使用 per-org/workspace/binding/version AAD。

## 3. Agent、App 与 Run

```text
AgentDefinition -> AgentVersion
SolutionPack -> SolutionPackVersion
AgentVersionBinding(model, knowledge, memory, tools, skills, policy, evals, budget)
Case -> Run -> ContextEpoch -> Attempt        # case_id 可空；跨 App/人工处置时显式绑定
Run -> TaskGraphVersion -> TaskNodeInstance
Run -> CanonicalEvent -> CanonicalProjection
Run -> Artifact / Evidence / Approval / ActionReceipt / UsageLedgerEntry
```

`SolutionPackVersion` 包含 AgentDefinition、TaskGraphTemplate、SkillVersions、Capability/Knowledge
requirements、Memory/Evidence policy、input/output schema、view manifest、eval suites 和 release claims。

Run 可在 Workbench 中独立创建；绑定 Case 后只能通过显式 command 共享选中的 Evidence/Artifact/Decision，
不能把同 Workspace 的独立 Run transcript 自动并入 Case。

### 3.1 CanonicalEvent

公共字段：

```text
event_id, organization_id, workspace_id, case_id, run_id,
sequence_no, event_type, payload_schema_version, payload,
actor_ref, task_id, attempt_id, epoch_id, idempotency_key,
previous_event_digest, event_digest, committed_at
```

同一 Run 用 advisory lock 或 CAS 分配 `sequence_no`。event、projection、run state 和 audit/stream outbox
同事务提交。重复 idempotency key + 相同 payload 返回原结果；不同 payload 冲突。

### 3.2 CanonicalProjection

Projection 必须可由 committed events 重建，包括 objective、constraints、completion obligations、Task
Graph、entities、open questions、decisions、conflicts、Evidence/artifacts、tool/action/approval、memory refs、
source watermarks、budget/usage、active epoch/attempt 和 terminal reason。Projection 是缓存，不可单独修改。

### 3.3 Task/Attempt

Task node 记录 primitive、input/output schema/version、dependencies、capability requirements、budget、status、
attempts、artifact refs 和 completion obligations。Attempt 记录 provider/tool binding、ContextManifest、
started/terminal event 和结构化 failure；只有 committed Attempt 的 business delta 进入 reducer。

## 4. Context 契约

`ContextEpoch` 绑定 canonical source head、Agent/Profile/Policy、target ModelProfile、projection rule 和
`TransitionManifest`。

`TransitionManifest`：source/target epoch、source head digest、authoritative inventory、target projection、
omissions/transforms、target profile、validation result。

`ContextManifest`：Run/Attempt、source inventory、Knowledge/Memory/Tool refs、Context IR digest、budget estimate、
omissions/transforms、serialized wire digest、redaction policy 和 pre-send result。

只有 `wire_body_captured` 后、实际网络发送前生成最终 digest。authoritative inventory 缺失时没有
ContextManifest success，只能写 `context_refusal`。

## 5. Knowledge

```text
DataSource -> DataSourceVersion -> SyncRun/Watermark
SourceObject -> SourceVersion -> SourceLocator
SourceVersion -> DocumentUnit | TableUnit | CodeUnit | GitHubUnit | SchemaUnit
SourceVersion -> AccessDescriptor / Classification / FreshnessState
ContextNode / ContextEdge (derived, rebuildable, source_refs required)
```

`SourceVersion` 保存 content digest、object manifest、source-native identity、observed_at、valid_from/to、
ACL snapshot/version、connector/version、parser/index version 和 parent/tombstone。源内容只追加新版本。

代码 identity 至少为 repository + commit + path + symbol/span；GitHub artifact 使用 stable external id +
updated/version digest；数据库 Query Evidence 绑定 schema snapshot、transaction/snapshot identifier（若支持）、
SQL、typed params 和 canonical result。

## 6. Memory

```text
MemoryRecord(
  id, version, organization_id, workspace_id,
  scope=user|team|case, scope_subject_id,
  type=preference|fact|decision|episode|lesson,
  subject, key, canonical_value, source_refs,
  observed_at, valid_from, valid_to, confidence, sensitivity,
  status=candidate|confirmed|superseded|revoked|expired,
  author_ref, approver_ref, conflict_refs, retention_policy,
  allowed_profile_refs, acl_version
)
```

纠正创建 superseding version；冲突并存直到明确解决；撤销和删除写 tombstone 并移除检索正文。每个
retrieval result 携带 applied filters、record/version、status、source refs 和 reason，便于审计。

## 7. Capabilities 与 Connections

```text
Provider -> ProviderVersion
ProviderVersion -> ToolDefinitionVersion | SkillVersion | WorkflowVersion
ProviderVersion -> AdmissionRecord
Connection(id, owner_scope, provider_ref, auth_scheme, credential_ref, scopes, audience, status)
CapabilityBinding(workspace/agent_version, capability_version, connection_ref, narrowed_scope)
```

`ToolDefinitionVersion` 包含 input/output JSON Schema、effect、risk、idempotency、data classes in/out、network
scope、timeout/result limits、credential requirements 和 provider contract digest。远端 annotation 只保存为
untrusted metadata；AdmissionRecord 保存管理员确认值。

`CredentialBinding` 只保存 SecretBackend handle、subject mode、scope/audience、created/rotated/expires/
revoked 和 fingerprint；任何 read API 不返回明文。

## 8. Evidence 与 Claim

`EvidenceRef` 是 tagged union：`QueryReplay | CellRef | DocRef | CodeRef | GitHubRef | ApiRef | AgentRef |
PatternRef`。公共字段为 Evidence id/version、SourceVersion/snapshot、locator、canonical value/result digest、
verifier version 和 stale/authorization status。

Canonical value：integer/decimal 为规范十进制串；float 为 IEEE-754 binary64 bits；text 为 Unicode NFC
+ UTF-8；bytes 为 base64url；datetime 带 offset 并归一 UTC；非有限 float 默认不能成为正式数值 Claim。

`ClaimBinding` 包含 answer digest、code-point `[start,end)`、span digest、claim type、canonical value、
EvidenceRefs 和 verification result。Claim 更新必须生成新 answer/version，不能移动旧 span。

## 9. Approval 与 ActionReceipt

`ApprovalRequest` 绑定 Run/Task/call、ToolVersion、normalized input digest、policy decision/revision、risk、
requester 和 expiry。修改参数等于拒绝旧请求并创建新 digest。

`ActionReceipt` 包含 actor/delegation、Agent/Tool/Provider/Connection version、policy/approval、idempotency key、
input summary/digest、timestamps、external correlation id、typed result digest、effect status
`committed|verified|failed|unknown|compensated` 和 verifier result。

## 10. Eval、Release 与 Claim Registry

```text
Dataset -> DatasetVersion
EvalSuite -> EvalSuiteVersion
EvalRun(mode, bindings, prereg_ref, status) -> EvalSample/Score/ArtifactManifest
AgentRelease -> AgentVersion + dependency digests + EvalRun refs + Policy ref
ReleaseClaim(text/template, status, scope, artifact_refs)
```

EvalSample 含 sample/template/independence unit、dataset digest、target、attempts、terminal status、scores、
Evidence、usage 和 failure。Run 只有全部预注册单位 terminal 才可 seal；partial 可 resume，不可发布。

## 11. Artifact protocol

Artifact manifest 记录 immutable object key、SHA-256、size、media type、schema/version、organization、owner
resource、classification、retention、encryption/key ref、created_at。写入顺序：temporary upload -> server/read-
back digest -> immutable key -> PG manifest commit。

Evidence Bundle mode：

| mode | 验证边界 |
| --- | --- |
| `embedded_public` | 第三方可完全离线复算 |
| `external_snapshot` | 有相同 digest snapshot 的授权主体可复算 |
| `private_org` | 组织信任域内复算，不作公开数据可用性声明 |

删除先写 tombstone/retention decision，再删 object；历史记录保留不可逆 digest 和删除原因。hash 证明相对
已知 digest 的一致性，不证明发布者身份；release provenance 使用独立签名 attestation。
