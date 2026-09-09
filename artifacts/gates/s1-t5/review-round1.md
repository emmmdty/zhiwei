# S1-T5 设计裁决 — 第一轮独立验收记录（重跑，覆盖旧记录）

> 被审对象：`docs/handoffs/s1-t5-design.md`（执行方设计裁决，修订后，待 RED 冻结）
> 验收方：第一轮独立验收重跑（未参与实现，只读；本文件是唯一写入物，gitignored）
> 审查依据：冻结事实源（总设计 §9.1/§9.4、specs/s1 §3/§5、Plan Task 5、PERMISSIONS §3.1、
> s1-t4.md §3/§10/§12）+ 代码现状（app.py/config/settings.py/oidc.py/sessions.py/commands.py/
> repositories.py/persistence/tenant.py/policy_gate.py/models.py/0002/0003/0008）+ 本机联机抓取的
> RFC 7643/7644 固定版本文本（rfc-editor.org）。
> 上轮旧记录仅作对照（5 个 blocking 的闭合核验），本重跑对全部审查面独立复核，不采信旧结论。
> 结论：**通过（0 blocking / 11 INFO）**。5 个 blocking 全部闭合，修订未引入新矛盾；INFO 均为
> 登记/措辞/机械性修正，不改变方向性设计。

---

## 0. 总评

修订后的设计在以下面向上与冻结事实源、代码现状逐项吻合（验证详下）：

- 5 个上轮 blocking 全部闭合，且闭合方式与既有机制逐字兼容（issuer 走 create_app 既有组合路径、
  读 gate 复用 `authorize_mutation` 单一机制、400 invalidValue 两处统一、meta/ListResponse 对齐
  RFC 逐字 MUST、invalidFilter 对齐 RFC 7644 §3.4.2.2）；
- 认证复用 BFF 会话 + `session_actor` 表现层包装，不造第三套 auth；读/写统一经 gate 是唯一授权
  机制（同一 Rego 矩阵 cell org.manage）；
- 白名单扩展全 additive、`create_user` 可选参数零破坏既有调用点；
- disable 接线与 sessions.py/commands.py/auth.py 现状逐行吻合；
- 幂等语义与 s1-t4.md §3 逐字一致，无 Idempotency-Key 不构成第二套幂等机制；
- RED 失败机制规避了 T4 被拒教训（integration 不 import SCIM 模块，走 create_app 真实 404）。

**不通过原因不存在**。上轮 5 项 blocking 已全部补齐为明确契约裁决；本轮新发现均为登记类/
措辞类 INFO（含 1 处迁移 down_revision 字符串机械错误、meta.version「偏差」定性不准、
lastModified 语义偏差登记、identity 事务回滚措辞、跨 org owner 读暴露登记等）。

---

## 一、上轮 5 个 blocking 专项核验（闭合状态）

### B1. POST /Users 的 issuer 来源 → 闭合 ✓

设计 §2：issuer = `ZHIWEI_OIDC_ISSUER`（部署期固定），create_app 组合时注入 ScimService；
请求体不可声明 issuer（`extra="forbid"`）。

- **create_app 注入路径实证**：`src/zhiwei/app.py:46` `_REQUIRED` 含 `("ZHIWEI_OIDC_ISSUER",
  "oidc_issuer")`（缺失 → 组合期拒绝，fail closed）；`app.py:67/76-87` 读 `settings.oidc_issuer`
  并断言非 None；`app.py:105-111` 既有 `OIDCService(issuer=oidc_issuer, ...)` 组合模式。
  `ScimService(issuer=oidc_issuer, ...)` 走同一路径，支撑成立 ✓。`config/settings.py`（实际在
  `src/zhiwei/config/settings.py:76,157`）`oidc_issuer: str | None` 来自 `ZHIWEI_OIDC_ISSUER`，
  Settings 冻结不可改写 ✓。
- **extra=forbid 一致性**：RFC 7643 §3.1/§4.1 core User schema 无 issuer 属性；设计 User 子集
  （schemas/userName/externalId/active）亦无 issuer；`extra="forbid"` → 未知属性 400 ✓ 表述一致。
- **与 T2 登录绑定键一致性——按构造成立（不依赖部署巧合）**：登录路径
  `sessions.py:522-524` `issuer = claims["iss"]`，而 `validate_id_token`（oidc.py:154）强制
  `claims["iss"] == expected_issuer`，其中 `expected_issuer = attempt.issuer = OIDCService._issuer`
  （oidc.py:226、263-267）＝ 同一份 `ZHIWEI_OIDC_ISSURE` 配置。SCIM 以配置值 bind、登录以经
  校验的 claims.iss 查找，二者恒相等 → 绑定键 `(issuer, subject)` 一致性由验证链保证，issuer
  错配导致的「登录恒 UnknownPrincipalError」缺口关闭 ✓。

### B2. GET /Users/{id} 跨租户读授权 → 闭合 ✓

设计 §1/§2/§10：SCIM 读与写统一经 `authorize_mutation`（ORG/MANAGE）；allowed 读不写审计；
denied 读写 denied 审计。

- **机制复用成立**：`policy_gate.py:247-328` `authorize_mutation` 自身只写 denied 审计
  （`_write_denied_audit`，独立事务）；allowed 审计由端点经 `append_allowed_audit`
  （policy_gate.py:420-448）在同一业务事务内追加——读端点「调 gate、不调 append_allowed_audit」
  即精确得到「allowed 读零审计、denied 读写审计」语义，零 gate 复制 ✓。
- **与 T4 审计语义逐字对照**：总设计 §9.4 冻结的是「成功和拒绝 **mutation** 同事务写 Audit
  outbox」；s1-t4.md §3 的 metadata 三类规则（allowed 真实决策、denied 双 NULL+固定码或真实
  决策、failed 双 NULL+映射码）约束 mutation 记录本身。读审计是 T4 冻结面之外的新增量：denied
  读记录复用 `denied_audit_record`（policy_gate.py:116-150），metadata 形状与 T4 denied 路径
  完全一致（decision_id/policy_revision 取真实决策、本地拒绝为 NULL），**不破坏 T4 任何冻结
  断言**（T4 测试不含 SCIM 读路径，零触碰）✓。denied 读写 outbox 与 T4「跨租户猜 ID → +1
  denied audit + outbox」（s1-t4.md §3 表）同构，无越界。
- **枚举探测 → 审计噪音**：与 T4 既有 denied 审计（跨租户猜测即留痕）同哲学，且是有意为之
  （「枚举探测留痕」）；噪音换 fail-closed 可审计性，可接受 ✓。
- **403 vs 404 存在性 oracle**：非 owner（Member）读任何 id → gate 403，与资源存在性无关 →
  成员层无 oracle ✓；owner 层读为已授权面。残余：**任意 org 的 owner 可读任意 principal**
  （identity-global，0002 无 RLS、无租户列——实证见下）——跨 org owner 可见性未在 §13 登记
  （见 INFO-5，登记即可，非 blocking）。
- 实证「principals 无 RLS」：0002_identity.py:29-36 `_TENANT_RLS_POLICIES` 只含 memberships/
  workspace_memberships/groups/group_members；principals/external_identities 无 RLS 无租户列 ✓。

### B3. 禁用成员入组 400 vs 409 矛盾 → 闭合 ✓

设计 §8 scimType 表：「成员 principal 不存在或 disabled → 400 invalidValue」；§11 生命周期：
「add member（禁用主体）400 invalidValue」——两处统一为 400 invalidValue ✓。链路实证：
`add_group_member` → `_require_active`（commands.py:228-236）→ `PrincipalDisabledError`
（repositories.py:644-649），API 映射 400 invalidValue；T4 `_FAILED_REASONS` 既有
`principal_disabled`（policy_gate.py:66）可支撑 failed 审计 ✓。RFC 7644 §3.12 Table 9
invalidValue「value specified was not compatible with the operation or attribute type」覆盖该
情形 ✓。

### B4. 成功响应体 meta 缺失 → 闭合 ✓

设计 §8a：User 资源 meta{resourceType, created, lastModified, location}；Group 资源同；ListResponse
五字段全出。

- RFC 7643 §3.1 逐字核对：「When accepted by a service provider (e.g., after a SCIM create),
  the attributes 'id' and 'meta' (and its associated sub-attributes) MUST be assigned values
  by the service provider」；meta 子属性 resourceType/created/lastModified/location/version——
  resourceType（RFC 7644 §3.3.1「SHALL be set…to the corresponding resource type」）、created
  （DateTime）、lastModified（未修改过 MUST 等于 created）、location（URI）→ 设计全部赋值 ✓。
  RFC 7644 §3.3：201 响应「SHALL include, in the HTTP 'Location' header and the HTTP body, a
  JSON representation with the attribute 'meta.location'」→ 设计 POST 带 Location 头 + 资源体
  meta.location ✓。
- **meta.version 省略**：RFC 7643 §3.1 version「Service provider support for this attribute is
  optional and subject to the service provider's support for versioning (see Section 3.14 of
  [RFC7644])」；RFC 7644 §3.14「Service providers MAY support weak ETags…」——versioning 是 MAY，
  省略 version 属 RFC 合规行为，**不是偏差**。设计 §8a/§13 称「登记为对 RFC 7643 §3.1
  meta.version 的偏差」定性不准 → INFO-2（措辞修正，闭合成立）。
- **ListResponse 五字段**：RFC 7644 §3.4.2 逐字：totalResults REQUIRED；Resources「REQUIRED if
  'totalResults' is non-zero」；startIndex/itemsPerPage「REQUIRED when partial results are
  returned due to pagination」。设计恒定输出五字段（含分页时语义正确），为 REQUIRED 的超集，
  合规 ✓。分页参数：§3.4.2.4 Table 6 startIndex 默认 1 ✓、count 未指定时上限由 provider 设定
  （设计默认 100、截断 1000）✓、count=0 返回仅 totalResults（设计未覆盖该边界，RED 可钉）—
  不阻塞。

### B5. filter 拒绝缺 scimType → 闭合 ✓

设计 §8 scimType 集合现含 `invalidFilter`（GET filter 参数拒绝专用），§1 矩阵同步标注
「RFC 7644 §3.4.2.2」✓。RFC 7644 §3.4.2.2 逐字：「Providers MUST decline to filter results
if the specified filter operation is not recognized and return an HTTP 400 error with a
'scimType' error of 'invalidFilter'」——设计行为（filter 一律 400 + invalidFilter）满足该 MUST
（filtering 本身是 OPTIONAL 能力，拒绝即「不识别」的合法表达）✓。

### 修订未引入新矛盾的复核

- B1/B2 交叉：读端点也要求 org context（actor.organization_id None → 403，policy_gate.py:272-275）
  与 §11「无 org header 403（读与写都要求 org context）」一致 ✓。
- B2/B4 交叉：GET /Users/{id} 返回体含 meta ✓（读与写同一资源体形状）。
- B3/§1 交叉：POST /Groups members 初始成员集含禁用主体 → 同一 400 invalidValue 链路 ✓。

---

## 二、完整重审面（①—⑩，独立复核）

### ① 子集矩阵 vs RFC 7643/7644 与任务书必需子集

| 裁决 | RFC 依据（本机抓取逐字） | 核对 |
| --- | --- | --- |
| 重复 externalId POST /Users → 409 uniqueness | §3.3「duplicate 'userName'…MUST return HTTP status code 409 (Conflict) with a 'scimType' error code of 'uniqueness'」 | ✓ |
| 重复 displayName POST /Groups → 409 uniqueness | 同上 + uq_groups_scope_name（0002:177）撞约束 → NameConflictError（repositories.py:502-504） | ✓ |
| PATCH 仅 replace/active；其他 op → 501、path → noTarget、值非布尔 → invalidValue | §3.5.2 PATCH 为 OPTIONAL；Table 8 501「does not support the request operation, e.g., PATCH」；Table 9 noTarget（path 未产生可操作属性）、invalidValue | ✓ |
| PUT 仅既有资源、MUST NOT create | §3.5.1「HTTP PUT MUST NOT be used to create new resources」 | ✓（未知 id → 404，RED 钉，INFO-8） |
| PUT userName≠subject → 400 mutability | §3.5.1 immutable：「If one or more values are already set…the input value(s) MUST match, or HTTP status code 400 SHOULD be returned with a 'scimType' error code of 'mutability'」 | ✓（RFC 推荐码） |
| GET /Users 列表 → 501；/Bulk、/Me、发现端点 → 501 | §3.4.2 查询为 MAY；§3.11 /Me「does NOT support this feature SHOULD respond with 501」；§3.7/§4 MAY | ✓ |
| If-Match/If-None-Match → 400 | §3.14 versioning MAY；不支持时显式 400 fail closed，无 MUST 冲突 | ✓ |
| 未知属性 → 400（extra=forbid） | §3.1「SCIM service provider interprets a request in the context of its own schema」；项目 fail-closed 纪律优先于 §3.3 readOnly-ignore 许可 | ✓（任务书支持） |
| 错误体形状 | §3.12：status 为 JSON **字符串**且 REQUIRED；scimType OPTIONAL；detail OPTIONAL；Error URI | ✓ 设计 `"status":"<code>"` 逐字符合 |
| 401/403/404/501 无 scimType | §3.12「scimType…OPTIONAL」；Table 9 仅对 400 定义 detail error keywords | ✓ 合规 |

任务书必需子集（Plan Task 5 / specs/s1 §3,§5）：create/update/disable ✓、重复 external
identity 409 ✓、group reconciliation 双向 diff ✓、idempotent retries ✓、disable 阻断新
session/command + audit ✓、不删历史 actor 引用 ✓（§5 + §11 行存活断言）、JIT 由 policy 决定 ✓
（§6，operator 已批准）。re-enable 超覆盖 ✓。

### ② 认证方案：无第三套 auth；读 gate 与 /api/v1 读路径差异

- 候选 A（BFF 会话 + cookie + CSRF + Origin + org/ws header）复用 `session_actor`/
  `resolve_context`（membership 验证），RFC 7644 §2 明列 cookies 为合法方式（§7.4 安全注意
  适用）✓；候选 B（machine credential）登记 S2 遗留 ✓。
- 读/写统一经 gate 与 /api/v1 读路径差异的合理性：/api/v1 读端点（list members/groups）有租户
  谓词 + FORCE RLS 兜底（0002:29-36 RLS 矩阵实证）；SCIM User 读的 principals/external_identities
  是 identity-global 无 RLS（同上实证）——「RLS 类比」确实不成立，必须由 gate 承担作用域授权；
  gate 是唯一授权机制（同一 Rego 矩阵 cell org.manage，authz.rego 实证路径既有）✓ 差异合理。
- 表现层包装（HTTPException → SCIM 错误体，状态码不变）经既有中间件保持 401 clear_session_cookie
  语义（app.py:127-132）✓。

### ③ schema 裁决（externalId≡name、0009 GRANT、0002/0003 grants）

- externalId ≡ displayName ≡ name 零新列/新表：唯一范围 (org, ws) 由 uq_groups_scope_name
  （0002:177，三列 NOT NULL）强制；备选（新列+部分唯一索引）因必需子集不含 rename 而否决，
  裁决记录充分 ✓；externalId 唯一性 RFC 只要求 client 控制（7643 §3.1），workspace 级唯一
  无 MUST 冲突 ✓。
- 0009 GRANT DELETE 缺口实证：0002:219-221 group_members 仅 GRANT SELECT, INSERT → DELETE
  缺口属实；0009 全 additive；GRANT 与 FORCE RLS 正交（zhiwei_app 非 owner、无 BYPASSRLS，
  0002:234 FORCE RLS 实证）✓。
- identity 引擎授权：0003:189-191 zhiwei_identity GRANT SELECT, INSERT ON principals +
  UPDATE(status) + SELECT, INSERT ON external_identities → set_principal_status / 按 principal
  读 external identity 均被覆盖 ✓（设计 §3 主张属实）。
- ⚠ 0009 down_revision 字符串与 revision id 不符：设计 §3 写 `down_revision=0008_
  organization_bootstrap_claims`，而 0008 的 revision id 是 **`0008_bootstrap_claims`**
  （0008_organization_bootstrap_claims.py:37，文件名 ≠ revision id）。alembic 按 revision id
  解析，照抄会启动失败（失败是响亮的、自纠正的，非静默错误）→ INFO-1。

### ④ 白名单扩展全 additive

- commands.py `create_user(..., principal_id: UUID|None)`：既有调用点全量核对——
  tests/integration/identity/test_memberships.py:671,744,886 与 tests/unit/identity/
  test_identity_domain.py 全部以 `issuer=`/`subject=` 关键字调用，新增可选关键字零破坏 ✓；
  disable_principal 命令不动 ✓。
- repositories.py +3（set_principal_status / get_external_identity_by_principal /
  remove_group_member），全 additive、镜像既有模式（UPDATE…RETURNING / ORDER BY issuer,
  subject LIMIT 1 确定性 / DELETE RETURNING + tenant guard 同 add_group_member）✓。
- 冻结面零触碰：policy_gate.py、sessions.py、auth.py、Rego、evals/、0001~0008 声明只读 ✓；
  无新第三方依赖 ✓。

### ⑤ 幂等语义（409/零副作用/无 Idempotency-Key）vs T4

- s1-t4.md §3 逐字对照：「幂等重放 | 0 | 0 | 0（不重复追加）| 0」→ 设计「reconciliation 重复
  payload 零副作用：changed=False → 不写 audit/outbox、零 INSERT/DELETE、200 + 资源体」逐字
  一致 ✓；状态迁移幂等（目标状态一致 → changed=False → 200 零副作用）同语义 ✓。
- 409 冲突 + failed 审计 reason：ExternalIdentityConflictError → `business_rejection`（
  `_FAILED_REASONS` 白名单外零改动，policy_gate.py:60-67,393 实证；§13 已登记精确码回退裁决）✓；
  NameConflictError → `name_conflict`（既有）✓。
- 无 Idempotency-Key：RFC 7644 协议面无该头；SCIM 幂等 = 资源自然键 + diff 语义；与 /api/v1
  Idempotency-Key（S0 (org, ws, scope, key) 键空间）是协议边界差异，不是两套幂等机制，无冲突 ✓。
- **并发双 POST 自洽性**：`bind_external_identity`（repositories.py:109-124）ON CONFLICT DO
  NOTHING + RETURNING → 冲突时无行 → `ExternalIdentityConflictError` 实证 ✓；`create_user`
  （commands.py:198-212）先 get 预检、后 create_principal（flush 已发 INSERT）+ bind——并发同
  键双方过预检 → 一方 bind 胜出、败方异常。败方 principal 行能否清除取决于 **identity 引擎
  事务**回滚。⚠ 设计 §4 括注「tenant_session 回滚」用词不准：`tenant_session`
  （persistence/tenant.py:45-52）是 zhiwei_app 租户事务；principal/external_identity 写入走
  identity 引擎（IdentityStore），回滚必须发生在 identity 事务上。测试断言（principals/
  external_identities 计数 = 1 行）已把行为钉死，GREEN 必须按此实现 → INFO-4（措辞+实现提示）。

### ⑥ disable 接线（逐行吻合）

- 新登录：sessions.py:564-580 `_resolve_login_principal` disabled → `PrincipalLoginDeniedError`
  → auth.py:37-40/153-156 → 403 login failed ✓。
- 既有 session：sessions.py:584-604 `authenticate_cookie` 每请求重读 principal → disabled → None
  → auth.py:100-104 401 + clear_session_cookie ✓（app.py 中间件联动）。
- 新 command：commands.py:228-236 `_require_active` / repositories.py:644-649 →
  PrincipalDisabledError → 409/400 按端点点映射 ✓。
- re-enable 对称（同一 set_principal_status 双向）；不删历史 actor 引用（只改 status 列、
  DELETE /Users → 501、external_identities/memberships/audit 行永不因 disable 删除）✓；
  §11 现含显式行存活断言（principals/external_identities/memberships/group_members/audit 行数
  不变）——上轮 INFO-13 闭合 ✓。
- changed 判定：§5 现明确 read-then-CAS（先读 status，一致 → (principal, False) 零写入；不一致
  → UPDATE 后 True；单写者语义论证成立）——上轮 INFO-8 闭合 ✓。

### ⑦ JIT 冻结口径

- 全部 provisioning mutation 经 authorize_mutation、policy 先于事务、deny → 拒绝 + denied 审计；
  测试钉死 FakeOPA deny + slow 真实 OPA deny 双路径 ✓。
- 无静默放行：生产登录行为不变（未 provisioning → UnknownPrincipalError，sessions.py:569-572，
  fail closed）；登录时 JIT 触发器/目标 org 来源/Rego JIT 规则按 T4「随策略交付」模式登记遗留，
  措辞与 s1-t4.md §10/§12 一致 ✓；sessions.py/api/auth.py 零改动 ✓。

### ⑧ fail closed 全局

- 未知属性 400（extra=forbid）、未知操作/端点 501、策略不可达 deny + 审计、未注册路径 404
  （登记）、组合期缺依赖 TypeError（create_scim_router(policy_enforcer=None)/缺 sessions →
  TypeError，§11）、issuer 缺失 → create_app 组合期拒绝（_REQUIRED）——无放宽点 ✓。
- 未捕获异常 500 返回 FastAPI 默认体（非 SCIM 形状）已显式登记（§8/§13）——上轮 INFO-9 闭合 ✓。

### ⑨ RED 失败机制

- contract：ImportError（create_scim_router 不存在）——新模块契约面缺失是正确失败原因，T4 同款
  惯例 ✓。
- integration：**不 import SCIM 模块**，走 create_app（既有）+ 真实 HTTP → /scim/v2/* 得到
  真实 404 vs 201/200/501 断言失败——反例到真实行为，规避 T4 被拒教训（fixture 截断到不了
  真实反例）✓；RED 原始输出存 artifacts/gates/s1-t5/red/ ✓。
- 提交边界（RED → GREEN → docs）+ `make handoff-check HANDOFF_BASE=<RED>` ✓。

### ⑩ 对抗探针覆盖任务书第二轮全部项

- 任务书：create/update/disable、重复 external identity（409 + uniqueness + failed 审计）、
  group reconciliation 幂等（四表计数）、disable 阻断新 session/command + 审计、不删历史引用
  （行存活断言）✓ 全覆盖。
- 超任务书补充：跨租户猜 group id 404（防枚举）、FakeOPA deny 403 + denied 审计 + 业务零写入、
  OPA 不可达 403 + 审计、policy input 形状断言（action=manage、resource.type=org、roles 来自
  真实 membership）、**读路径授权**（member GET /Users/{id} 与 GET /Groups → 403 + denied 审计）、
  无 org header 的 GET → 403、审计 metadata 逐字段断言与 T4 三类语义一致、幂等 no-op 不追加
  audit/outbox、slow 真实 OPA 双路径（skip-guard 与既有 slow 纪律同款）✓。

---

## Blocking 清单

**无（0 项）。** 上轮 5 项全部闭合，闭合方式经冻结事实源、RFC 逐字文本与代码现状核验成立；
修订未引入新矛盾。

## INFO 清单（11 项，不阻塞）

1. **0009 down_revision 字符串错误**（修订引入，机械性）：设计 §3 写 `down_revision=0008_
   organization_bootstrap_claims`，实际 revision id 是 **`0008_bootstrap_claims`**
   （0008_organization_bootstrap_claims.py:37，文件名 ≠ revision id）。照抄会导致 alembic
   「Can't locate revision」启动失败——错误响亮、自纠正，但设计应先改对（GREEN 前修订 §3）。
2. **meta.version「偏差」定性不准**：RFC 7643 §3.1 明示 version 支持为 optional（subject to
   versioning support，RFC 7644 §3.14 版本化是 MAY）。省略 version 是合规行为，不是对 RFC 的
   偏差。§8a/§13 措辞应改为「省略（RFC 允许：version 随版本化支持可选）」。
3. **meta.lastModified 语义偏差未登记**：§8a 固定 lastModified = principals.created_at，但
   principals 无 updated_at 列（models.py:64-78，仅 created_at）；disable/enable 是资源修改，
   RFC 7643 §3.1 定义 lastModified 为「most recent DateTime that the details of this resource
   were updated」。保持恒等于 created 需在 §13 显式登记为已知偏差（或给 principals 加
   updated_at——与「零 schema 改动」冲突，建议登记）。
4. **§4 事务回滚措辞**：「败方事务整体回滚（…tenant_session 回滚）」用词不准：principal/
   external_identity 写入在 identity 引擎事务（IdentityStore 路径），非 zhiwei_app 的
   tenant_session。应写「identity 引擎事务回滚」；GREEN 必须把 create_user 包在单一 identity
   事务内（测试计数=1 已钉死行为）。
5. **跨 org owner 读暴露未登记**：非 owner 一律 403 无 oracle（B2 闭合成立），但任意 org 的
   org_owner 可经 GET /Users/{id} 读任意 principal 的 userName/active/created_at/存在性
   （identity-global 无租户谓词可依）。建议 §13 登记为已知限制（S2 缓解：membership 作用域读 /
   issuer 作用域读），不阻塞 S1。
6. **上轮 INFO-6 未修订**：§4 把「409 后客户端以 GET 查询定位资源」引为「RFC 7644 §3.12 错误
   处理语境」；§3.12 只定义错误体，实际出处是 §3.3（409 uniqueness 语境）与 §3.4.1（已知资源
   GET）。不影响实质。
7. **POST 上 mutability 的应用面**：§8 用 mutability 覆盖「POST externalId≠displayName」；
   Table 9 中 mutability 的 Applicability 只列 PUT/PATCH。PUT userName≠subject → mutability
   是 RFC §3.5.1 推荐码 ✓；POST 情形是注册表应用面的轻微外延（invalidValue 亦可辩护），建议
   登记。
8. **GET /Users/{id} 未知 id 语义未钉**：§3.12 Table 8 404 覆盖「resource does not exist」；
   设计未写明未知 user id → 404（cross-tenant group 404 已写，user 侧应在 RED 钉死 404，
   含 PUT/PATCH 未知 id）。
9. **未知属性 400 的 scimType 未指定**：§11 列「未知属性 → 400」但未定 invalidSyntax 还是
   invalidValue（Table 9：invalidSyntax = 不满足 request schema；invalidValue = 值不兼容）。
   RED 应钉死其一（建议 invalidSyntax）。
10. **不声称 SCIM conformance**：发现端点 501 已登记；建议 §13 补一句「S1 不声称 SCIM 2.0
    conformance，仅实现冻结子集」以免后续 conformance 审查误解（上轮 INFO-5 未补）。
11. **Content-Location 对齐**：RFC 7643 §3.1 meta.location「MUST be the same as the
    Content-Location HTTP response header」；设计只规定 POST 的 Location 头。建议资源响应
    （GET/PUT/PATCH/201）同时发 Content-Location = meta.location，或在 §13 登记不发送。

上轮其余 INFO（1/3/4/7/9/10/11/12/13）已在修订中登记或闭合：userName per-issuer 唯一性（§13）、
externalId≡displayName 互操作（§13）、SHOULD-ignore 偏离（§1 冻结原则）、并发败者残留（§4/§11）、
changed 判定（§5）、500 形状（§8/§13）、None-org 读检查（§11 + gate）、/scim/v1 404（§1/§13）、
DECISIONS.md 承接（§13）、行存活断言（§11）。

---

## 验收结论

**通过（0 blocking / 11 INFO）。** 上轮 5 个 blocking 全部闭合且经独立复核成立：issuer 来源按
构造与 T2 登录绑定键一致；读经 gate 复用唯一授权机制且不破坏 T4 mutation 审计冻结面；禁用成员
入组两处统一 400 invalidValue；meta/ListResponse 对齐 RFC 7643 §3.1 / RFC 7644 §3.3/§3.4.2
逐字 REQUIRED；invalidFilter 满足 RFC 7644 §3.4.2.2 MUST。完整重审面（子集矩阵、认证、schema、
白名单 additive、幂等、disable 接线、JIT、fail closed、RED 机制、对抗探针）全部核验成立。
11 项 INFO 均为登记/措辞/机械性修正（其中 1 项为 0009 down_revision 字符串笔误），不改变
方向性设计，不阻塞进入 RED；建议执行方在 GREEN 前把 INFO-1/2/3/4/5 的措辞与登记并入设计
（与 RED 测试内容无关，RED 可先行冻结）。
