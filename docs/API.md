# API、SSE 与 CLI 契约

> 详细领域语义见[冻结总设计](superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md)。
> API 不是数据库 CRUD 镜像；命令必须经过 application service、policy 和 audit。

## 1. 通用规则

- REST 基路径 `/api/v1`；浏览器通过同源 BFF session，SDK 使用 OIDC/OAuth service identity。
- 所有 mutation 要求 `Idempotency-Key`；版本化资源更新要求 `If-Match` 或 `expected_version`。
- 请求 context 从 authenticated principal 解析 organization/workspace，不信任 body 中自报 owner。
- list 使用稳定 cursor；artifact 先授权再代理或生成短期 scoped URL。
- 长任务返回 `202 + command_id/run_id`，状态由 REST projection + SSE 获取。
- 错误体：

```json
{
  "code": "CONTEXT_REFUSAL",
  "message": "authoritative state exceeds target context budget",
  "details": {},
  "request_id": "...",
  "trace_id": "...",
  "retryable": false
}
```

## 2. Identity 与组织

```text
GET  /auth/login
GET  /auth/callback
POST /auth/logout
GET  /api/v1/me

GET|POST       /api/v1/organizations
GET|PATCH      /api/v1/organizations/{org_id}
GET|POST       /api/v1/organizations/{org_id}/workspaces
GET|POST       /api/v1/organizations/{org_id}/members
DELETE         /api/v1/organizations/{org_id}/members/{principal_id}
GET|POST       /api/v1/workspaces/{workspace_id}/groups
GET|POST       /api/v1/workspaces/{workspace_id}/service-accounts
POST           /api/v1/scim/v2/...
```

权限变更、disable/revoke 和 SCIM mutation 必须写 AuditEvent；成员 API 不返回 IdP tokens。

## 3. Agent、Solution Pack 与发布

```text
GET|POST       /api/v1/workspaces/{workspace_id}/agents
GET|PATCH      /api/v1/agents/{agent_id}
POST           /api/v1/agents/{agent_id}/versions
GET             /api/v1/agent-versions/{version_id}/diff
POST            /api/v1/agent-versions/{version_id}/validate
POST            /api/v1/agent-versions/{version_id}/sandbox-runs
POST            /api/v1/agent-versions/{version_id}/evaluate
POST            /api/v1/agent-versions/{version_id}/stage
POST            /api/v1/agent-versions/{version_id}/publish
POST            /api/v1/agent-versions/{version_id}/deprecate

GET|POST        /api/v1/solution-packs
POST            /api/v1/solution-packs/{pack_id}/install
```

发布命令返回 dependency digests、Gate results 和 Claim Registry diff；不能通过普通 PATCH 改为 published。

## 4. Workbench、Cases 与 Runs

```text
GET|POST       /api/v1/workspaces/{workspace_id}/cases
GET|PATCH      /api/v1/cases/{case_id}
POST           /api/v1/cases/{case_id}/share-artifacts
POST           /api/v1/cases/{case_id}/resolve

POST           /api/v1/agent-versions/{version_id}/runs
GET            /api/v1/runs/{run_id}
GET            /api/v1/runs/{run_id}/tasks
GET            /api/v1/runs/{run_id}/events
GET            /api/v1/runs/{run_id}/artifacts
POST           /api/v1/runs/{run_id}/input
POST           /api/v1/runs/{run_id}/cancel
POST           /api/v1/runs/{run_id}/model-transitions
POST           /api/v1/runs/{run_id}/retry-failed-task
GET            /api/v1/runs/{run_id}/context-manifests
```

Ask/Discover/ChangeBrief 输入使用各自 SolutionPack schema，但创建 Run 的 endpoint 和状态机相同。

## 5. Knowledge

```text
GET|POST       /api/v1/workspaces/{workspace_id}/knowledge-collections
GET|POST       /api/v1/knowledge-collections/{id}/sources
POST           /api/v1/data-sources/{id}/sync
GET            /api/v1/data-sources/{id}/versions
GET            /api/v1/data-sources/{id}/watermarks
POST           /api/v1/knowledge/query              # Builder/debug，仍做 ACL
GET            /api/v1/source-versions/{id}/locators/{locator_id}
POST           /api/v1/data-sources/{id}/disable
```

query 返回 source version、locator、ACL/freshness 和 score breakdown；不得只返回匿名 chunk text。

## 6. Memory

```text
GET            /api/v1/workspaces/{workspace_id}/memory
GET            /api/v1/memory/{id}
POST           /api/v1/memory/{id}/confirm
POST           /api/v1/memory/{id}/correct
POST           /api/v1/memory/{id}/resolve-conflict
POST           /api/v1/memory/{id}/revoke
DELETE         /api/v1/memory/{id}
GET            /api/v1/memory/{id}/provenance
```

客户端不能直接创建 `confirmed team memory`；write candidate/confirm 受不同权限控制。

## 7. Capability Hub 与 Connections

```text
GET             /api/v1/capability-catalog/search
POST            /api/v1/providers/import
GET             /api/v1/provider-versions/{id}/inspection
POST            /api/v1/provider-versions/{id}/test
POST            /api/v1/provider-versions/{id}/admit
POST            /api/v1/provider-versions/{id}/publish
POST            /api/v1/provider-versions/{id}/suspend

GET|POST        /api/v1/workspaces/{workspace_id}/connections
POST            /api/v1/connections/{id}/oauth/start
GET             /api/v1/connections/oauth/callback
POST            /api/v1/connections/{id}/test
POST            /api/v1/connections/{id}/rotate
POST            /api/v1/connections/{id}/revoke

POST            /api/v1/agent-versions/{id}/capability-bindings
DELETE          /api/v1/agent-versions/{id}/capability-bindings/{binding_id}
```

secret fields 为 write-only，响应只给 fingerprint/status/expiry。Import、admit、connect、bind 是不同权限。

## 8. Approvals、Evidence 与 Actions

```text
GET             /api/v1/workspaces/{workspace_id}/approvals
GET             /api/v1/approvals/{id}
POST            /api/v1/approvals/{id}/approve
POST            /api/v1/approvals/{id}/reject
POST            /api/v1/approvals/{id}/replace-input

GET             /api/v1/runs/{run_id}/claims
GET             /api/v1/evidence/{id}
GET             /api/v1/evidence/{id}/bundle
POST            /api/v1/evidence/verify
GET             /api/v1/action-receipts/{id}
POST            /api/v1/action-receipts/{id}/verify
```

`replace-input` 创建新 ApprovalRequest，不修改已批准 digest。verify 返回稳定 error code 和 failed layer。

## 9. Discover

```text
GET|POST        /api/v1/workspaces/{workspace_id}/discovery-programs
POST            /api/v1/discovery-programs/{id}/versions
POST            /api/v1/discovery-program-versions/{id}/activate
POST            /api/v1/discovery-programs/{id}/run
GET             /api/v1/discovery-programs/{id}/hypotheses
POST            /api/v1/risk-hypotheses/{id}/triage
POST            /api/v1/risk-hypotheses/{id}/create-case
POST            /api/v1/risk-hypotheses/{id}/resolve
```

Signal、RiskHypothesis、HumanResolution 使用不同 schema/endpoint；triage 不能覆写原始 detector output。

## 10. Eval、Claims 与 Observability

```text
GET|POST        /api/v1/workspaces/{workspace_id}/datasets
GET|POST        /api/v1/workspaces/{workspace_id}/eval-suites
POST            /api/v1/eval-suites/{id}/runs
GET             /api/v1/eval-runs/{id}
POST            /api/v1/eval-runs/{id}/resume
POST            /api/v1/eval-runs/{id}/seal
GET             /api/v1/agent-versions/{id}/claims
GET             /api/v1/runs/{id}/usage
GET             /api/v1/workspaces/{id}/audit-events
```

只有满足 frozen registry 且全部单位 terminal 的 EvalRun 可 seal。Claim status 只能由 release service 按
artifact 升级，不能手工 PATCH 为 verified。

## 11. SSE

`GET /api/v1/workspaces/{workspace_id}/events?cursor=...` 发送：

```text
run.created | run.state_changed | task.started | task.completed | task.failed
model.started | model.delta | model.completed
knowledge.retrieved | memory.retrieved
tool.intent | tool.started | tool.completed | tool.effect_unknown
approval.requested | approval.decided
evidence.attached | artifact.created | context.transitioned
run.completed | run.failed | run.cancelled
```

事件含 event id/cursor、run/task、schema version、small typed payload 和 REST resource link。SSE 不是
canonical event log；断线、慢客户端或 Redis 丢失后用 cursor/projection 恢复，大内容不进流。

## 12. CLI

CLI 面向开发、验证和运维，调用相同 application/domain service：

```text
zhiwei dev up|down|doctor
zhiwei db migrate|check
zhiwei assets lock --check|--write
zhiwei provider inspect|test|admit
zhiwei source sync|status
zhiwei models attest
zhiwei verify evidence <bundle>
zhiwei verify context <manifest>
zhiwei eval run|resume|seal|verify|report|external-status
zhiwei risk generate|verify
zhiwei release check|attest
zhiwei backup create|verify
zhiwei restore verify
zhiwei ops fault-run|load-run
```

交互式产品能力在 Web/API，不另做绕过组织、policy、budget 和 Evidence 的 `zhiwei ask` 快速路径。CLI
默认 JSON stdout、diagnostic stderr；Evidence 退出码在 S6 冻结，release/ops 命令码表在 S9/S11 contract
中冻结。

## 13. Live 边界

Docker 启动只允许 fixture/replay。真实模型调用要求 Endpoint/Model attestation、允许的数据分类、专用
Connection、精确 origin、预算 reserve、operator explicit action 和使用条款 digest。UI、API、CLI 共用
同一 preflight，不提供隐藏 bypass；CI 永不读取 live key。
