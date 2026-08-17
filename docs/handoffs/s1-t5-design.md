# S1-T5 设计裁决 — SCIM 2.0 与 membership 生命周期

> 状态：执行方设计裁决，待第一轮独立验收（对照 RFC 7643/7644 + 冻结 spec）。
> 事实源优先级：冻结总设计 §9.1/§9.4 > ADR > specs/s1 §3/§5/§6 > Plan Task 5 >
> docs/PERMISSIONS.md §3.1 > T4 交接单。
> 已获 operator 批准的两项裁决：①白名单扩展（commands.py/repositories.py 增量方法 +
> 0009 迁移，全 additive）；②JIT 纵切冻结在 provisioning mutation 层（登录时触发器与
> Rego 授权规则随策略交付）。

## 0. 范围与文件

任务白名单（operator 批准扩展后）：

```text
新增：src/zhiwei/identity/scim.py、src/zhiwei/api/scim.py、
      tests/contract/identity/test_scim.py、tests/integration/identity/test_scim_lifecycle.py、
      migrations/versions/0009_scim_group_member_delete.py
修改（仅增量）：src/zhiwei/app.py（注册 scim router，沿用既有 factory 模式）
      src/zhiwei/identity/commands.py（+2 命令、create_user 增可选参数）
      src/zhiwei/identity/repositories.py（+3 方法，全 additive）
只读：0001~0008 迁移、tests/ 既有文件、evals/、Rego、policy_gate.py、
      src/zhiwei/policy/、sessions.py、auth.py、api/{organizations,workspaces,memberships}.py、
      治理文档
```

## 1. SCIM 子集矩阵（RFC 7644 §3 对应操作，prefix `/scim/v2`）

冻结原则：**必需子集之外的一切操作显式拒绝（501），绝不静默忽略**（项目纪律高于
RFC 7644 §3.3 对未知属性的「可忽略」许可）；未知属性经 Pydantic `extra="forbid"`
一律 400 + SCIM 错误体。

| 端点 | 方法 | 裁决 | 成功码 |
| --- | --- | --- | --- |
| /Users | POST | **支持**：创建 User principal + external identity（绑定键 `(issuer, subject)`，见 §4） | 201 + Location |
| /Users | GET | **显式不支持**（列表；必需子集不含，IdentityStore 无列表方法） | 501 |
| /Users/{id} | GET | **支持**：返回 `{schemas, id, externalId, userName, active, meta}`（409 后客户端重试语义依赖它，RFC 7644 §3.12；meta 见 §8a） | 200 |
| /Users/{id} | PUT | **支持**：replace `{userName, active}`。userName 必须等于已绑定 subject（否则 400 mutability，readOnly 属性不可改）；active 触发状态迁移（含 re-enable） | 200 |
| /Users/{id} | PATCH | **支持子集**：仅 `op=replace` + `path=active` + value 布尔；其他 op → 501，其他 path → 400 noTarget，value 非布尔 → 400 invalidValue | 200 |
| /Users/{id} | DELETE | **显式不支持**（不删除历史 actor 引用；生命周期终点是 disable） | 501 |
| /Groups | POST | **支持**：externalId ≡ displayName ≡ group name（否则 400 mutability）；members 可选（初始成员集，幂等 add） | 201 |
| /Groups | GET | **支持**：list（`startIndex`/`count` 基础分页，ListResponse 五字段见 §8a）；`filter` 参数 → 400 + scimType `invalidFilter`（RFC 7644 §3.4.2.2） | 200 |
| /Groups/{id} | GET | **支持**：get_group + list_group_members | 200 |
| /Groups/{id} | PUT | **支持**：member reconciliation 全量 replace（diff：add 缺失 + remove 多余，同一 tenant 事务原子）；displayName 必须等于 group name（改名 → 400 mutability，S1 不支持 rename） | 200 |
| /Groups/{id} | PATCH | **显式不支持**（PUT replace 即 reconciliation 面） | 501 |
| /Groups/{id} | DELETE | **显式不支持**（group 删除生命周期未在冻结 spec 定义） | 501 |
| /Bulk | POST | **显式不支持** | 501 |
| /Me | GET | **显式不支持** | 501 |
| /ServiceProviderConfig | GET | **显式不支持** | 501 |
| /ResourceTypes | GET | **显式不支持** | 501 |
| /Schemas | GET | **显式不支持** | 501 |
| /.search | POST | **显式不支持** | 501 |
| 未注册 /scim/v2 路径 | * | FastAPI 默认 404（注册为 INFO 限制，见 §13） | 404 |

补充冻结：

- **If-Match/If-None-Match 头**：S1 不实现版本化（不 emit ETag）；携带该头 → 400 显式
  拒绝（fail closed，RFC 7644 §3.14 允许 server 不支持 versioning）。
- **路径参数以 str 接收、手工解析 UUID**：解析失败 → 400 SCIM 错误体（scimType
  invalidValue），避免 FastAPI 422 非 SCIM 形状错误体（router 级 exception handler
  的可用性不成为契约依赖，见 §8）。
- **读路径与写路径统一经 policy gate（ORG/MANAGE）**：principals 是 identity-global
  无 RLS（0002 实证），「与 /api/v1 读路径类比」不成立——/api/v1 读有租户谓词 +
  FORCE RLS 兜底，SCIM 读无租户数据可依，必须由 gate 承担作用域授权（§2）。唯一授权
  机制仍是 gate（同一矩阵 cell org.manage），不建第二套权限判定。

## 2. 端点认证方案（候选 A；候选 B 遗留）

- **候选 A（选定）**：OIDC BFF 会话。SCIM 客户端以 org 管理身份（org_owner 矩阵
  `org.manage`）登录获得会话 cookie；请求携带 cookie + CSRF + 可信 Origin +
  `X-ZhiWei-Organization`（Groups 另需 `X-ZhiWei-Workspace`）。身份与 tenant context
  复用既有 `session_actor` 依赖（`resolve_context` 验证 membership）——**不造第三套
  auth**。
- **读写统一经 gate**：每个 SCIM 端点（GET 与 mutation）先经
  `api.policy_gate.authorize_mutation`（policy_type=ORG、policy_action=MANAGE、
  resource_id 见 §10），deny → 403 + denied 审计。**理由（第一轮 blocking 2 裁决）**：
  principals 是 identity-global 无 RLS，「/api/v1 读靠 RLS+租户谓词」的类比在 SCIM
  User 读上不成立——SCIM 读必须由 gate 做作用域授权，否则任何 org 成员可读任意用户
  userName/active/存在性，违反 PERMISSIONS §3.1（Member 仅读自身）。授权机制仍是
  唯一 gate（同一矩阵 cell），不是第二套权限判定。allowed 读**不写** allowed 审计
  （T4 只审计 mutation 与 denied）；denied 读写 denied 审计（枚举探测留痕）。
- **issuer 来源（第一轮 blocking 1 裁决）**：POST /Users 的绑定键 `(issuer, subject)`
  之 issuer = **`ZHIWEI_OIDC_ISSUER`（部署期固定，create_app 组合时注入 ScimService）**。
  请求体不携带 issuer（`extra="forbid"` 拒绝该属性），客户端不可声明；理由：S1 单
  IdP 部署，OIDC 登录与 SCIM 供给同一 issuer（local-product Keycloak 同源）；issuer
  错配会让登录恒 UnknownPrincipalError。多 IdP 形态登记遗留（§13）。
- **候选 B（machine credential / OAuth client credentials）**：属 S2 范围，生产凭证
  形态登记为遗留（§13）。S1 本地验证用测试模拟 SCIM 客户端（真实 BFF 登录 +
  FakeIdP + FakeOPA；slow 用例用真实 OPA 边车）。
- SCIM 端点用同一 `actor_dependency` 的**表现层包装**：捕获 HTTPException 后以
  SCIM 错误体重抛（状态码不变）——是错误体转换，不是第二套认证/授权。

## 3. schema 影响与迁移

- **User：零 schema 改动**。绑定键 `(issuer, subject)` 落 `external_identities`（既有
  表，PK 即 (issuer, subject)）；disable 用既有 `principals.status`。
- **Group：零新列/新表**。裁决：**externalId ≡ group name**，唯一范围 (org, workspace)
  由既有 `uq_groups_scope_name`（0002）强制——重复 externalId 的 POST 自然撞唯一约束
   → NameConflictError → 409 uniqueness。客户端必须 externalId == displayName（否则
   400 mutability，见 §1）。备选方案（新 external_id 列 + 部分唯一索引）因必需子集不含 rename
  而否决：不为未冻结的能力扩 schema。SCIM 与非 SCIM 创建的 group 统一在同一键空间，
  不产生第二套标识。
- **迁移 0009_scim_group_member_delete**（`down_revision="0008_bootstrap_claims"`——
  revision id 而非文件名）：
  - upgrade：`GRANT DELETE ON TABLE group_members TO zhiwei_app`（0002 只给 SELECT,
    INSERT；reconciliation 的 remove 需要 DELETE）；
  - downgrade：REVOKE DELETE。
  - 无 DDL 表结构变化；**RLS 语义不变**（group_members 既有 FORCE RLS policy 覆盖
    表级全部行；GRANT 不影响 RLS 过滤；zhiwei_app 非 owner、无 BYPASSRLS，既有事实）。
  - identity 引擎侧零改动：zhiwei_identity 已有 principals UPDATE(status)（0003 列级
    授权）与 external_identities SELECT/INSERT，set_principal_status / 按 principal
    读 external identity 均被既有授权覆盖。

## 4. 幂等与重试语义（对照 RFC 7644 §3.4.1/§3.12）

- **同 externalId 重复 POST /Users** → ExternalIdentityConflictError → **409** +
  scimType=uniqueness + failed 审计（reason 落 T4 默认码 `business_rejection`——
  policy_gate 白名单外零改动，`_FAILED_REASONS` 不新增映射，见 §10）。
- **同 displayName(=externalId) 重复 POST /Groups**（同 workspace）→ NameConflictError
  → **409** + uniqueness + failed 审计（reason=`name_conflict`，T4 既有映射）。
- **reconciliation 重复 payload 零副作用**：PUT 与当前成员集 diff 为空 →
  `changed=False` → 不写 audit/outbox、不触发任何 INSERT/DELETE，返回 200 + 资源体
  （与 T4 幂等重放 created=False 语义一致）。
- **client retry 语义**：RFC 7644 §3.4.1 创建冲突（409）后，客户端以 GET /Users/{id}
  或 GET /Groups/{id} 查询定位既有资源；两个 GET 均支持（§1）。
  POST 重试不产生重复行（唯一约束 + `on_conflict` 语义在命令层已有）；**并发**同键
  双 POST → 一方 201 一方 409，败方在 **identity 引擎事务**内整体回滚
  （bind_external_identity 唯一约束冲突 → 异常 → 事务回滚），principals/
  external_identities 零残留——测试直读计数钉死（§11）。
- SCIM 端点**不要求 Idempotency-Key**（RFC 7644 协议面无此头约定；幂等由资源自然键 +
  diff 语义保证）。与 /api/v1 mutation 的 Idempotency-Key 约定差异是协议边界差异，
  不是两套幂等机制。
- 状态迁移幂等：PATCH/PUT 目标状态与当前一致 → `changed=False` → 200，零副作用。

## 5. disable 语义与接线点

- 入口：PATCH `active=false` 或 PUT `{active: false}` → `commands.set_principal_status`
  （identity 引擎 `UPDATE principals.status='disabled'`，identity 事务）。
- **阻断新 session（T2 既有行为，零改动验证）**：
  - 新登录：`sessions.py:_resolve_login_principal` 对 disabled principal 抛
    PrincipalLoginDeniedError → callback 403 login failed；
  - 既有 session 下次请求：`authenticate_cookie` 每请求重读 principal → disabled →
    返回 None → 401 + 清 cookie。
- **阻断新 command（T1 既有行为，零改动验证）**：`commands._require_active` /
  `IdentityRepository._require_active` → PrincipalDisabledError → API 409。
- **审计与 outbox**：状态实际迁移（changed=True）→ gate allowed 审计
  （action `scim.user.disable` / `scim.user.enable`，metadata 三类语义与 T4 逐字一致）
  + 同事务 outbox `audit.decision`。changed 判定：命令层先读当前 status，与目标一致
  → `(principal, False)` 零写入；不一致 → UPDATE 后 `(principal, True)`（read-then-CAS
  的单写者语义足够——status 迁移是幂等值替换，无并发校验需求，且 identity 引擎
  事务内完成）。
- **不删除历史 actor 引用**：disable 只改 status 列；DELETE /Users → 501；
  external_identities / memberships / audit 行永不因 disable 删除。
- re-enable（active=true）走同一 `set_principal_status`，对称恢复登录与 command。

## 6. JIT 接线（operator 已批准口径）

- **冻结口径**：JIT 与 SCIM 是同一族 provisioning mutation（create principal + bind
  external identity [+ org membership]），**统一走 `authorize_mutation`，policy 先于
  事务**；deny → 拒绝 + denied 审计；不得因「Rego 规则尚未交付」而放宽或静默放行。
- T5 落地：全部 provisioning mutation（用户 create/状态迁移、group create/reconcile）
  经 gate；测试钉死「policy deny → provisioning mutation 拒绝 + 审计」（FakeOPA deny
  + slow 真实 OPA deny 双路径）。
- **遗留（随策略交付，同 T4 bootstrap 例外模式措辞）**：登录时 JIT 触发器（sessions.py
  接线点）、JIT 目标 org 来源（IdP claim 契约）、Rego JIT 授权规则。S1 生产登录行为
  不变：未 provisioning 的身份登录被 T2 拒绝（UnknownPrincipalError，fail closed）。
- sessions.py / api/auth.py 零改动。

## 7. 白名单扩展（operator 批准：全 additive）

| 文件 | 增量 | 说明 |
| --- | --- | --- |
| commands.py | `set_principal_status(repository, principal_id, status) -> tuple[Principal, bool]` | 镜像 disable_principal（identity 引擎路径），返回 changed 供审计判定 |
| commands.py | `remove_group_member(repository, *, group_id, organization_id, workspace_id, principal_id) -> bool` | 镜像 add_group_member；无幂等 claim（SCIM 无 Idempotency-Key，diff 语义天然幂等） |
| commands.py | `create_user(..., principal_id: UUID \| None = None)` | 增可选参数：SCIM 服务预生成 id 供 gate policy input（resource_id 先于事务） |
| repositories.py | `IdentityStore.set_principal_status(principal_id, status) -> Principal \| None` | UPDATE status RETURNING（既有列级授权覆盖） |
| repositories.py | `IdentityStore.get_external_identity_by_principal(principal_id) -> ExternalIdentity \| None` | 按 principal 读绑定（GET /Users/{id} 的 userName；ORDER BY issuer, subject LIMIT 1 确定性） |
| repositories.py | `IdentityRepository.remove_group_member(...) -> bool` | DELETE RETURNING，tenant guard 同 add_group_member |
| migrations | `0009_scim_group_member_delete` | 仅 GRANT DELETE on group_members（§3） |

禁用既有命令保持不动；disable_principal 命令不修改（SCIM 双向状态迁移统一走
set_principal_status，T1 路径不受影响）。

## 8. 错误体与表现层

- 全部 SCIM 4xx/5xx 使用 RFC 7644 §3.12 错误体：
  `{"schemas":["urn:ietf:params:scim:api:messages:2.0:Error"],"status":"<code>","scimType":...?,"detail":"..."}`
  （status 为**字符串**，与 RFC 7644 §3.12 示例逐字一致）。
- scimType 使用：`uniqueness`（409 重复键）、`mutability`（400 PUT 尝试修改不可变
  绑定：PUT userName≠subject）、`invalidValue`（400 结构值非法：POST userName≠
  externalId、POST/PUT externalId≠displayName、UUID 解析失败、PATCH value 非布尔、
  成员 principal 不存在或 disabled）、`noTarget`（400 PATCH path 不受支持）、
  `invalidFilter`（400 GET filter 参数不受支持，RFC 7644 §3.4.2.2）、`invalidSyntax`
  （400 body 畸形 / 未知属性——Pydantic extra=forbid 触发，RFC 7644 §3.12 语义）；
  401/403/404/501 仅 detail。
- 实现：端点内 try/except + `session_actor` 表现层包装把 HTTPException 转成 SCIM 形状
  （状态码不变，detail 保留原始语义：policy denied / outside tenant scope /
  organization context required / session required …）。401 时 request.state 的
  clear_session_cookie 语义不变（中间件继续清 cookie）。
- 未知 /scim/v2 路径 → FastAPI 默认 404（无 app 级全局 404 定制，不侵入非 SCIM 路由；
  INFO 限制）。**未捕获异常（500）返回 FastAPI 默认错误体**（非 SCIM 形状）——SCIM
  形状保证覆盖所有 SCIM 控制内拒绝；不为 500 加全局 handler（会侵入非 SCIM 路由），
  登记 INFO 限制（§13）。

## 8a. 成功响应体（第一轮 blocking 4 裁决）

- **User 资源**（POST/GET/PUT/PATCH 返回体）：
  `{"schemas":["urn:ietf:params:scim:schemas:core:2.0:User"],"id":"<principal_id>",
  "externalId":"<subject>","userName":"<subject>","active":bool,"meta":{...}}`；
  meta 含 `resourceType:"User"`、`created`、`lastModified`（principals.created_at——
  principals 无 updated_at 列，lastModified 恒等于 created_at，登记为偏差）、
  `location:"/scim/v2/Users/{id}"`。**meta.version 省略**：S1 不支持版本化（RFC 7644
  §3.14 允许；If-Match → 400），登记为对 RFC 7643 §3.1 meta.version 的偏差（§13）。
- **Group 资源**：`{"schemas":["urn:ietf:params:scim:schemas:core:2.0:Group"],
  "id","externalId":name,"displayName":name,"members":[{"value":"<principal_id>"},...],
  "meta":{...}}`。members[].display 省略（RFC 7643 该属性可选，避免额外的 identity
  读）。
- **ListResponse**（GET /Groups）：五字段全出——
  `{"schemas":["urn:ietf:params:scim:api:messages:2.0:ListResponse"],"totalResults",
  "itemsPerPage","startIndex","Resources":[...]}`（RFC 7644 §3.4.2；itemsPerPage/
  startIndex 反映请求参数，缺省 startIndex=1、count=100，count 超 1000 截断为 1000）。
- POST 成功响应带 `Location: /scim/v2/Users/{id}` / `/scim/v2/Groups/{id}`（RFC 7644
  §3.4.1）。

## 9. 审计 action 命名（新 taxonomy，登记）

| action | resource_type | 触发 |
| --- | --- | --- |
| scim.user.create | principal | POST /Users（created） |
| scim.user.disable | principal | 状态迁移到 disabled（changed） |
| scim.user.enable | principal | 状态迁移到 active（changed） |
| scim.group.create | group | POST /Groups（created） |
| scim.group.reconcile | group | PUT /Groups 成员 diff 非空（changed） |

- metadata 三类语义与 T4 逐字一致：allowed 携带真实 decision_id/policy_revision/
  decision_reason；denied（OPA/不可达/本地）双 NULL + 固定 reason；failed 双 NULL +
  映射码。resource_version：allowed=1、denied/failed=0。
- 幂等 no-op（changed=False）不写审计/outbox（与 T4 幂等重放一致）。
- User 类 mutation 的审计 scope = actor org（identity-global 资源本身无租户列，审计
  落 actor context）；group 类 = actor org + workspace。

## 10. policy gate 接线（硬约束 1）

- SCIM 全部端点（读 + mutation）经 `authorize_mutation`（policy_type=ORG、
  policy_action=MANAGE，Rego 矩阵 `org.manage: {org_owner}`）——**不复制 gate 逻辑、
  不建第二套权限矩阵**。read 与 mutation 的区别只在审计：mutation 成功后写 allowed
  审计；读成功不写审计（T4 只审计 mutation 与 denied）。
- gate 参数：User 类 `organization_id=actor.organization_id, workspace_id=None`；
  Group 类 `organization_id=actor.organization_id, workspace_id=actor.workspace_id`；
  resource_id：create 类为预生成 id（§7），其余为目标资源 id。
- 业务拒绝走 `append_failed_mutation_audit`：ExternalIdentityConflictError →
  `business_rejection`（policy_gate.py 的 `_FAILED_REASONS` 白名单外零改动，T4 默认
  码语义），NameConflictError → `name_conflict`（既有）。
- 跨租户：SCIM 端点的 target 恒为 actor 自己声明的 org/ws（headers 经 membership
  resolver 验证）；对 group id 的跨租户猜测 → repository tenant guard + RLS → 404
  （读语义统一防枚举），gate 不会读到目标租户。

## 11. 测试计划

### tests/contract/identity/test_scim.py（无 DB；router 手工组装 + stub actor + FakePolicyEnforcer）

- 组合契约：`create_scim_router(policy_enforcer=None)` → TypeError（fail closed）；
  缺 sessions/identity_sessions → TypeError。
- 501 矩阵逐端点断言 + SCIM 错误体形状。
- 400 校验：未知属性（invalidSyntax）、externalId 缺失/空、POST userName≠externalId
  （invalidValue）、PUT userName≠subject（mutability）、POST/PUT externalId≠displayName
  （invalidValue）、PATCH op=add/remove（501）、PATCH path≠active（noTarget）、PATCH
  value 非布尔（invalidValue）、filter 参数（invalidFilter）、If-Match、非法 UUID 路径
  参数（str 解析路径，invalidValue）。
- 认证/上下文：无 cookie 401、CSRF 缺失/不匹配 403、无 org header 403（读与写都
  要求 org context；以上全部在触达 DB 之前，真实拒绝行为）。
- 这些用例在 RED 阶段以 ImportError（create_scim_router 不存在）失败——契约面缺失
  是正确失败原因（新模块 RED 惯例，T4 同款：组合契约缺失）。

### tests/integration/identity/test_scim_lifecycle.py（真实 DB + create_app + FakeIdP + FakeOPA）

RED 阶段**不 import SCIM 模块**：走 create_app（既有）+ HTTP → /scim/v2/* 得到真实
404 → 断言 201/200/501 失败——反例到真实 HTTP 行为。

- 生命周期：owner 创建 user（201 + Location + 资源体含 meta；DB principals/
  external_identities 直读计数）→ 重复 externalId 409 + uniqueness → GET /Users/{id}
  200 → GET/PUT/PATCH /Users/{unknown-uuid} 404（未知 id 钉死 404）→ PATCH disable
  → DB status=disabled + 审计/outbox → 既有 session 下次请求 401 → 新登录 403 →
  add member（禁用主体）400 invalidValue → PUT re-enable → 登录恢复。
- **并发重复 externalId**：asyncio.gather 双 POST 同键 → 一方 201 一方 409；
  principals/external_identities 计数 = 1 行（败方事务整体回滚，零残留）。
- **disable 行存活断言**：disable 后 principals/external_identities/memberships/
  group_members/audit 行数全部不变，仅 principals.status 变化。
- group：POST 201 → 重复 externalId 409 → PUT reconcile [a] → 重放零副作用（四表
  migrator 直读计数不变）→ PUT [a,b] 增员 → PUT [a] 删员（0009 DELETE 授权实证）→
  displayName 不匹配 400 → PATCH/DELETE 501 → GET /Groups 分页（totalResults/
  itemsPerPage/startIndex/Resources）。
- 对抗探针：跨租户猜 group id（org_b actor）404；无权限 SCIM（FakeOPA deny）403 +
  denied 审计 + 业务零写入；FakeOPA 不可达 403 fail closed + 审计；FakeOPA inputs
  断言 policy input（action=manage、resource.type=org、actor roles 来自真实 membership）；
  **读路径授权**：member（非 owner）GET /Users/{id} 与 GET /Groups → 403 + denied 审计
  （读也经 gate，第一轮 blocking 2 裁决）；无 org header 的 GET → 403。
- 审计 metadata 逐字段断言与 T4 三类语义一致；幂等 no-op 不追加 audit/outbox。
- slow（真实 OPA 边车，skip-guard 无 docker 跳过）：org_owner SCIM create → 201 +
  真实决策 metadata；member（非 owner）SCIM create → 真实 deny → 403 + denied 审计。

## 12. RED 失败证据与提交边界

- RED 预期失败：contract 文件 ImportError（模块缺失）；integration 文件全部用例
  404 vs 201/200/501 断言失败。原始输出保存 `artifacts/gates/s1-t5/red/`。
- 提交序列：
  1. `test(identity): red freeze scim lifecycle contracts`（仅两个测试文件）；
  2. `feat(identity): add scim lifecycle`（scim.py、api/scim.py、app.py、commands.py、
     repositories.py、0009 迁移）；
  3. `docs(identity): hand off s1-t5 scim lifecycle`（交接单 + 设计链接 + 验收记录）。
- GREEN 后 tests/ 锁定：`make handoff-check HANDOFF_BASE=<RED>` exit 0。

## 13. 遗留事项登记（不阻塞）

- machine credential（SCIM bearer/OAuth client credentials）形态：S2；
- 登录时 JIT 触发器、目标 org 来源、Rego JIT 授权规则：随策略交付；
- GET /Users 列表、filter（Users 侧）、Bulk、/Me、ServiceProviderConfig、group
  改名/删除、ETag 版本化：显式 501/400，未来按需扩展（扩展时补子集矩阵裁决）；
- 未注册 /scim/v2 路径返回 FastAPI 默认 404 体（非 SCIM 形状）；未捕获异常 500
  返回 FastAPI 默认体（非 SCIM 形状）——SCIM 形状保证覆盖全部 SCIM 控制内拒绝；
- SCIM 409 failed 审计 reason 使用 T4 默认码 `business_rejection`（ExternalIdentityConflictError
  未进 `_FAILED_REASONS`，白名单外零改动；若验收要求精确码，回本裁决改 policy_gate 白名单）；
- meta.version 省略（对 RFC 7643 §3.1 meta.version 的偏差）：S1 不支持版本化；
  另 lastModified 恒等于 created_at（principals 无 updated_at 列）；
- **本子集不声称完整 SCIM 2.0 conformance**（缺列表搜索/版本化/发现端点）；对外表述
  用「SCIM 2.0 必需子集」；
- POST 成功响应只发 Location，不发 Content-Location（RFC 7644 §3.4.1 语义；登记
  对齐）;
- userName≡subject 的唯一性是 per-issuer（S1 单 IdP 部署内有效；多 IdP 需 per-issuer
  namespace 裁决）；externalId≡displayName 与主流 IdP（Okta/Entra）互操作风险——
  本子集显式拒绝不相等 payload，客户需配置 externalId=displayName；
- 本裁决（externalId≡name 映射、issuer=OIDC_ISSUER、读也经 gate）建议由 operator
  在阶段 Gate 时并入 docs/DECISIONS.md（治理文件只读，本任务不直接改动）。
