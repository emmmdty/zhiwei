# S1 - Tenancy, Identity and Policy

> Status: frozen implementation specification  
> Revised: 2026-09-03（S0–S2 复审增补，见 ADR-012：workspace 创建动作语义、读路径授权、
> refresh 接线、真实栈测试边界、Gate 例外与 deselect 规则）  
> Revised: 2026-09-04（见 ADR-013：identity 真实 OPA slow 测试纳入 Gate——spec 必需场景
> 不得只存在于默认 deselect marker 之后，ADR-012 §5）  
> Depends on: S0  
> Unlocks: S2

## 1. Goal

交付真实 Organization/Workspace 多用户纵切：OIDC BFF、Membership/Group/ServiceAccount、RBAC + OPA、
PostgreSQL RLS、audit 和最小 Web shell。目标不是页面齐全，而是任何后续 Run/Data/Tool 都已有统一主体和
失败关闭授权边界。

## 2. Required modules

```text
src/zhiwei/identity/{domain,commands,oidc,sessions,scim,repositories,audit}.py
src/zhiwei/secrets/{base,local}.py
src/zhiwei/policy/{roles,input,client,enforcement}.py
src/zhiwei/api/{auth,organizations,workspaces,memberships}.py
apps/web/src/features/{auth,organizations,workspaces,members}/
deploy/compose/{keycloak,opa}/
```

## 3. Contracts

- Principal：User/ServiceAccount/AgentIdentity；ExternalIdentity key 为 `(issuer, subject)`。
- Organization/Workspace/Group/Membership/WorkspaceMembership immutable audit semantics。
- OIDC Authorization Code + PKCE、state/nonce、server-side session、Secure/HttpOnly/SameSite cookie。
- SCIM create/update/disable/group reconciliation；JIT 是否允许由 Org policy 决定。
- 基础角色、resource/action/scope 矩阵与职责分离按 `docs/PERMISSIONS.md`；Agent Builder 是独立角色。OPA
  input 包含 actor/effective identity、resource/version、purpose、
  classification、risk、workspace、delegation、request context。
- SecretBackend port 和 local AES-GCM envelope store 在本阶段实现。AuthSession 为 principal/session 级，只
  保存 encrypted access/refresh token + key/version/expiry metadata，AAD 不含 org；active org 登录后按
  Membership 选择。master key 来自 Docker secret，S4 复用 port 扩展 per-org credentials。
- PEP helper 默认 deny 并返回 decision id/bundle revision/reason；所有 mutation 同事务写 AuditEvent。
- **workspace 创建与成员管理的可达性**（ADR-012）：workspace 创建走 `workspace_policy.configure`
  （org 作用域，org_owner）——不得使用要求 workspace 上下文的动作（创建时目标 workspace 尚不存在，
  否则矩阵恒 deny）。必须提供 workspace membership 管理 API（含 workspace_admin 角色绑定的产生
  路径），否则 workspace 生命周期不可运营。
- **读路径授权**（ADR-012）：membership/角色绑定列表仅 org_owner、security_admin（workspace_admin
  限本 workspace）；workspace 列表为 org 目录语义（active 成员可见）；SCIM Users/Groups 为 org
  作用域，跨 org principal 一律 404；runs/agents 等资源读走 PEP read cell，不允许仅依赖
  RLS+membership 判定可见性。
- **session refresh 必须接线为生产路径**（滑动续期或显式 refresh 端点）：refresh fencing 的域实现
  与测试不构成生命周期承诺；未接线前不得宣称 refresh/revoke 契约已交付（ADR-012 反例 6）。
- 所有 tenant table 启用 FORCE RLS；`SET LOCAL` transaction context；pool checkout/checkin reset assertion。

## 4. Web journey

local-product Keycloak 登录后，Org Owner 创建 Organization/Workspace，邀请 Member/Builder/Approver/Auditor，
建立 Group 并分配 workspace role。各角色只能看到/执行相应资源；Auditor 能查看脱敏 AuditEvent，不能编辑。

前端禁止假权限：导航可隐藏，但直接 API 仍由 server PEP/RLS 拒绝。401/403/404（防枚举）语义固定。

## 5. Required tests

- OIDC state/nonce/PKCE、session fixation、CSRF、logout、refresh/revoke、disabled user。
- **真实栈边界**（ADR-012）：OIDC 登录 happy path 至少一条真实 Keycloak 集成测试；授权矩阵每个
  mutation happy path 至少一条真实 OPA bundle 集成测试（FakeOPA 恒 allow 不足以验证矩阵语义）。
  环境阻塞时按 Gate 例外登记，不得以 Fake 测试替代宣称通过。
- 读路径 IDOR：membership/角色绑定列表越权（member 读取全量绑定被拒）、SCIM 跨 org principal
  404、workspace 目录可见性边界。
- session token DB/API/log/trace 无明文，server restart 后可解密，master-key rotation/re-encryption、IdP revoke/
  expiry/refresh race 和多 replica 同 session CAS。
- SCIM create/update/disable、重复 external identity、group reconciliation idempotency。
- API IDOR：逐资源跨 org/workspace；list/count/cursor 不泄露。
- RLS：app role/owner separation、missing tenant context、pool leakage、raw SQL escape。
- OPA：role/attribute交集、bundle update、unavailable/stale decision、policy change during request。
- audit outbox：allow/deny/mutation、actor/effective identity、digest chain。
- Playwright：Owner、Builder、Approver、Member、Auditor 五角色 journey。

## 6. Gate

```bash
uv run pytest tests/unit/identity tests/unit/policy -q
uv run pytest tests/integration/identity tests/integration/rls tests/security/tenancy -q
uv run pytest tests/integration/identity -q -m slow   # 真实 OPA SCIM 授权纵切（ADR-012 §5）
uv run pytest tests/integration/policy -q -m slow   # 真实 OPA：bundle update/policy change 必须执行（ADR-012）
docker compose -f deploy/compose/compose.test.yaml --profile identity config --quiet
npm --prefix apps/web run test:e2e -- tenancy.spec.ts
```

Gate artifact 记录 Keycloak/OPA/PG image digest、policy bundle digest、角色 journey 与全部越权 case。
按 ADR-012：deselect 清单必须逐项说明；环境阻塞项（如 Keycloak e2e）以显式例外条目登记，阶段状态
为「有条件收口」，不得表述为全绿。

## 7. Explicit non-goals

不在本阶段执行 Agent/Tool；ServiceAccount 只完成生命周期和授权契约。不得写“生产级安全”，只能写固定
版本的 identity/tenant/security suite 已通过。
