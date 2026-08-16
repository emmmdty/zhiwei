# S1-T4 修复设计 addendum：生产 PEP/audit 纵切

> 状态：修复执行前冻结（A 档，设计/验收方确认后生效）。
> 事实源：总设计 §9.2/§9.4、`docs/PERMISSIONS.md` §14、`specs/s1-tenancy-policy.md` §3、
> `docs/handoffs/s1-t4-design-gap.md`（0005 结构方案）、T3 `src/zhiwei/policy/` 冻结契约。
> 范围：仅裁决 9f134bf 交接审查列出的设计缺口；不重做已冻结的 RLS/pool/IDOR 边界。

## 1. 最小反例（每个缺口一个）

| # | 缺口 | 最小反例 |
| --- | --- | --- |
| G1 | `create_app` 没有组合 OPAClient/PolicyEnforcer | 生产组合根只注入 OIDC/session 依赖；授权判定无处可挂，mutation 端点不可能先于业务事务做 policy 求值。 |
| G2 | router factory 没有 policy/audit dependency | `create_workspaces_router(actor_dependency=..., sessions=...)` 的签名里不存在任何策略/审计注入点；即使有 enforcer 也无法接线。 |
| G3 | ActorContext 不含构造真实 PolicyInput 所需的 role bindings | `ActorContext(principal_id, organization_id, workspace_id)` 无角色绑定；`PolicyInput.actor.roles` 只能空着——空绑定对任何矩阵 cell 都是 deny，或被迫伪造绑定（第二套事实源）。 |
| G4 | request_id/trace_id 没有生产来源 | `AuditRecord` 要求非空 request/trace id，但生产 mutation 路径无人提供；一旦接线只能硬编码或从 body 取（信任用户输入）。 |
| G5 | guessed/cross-tenant deny 不能伪造未知 resource version | 跨租户猜 ID 的请求被拒绝时，目标资源版本不存在（也不允许去读目标租户确认）；若硬填 `1` 就是把「未知」伪装成「已知且为 1」。 |
| G6 | identity-global mutation 与 tenant audit scope 边界未裁决 | bootstrap（首登 principal 无 org）创建新 org：actor 无租户上下文，审计写进哪个 scope、用什么 actor/effective identity、policy input 的 org 是谁，均无裁决。 |
| G7 | AuditRecord/DB 边界与既有测试不一致 | `payload_digest` 在 Pydantic 只限 71 字符任意串（`"a"*71` 可过），DB 无 CHECK；`decision_reason` DB 允许 NULL 而 Pydantic 必填；`decision_id`/`policy_revision` 无配对约束（可只填其一）；这些规则 Pydantic 与 PostgreSQL 不一致，direct INSERT 可绕过全部边界。 |

## 2. 受影响 invariant（及处置）

| # | 不变量 | 影响与处置 |
| --- | --- | --- |
| I1 | `build_audit_digest` v1 公式逐字节不变；v2 覆盖全部语义字段 | **不动**。v2 已覆盖 17 项语义字段（audit_schema_version/org/ws/action/resource type+id+version/actor/effective/decision/revision/reason/result/request/trace/payload/previous digest）。Subagent B 以契约测试冻结「新增语义字段继续进入 v2 digest」。 |
| I2 | 0001~0005 迁移只读 | **不动**。新 CHECK 全部追加到新迁移 `0006_audit_contract`（down_revision=`0005_audit_structured`），可逆。 |
| I3 | `test_rls.py` / `test_pool.py` / `test_idor.py`（RLS/pool/IDOR 边界） | **只读**。router factory 新增 policy 参数为关键字可选参数（默认 None=legacy 直接组合路径），既有工厂调用不破坏；`zhiwei_app` repository 谓词与 FORCE RLS 不改。 |
| I4 | `test_audit.py`（T4 已冻结审计契约） | 修复 RED 阶段修订 2 处**机制缺陷**（§7），修订动机与断言语义不变；新 RED commit 后锁定。 |
| I5 | `test_settings.py` / `test_oidc.py`（T2 已冻结组合契约） | 修复 RED 阶段修订：组合期新增必需输入 `ZHIWEI_OPA_BASE_URL`（§3.2），2 处 helper 补键 + 1 处 parametrize 补项；断言语义不变。 |
| I6 | 未配置/不可达策略 endpoint 时 mutation 行为 | 组合期拒绝缺 OPA URL（fail closed，与既有 `_REQUIRED` 组合检查同模式）；运行期 OPA 不可达 → 本地拒绝 + denied 审计，mutation 零写入。 |
| I7 | 冻结的 `append_audit` / `append_fail_closed_audit` / `append_audit_chain` / outbox / digest 设施 | **复用，不复制**。生产接线只新增编排层（`policy_gate.py`），digest chain、advisory lock、outbox 写入均走既有实现。 |
| I8 | T3 OPAClient fencing / 有界缓存 / fail-closed | **不动** `src/zhiwei/policy/` 任何文件。gate 只调用 `enforcer.authorize` / `enforcer.deny`。 |

## 3. 最小应用服务边界

### 3.1 决策

1. **组合根是唯一强制点**：`create_app` 把 `ZHIWEI_OPA_BASE_URL` 加入必需配置（缺失→组合期
   ValueError，指名变量）；组合 `OPAClient(base_url=opa_base_url, http_client=policy_http_client)` +
   `PolicyEnforcer`；`policy_http_client` 是唯一外部 binding 替换点（与 `oidc_http_client` 同模式，
   只出现在测试）。生产代码**不硬编码** OPA URL、角色、decision id、revision、request/trace id、
   resource version。
2. **router factory 接受关键字可选 `policy_enforcer`**：提供→完整 PEP/audit 纵切；缺省→legacy
   直接组合路径（既有冻结测试的 RLS/IDOR 契约路径，只有仓库层防线，不写审计）。生产 app 永远由
   create_app 走强制路径。**不允许**任何端点自行复制 gate 逻辑——全部经 `zhiwei.api.policy_gate`
   单一编排。
3. **请求标识**：`request_id` / `trace_id` 由 gate 的 per-request 依赖生成（`uuid4().hex`，32 hex
   字符），缓存于 `request.state`，同一请求内 audit 与 policy 共用；**不从 body、cookie、header
   信任**；两次请求必不同。裁决：S1 无 tracing 传播（S2 起引入），缺失时**生成**而非 fail closed。
4. **resource version 诚实表达**：`0` = 语义 `unknown`（mutation 未应用，常见于 denied/failed 路径；
   PEP 不读目标租户、不猜版本）。allowed 路径写真实版本（create=1；update 语义 S2 起，S1 无更新端点）。`AuditRecord`
   `resource_version` 边界从 `ge=1` 放宽为 `ge=0`；DB CHECK 同款（v2 行 `resource_version >= 0`）。
   禁止把 unknown 写成 1。
5. **跨租户/猜测 ID**：gate 先做结构 scope 检查——mutation 目标 org ≠ actor org 时**不构造
   PolicyInput、不发 OPA 请求**（mock transport 计数可证 0 次），本地拒绝
   `tenant_scope_mismatch`，audit scope = **authenticated actor context**（actor org），
   resource_version=0，decision_id/revision=NULL，API 403 detail `outside tenant scope`（与既有
   冻结 detail 一致）。
6. **三类 metadata 规则**（AuditRecord Pydantic 与 PostgreSQL CHECK 逐条一致，direct INSERT 不可
   绕过）：

   | result | decision_id / policy_revision | decision_reason | 来源 |
   | --- | --- | --- | --- |
   | `allowed` | 必须同时非空 | 非空 | 真实 OPA 决策 |
   | `denied`（OPA deny） | 必须同时非空 | 非空（OPA reason） | 真实 OPA 决策 |
   | `denied`（本地拒绝） | 必须同时为 NULL | 非空（固定 reason 码） | PEP 本地 |
   | `failed` | 必须同时为 NULL | 非空（固定 reason 码） | 业务/审计失败 |

   禁止只有 decision_id 或只有 policy_revision（配对约束 `(decision_id IS NULL) = (policy_revision IS NULL)`）。
   固定本地 reason 码：`tenant_scope_mismatch`、`policy_input_invalid`、`enforcement_internal_error`、
   `opa_unavailable`、`idempotency_conflict`、`name_conflict`、`resource_conflict`、
   `organization_exists`、`principal_not_found`、`principal_disabled`。OPA 路径 reason 原样保留
   （`allow:...` / `deny:...`），不得改写。
7. **policy 先于 mutation**：gate 在业务事务开始前求值。denied → 独立审计事务
   （`append_fail_closed_audit`），业务事务不开始。allowed → 同一 tenant 事务内
   `append_audit` + outbox（与业务同提交/同回滚；audit 写失败→整体 rollback）。
   审计写失败（deny 路径）→ 异常上抛（500），mutation 绝不执行。
8. **幂等与审计控制流（独立审查 7.2 裁决，端点侧冻结）**：policy 在命令执行前求值，gate 无法
   预知重放/冲突——allowed 审计**只在 `outcome.created == True` 时**在同一 tenant 事务内追加；
   重放（created=False）不追加任何审计/outbox。命令抛业务拒绝（幂等/名称/资源冲突、
   principal 状态）→ 端点捕获后在**独立事务**写 `failed` 审计（NULL metadata，reason=固定码），
   业务零写入。任何路径都不得重复追加同一业务审计。
9. **bootstrap（identity-global → tenant 边界）**：首登 principal（无 org）创建新 org 合法。
   policy input 的 org = **新建 org**（mutation 目标即 scope）；actor roles 为空
   （无绑定）→ 真实 Rego 对 org.manage 无绑定必 deny，**这是策略语义问题**（首登 bootstrap 授权
   规则属于 Rego 交付，S1 只冻结 PEP 纵切：policy 先于 mutation、deny 即拒绝+审计）。审计 scope =
   新建 org（org, NULL workspace）；actor_ref / effective_identity_ref =
   `user:<principal_id>`（S1 会话路径只产生 USER principal；delegation 形状 S2 起）。
   **bootstrap 被拒审计例外（独立审查 7.1 裁决）**：`audit_events.organization_id` NOT NULL 且
   FK → organizations.id，且 `append_fail_closed_audit` 要求非空 tenant context——被 OPA 拒绝的
   bootstrap 目标 org 不存在，任何 scope 都无合法审计落点（identity-global 审计链不存在，另建
   属越界）。裁决：**OPA 拒绝的 bootstrap → 403 `policy denied`、业务零写入、不写 denied 审计**
   （schema 边界约束的冻结例外，其余所有 deny 均审计）；该例外必须由 RED 测试固定，不得静默扩大。
   `organization_exists`（org 已存在）的 failed 审计仍写在该 org scope（FK 满足）。
10. **PEP input 映射**（S1 冻结词汇无 workspace/group/membership 资源；用最接近的冻结锚点，
    语义校准随 S2 词汇扩展，本层只冻结编排）：

    | mutation | audit resource_type | PolicyInput (type, action) | purpose |
    | --- | --- | --- | --- |
    | POST /api/v1/organizations | `organization` | (org, manage) | general |
    | POST /organizations/{org}/workspaces | `workspace` | (workspace_policy, configure_workspace) | general |
    | POST /workspaces/{ws}/groups | `group` | (workspace_policy, configure_workspace) | general |
    | POST/DELETE /organizations/{org}/members | `membership` | (org, manage) | general |

    `resource.version`（PolicyInput 侧）取 `str(resource_version)`；ResourceContext 全空、
    delegation 空、classification/risk 空、context.now=utc_now、context.trace_id=本请求 trace_id。

11. **审计 action 字符串**（冻结，镜像 IDEMPOTENCY_SCOPE_* 语义名）：

    | mutation | audit action | audit scope | resource_version |
    | --- | --- | --- | --- |
    | bootstrap org | `organization.create` | (新 org, NULL) | 1 |
    | create workspace | `organization.workspace.create` | (org, NULL) | 1 |
    | create group | `workspace.group.create` | (org, ws) | 1 |
    | add org member | `organization.member.add` | (org, NULL) | 1 |
    | remove org member | `organization.member.remove` | (org, NULL) | 1 |

    S1 资源（org/ws/group/membership）无版本列：创建即版本 1，删除引用创建版本 1；
    allowed 路径写 1，denied/failed 路径写 0（unknown）。`actor_ref` /
    `effective_identity_ref` = `user:<principal_id>`（S1 会话只产生 USER principal）。
12. **API 403 detail 裁决**：OPA deny / `opa_unavailable` / `policy_input_invalid` /
    `enforcement_internal_error` → `policy denied`；`tenant_scope_mismatch`（org 不一致）→
    `outside tenant scope`（与既有冻结 detail 一致）；actor 无 org context 的目标 mutation →
    `organization context required`（与既有 detail 一致，audit reason 仍 `tenant_scope_mismatch`）。
    审计写失败（deny 路径）→ 异常上抛（500），mutation 绝不执行。
13. **request/trace 格式**：`uuid4().hex`（32 hex 字符），per-request 生成，`request.state` 缓存，
    同一请求内 policy input（context.trace_id）与 audit 共用。

### 3.2 生产编排

- `Settings` 增 `opa_base_url: str | None = None`（`ZHIWEI_OPA_BASE_URL`，非 Secret）；`create_app`
  加入 `_REQUIRED`。
- `create_app` 增参 `policy_http_client: httpx.AsyncClient | None = None`；组合
  `OPAClient` + `PolicyEnforcer`，传给三个 mutation router；`app.state` 暴露
  `policy_client` 供 dispose。
- 新模块 `src/zhiwei/api/policy_gate.py`：
  - `request_trace(request) -> (request_id, trace_id)`（per-request 生成 + state 缓存）；
  - `build_policy_input(...)`（ActorContext→PolicyInput；未知角色名→`policy_input_invalid` 本地拒绝）；
  - `authorize_mutation(...)`：scope 检查 → input 构造 → `enforcer.authorize` → denied 时
    `append_fail_closed_audit` + 403；allowed 时返回决策供端点同事务写审计；
  - `denied_audit_record(...)` / `allowed_audit_record(...)` 工厂；
  - `append_failed_mutation_audit(...)`（业务拒绝的独立事务 failed 审计）。
- `identity/domain.py`：`ActorContext` 增 `kind: PrincipalKind = PrincipalKind.USER`（S1 会话路径
  只产生交互 USER，注释声明）与 `role_bindings: tuple[ActorRoleBinding, ...] = ()`；新领域模型
  `ActorRoleBinding(name, scope, organization_id, workspace_id)`（org/workspace 一致性校验，
  镜像 policy.input.RoleBinding 形状但**不导入 policy 层**，防依赖环）。
- `identity/sessions.py` `resolve_context`：从既有 memberships 查询结果填充 role_bindings
  （scope='organization' 行→org 绑定；scope='workspace' 行→workspace 绑定）。
  **provenance 裁决（独立审查 7.3）**：`ActorContext.role_bindings` 只含**已解析 org** 的 org 级
  绑定 + **已解析 workspace**（若存在）的 workspace 级绑定，绝不含其他 org 的绑定——防止跨 org
  遗留角色字符串污染 `build_policy_input`（未知角色名 fail closed）。
- `identity/audit.py`：`AuditRecord` 加 `payload_digest` 严格格式（`^sha256:[0-9a-f]{64}$`）、
  `resource_version ge=0`、决策配对/result 一致性 model_validator（与 0006 CHECK 逐条一致）。
- `persistence/events.py` `AuditEventData`：v2 行加同款校验（payload_digest 格式、reason 非空、
  配对、result 一致性、version≥0）。
- 新迁移 `0006_audit_contract.py`（追加 CHECK，downgrade 全撤）：
  `ck_audit_events_v2_decision_reason`（v2→reason 非空非空串）、
  `ck_audit_events_v2_decision_pairing`（v2→(decision_id IS NULL)=(policy_revision IS NULL)）、
  `ck_audit_events_v2_allowed_metadata`（v2∧allowed→两者非空）、
  `ck_audit_events_v2_failed_metadata`（v2∧failed→两者 NULL）、
  `ck_audit_events_v2_payload_digest`（v2→`payload_digest ~ '^sha256:[0-9a-f]{64}$'`）、
  `ck_audit_events_v2_resource_version`（v2→`resource_version >= 0`）。
  校验：已有集群 0005→0006（zhiwei_test 当前无 v2 生产行；测试残留由 fixture 重建清理）、
  全新 base→head、downgrade→upgrade、v1/v2 混合链。
- `payload_digest` 取值语义：allowed → `digest({resource_type, resource_id, resource_version})`
  （业务变更指纹）；denied/failed → `digest({request_id, resource_type, resource_id, action})`
  （被拒请求指纹）。两者都经 `contracts.canonical.digest`（`sha256:` 前缀，格式契约自然满足）。

### 3.3 既有冻结测试修订（修复 RED 阶段，动机登记）

| 文件 | 修订 | 动机 |
| --- | --- | --- |
| `test_audit.py` `_record` | `payload_digest="a"*71` → `"sha256:"+"a"*64`（2 处，含 v1 行 `"b"*71` → `"sha256:"+"b"*64`） | 原 71 字符任意串不符合本 addendum §3.1.6 冻结的 digest 格式契约；断言语义（字段/链/原子性）不变。v1 行的 `"b"*71` 修订非 0006 CHECK 所必需（v2 限定），但生产 v1 行 payload_digest 恒为真实 `sha256:` digest（`unit_of_work.py:365`），测试数据对齐生产形状 |
| `test_audit.py` 篡改用例 | `("payload_digest", "f"*71)` → `"sha256:"+"f"*64` | 新 CHECK 会拒绝该 UPDATE（DB 错误而非 EventChainError）；换成格式合法但内容不同的 digest，断链断言语义不变 |
| `test_settings.py` | `_full_auth_app_settings` 补 `ZHIWEI_OPA_BASE_URL`；`dropped` parametrize 补同名项 | 组合期新增必需输入（I6）；断言语义不变 |
| `test_oidc.py` `_settings` | 补 `ZHIWEI_OPA_BASE_URL` | 同上；auth 流程不触发 policy 求值，无需 mock transport |

> 以上均非 RLS 测试；RLS/pool/IDOR 三文件零改动。

## 4. 文件白名单（修复）

**生产**：
- `src/zhiwei/config/settings.py`（+opa_base_url）
- `src/zhiwei/app.py`（+组合）
- `src/zhiwei/api/policy_gate.py`（新）
- `src/zhiwei/api/organizations.py` / `workspaces.py` / `memberships.py`（+policy_enforcer 可选参数与 gate 接线）
- `src/zhiwei/identity/domain.py`（ActorContext.kind/role_bindings + ActorRoleBinding）
- `src/zhiwei/identity/sessions.py`（resolve_context 填充 role_bindings）
- `src/zhiwei/identity/audit.py`（AuditRecord 边界）
- `src/zhiwei/persistence/events.py`（AuditEventData v2 校验）
- `migrations/versions/0006_audit_contract.py`（新）

**测试**（修复 RED 阶段）：`tests/integration/rls/`（新，Subagent A）、
`tests/security/tenancy/test_audit.py`（§3.3 机制修订）、`tests/unit/config/test_settings.py`、
`tests/security/identity/test_oidc.py`（§3.3）。

**文档**：本 addendum、`docs/handoffs/s1-t4.md`（交接单更新）、`.env.example`（补
`ZHIWEI_OPA_BASE_URL` 登记，独立审查 7.4）。

**禁止**：`src/zhiwei/policy/`、`policies/`、`evals/`、`migrations/versions/0001~0005`、
`tests/security/tenancy/test_rls.py|test_pool.py|test_idor.py`、新增第三方依赖、
`deploy/`（OPA sidecar 编排不属于本 Task）。

## 5. 验证矩阵（RED 冻结，GREEN 交付）

| 场景 | 业务 | audit/outbox | metadata | 事务 |
| --- | --- | --- | --- | --- |
| bootstrap 成功 | org+owner membership | v2 行+outbox 同现 | allowed+真实 decision/revision/request/trace | 同一 tenant 事务 |
| bootstrap 审计写失败 | 全部回滚 | 无 | — | 整体 rollback |
| **bootstrap 被 OPA 拒绝** | **零写入** | **无审计（§3.1.9 schema 边界例外）** | **—** | **业务事务不开始** |
| 每次真实 mutation（ws/group/member） | 落库 | 自动追加 | allowed | 同一事务 |
| 幂等重放 | 不变 | 不重复追加 | — | 零写入 |
| 幂等冲突 | 不变 | failed 审计 | NULL+`idempotency_conflict` | 独立事务 |
| OPA deny | 零写入 | denied 审计 | 真实 decision_id/revision/reason | 独立事务 |
| 本地 fail-closed（opa 不可达） | 零写入 | denied 审计 | NULL+`opa_unavailable` | 独立事务 |
| 审计写失败（deny 路径） | 仍零写入 | 无（失败上抛 500） | — | mutation 不执行 |
| 跨租户猜 ID | 零写入 | denied 审计（actor scope） | NULL+`tenant_scope_mismatch`、version=0、OPA 0 次调用 | 独立事务 |
| request/trace | — | 两次请求值不同、非 body 来源 | — | — |
