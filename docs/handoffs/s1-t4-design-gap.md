# S1-T4 设计缺口报告：结构化 Audit 字段与 RLS 契约解读

> 状态：待设计/验收确认（A 档）。本报告先行提交；白名单扩展在确认后生效。
> 冻结事实源：总设计 §9.2/§9.4、`docs/PERMISSIONS.md` §14、`specs/s1-tenancy-policy.md` §3。

## 1. 最小反例

当前 `AuditEvent` / `AuditEventData` / `build_audit_digest` 只覆盖 8 个语义字段：
`organization_id / workspace_id / action / resource_type / resource_id / actor_ref /
payload_digest / previous_event_digest`。

冻结设计要求审计记录 8 项事实（§9.4、PERMISSIONS §14）：

```
actor/effective identity（分字段）、org/workspace、resource/version、action、
policy decision/revision、result、request/trace id、前序 digest
```

用现有 schema 记录一次「OPA 拒绝的工作区成员写入」时：

```sql
SELECT actor_ref, resource_id, payload_digest FROM audit_events;
```

- 看不出**有效身份**（delegation/代理主体）与 actor 的区分——`actor_ref` 只有一个字符串；
- 看不出 **resource version**——`resource_id` 只到资源，`resource_version` 无处存放；
- 看不出 **OPA decision_id / revision / reason**——授权结论与策略版本完全丢失，
  无法回答「哪一版 bundle 批准了这次 mutation」；
- 看不出 **result / request_id / trace_id**——审计行无法关联请求与追踪。

`payload_digest` 是不可逆摘要，不构成「记录包含这些字段」；
把字段拼进 `actor_ref` 或塞进 digest 是被明确禁止的做法（T4 任务书）。
结论：schema 缺口真实存在，必须扩展。

## 2. 受影响 invariant（及其处置）

| # | 不变量 | 影响与处置 |
| --- | --- | --- |
| I1 | `build_audit_digest(AuditEventData)` 对既有 v1 行产生**逐字节不变**的 digest | digest 函数按 `audit_schema_version` 分派：v1 走原公式（原代码路径不改一行），v2 走扩展公式。已提交的 v1 行继续可验证。 |
| I2 | `verify_audit_chain` 支持单一 (org, workspace) scope 内的完整链 | 同一链内可混 v1/v2 行，逐行按版本验证；scope 跨租户拒绝规则不变。 |
| I3 | `uq_audit_events_scope_previous`（NULLS NOT DISTINCT）+ `uq_audit_events_event_digest` | 新列不影响这两个唯一约束；并发防分叉仍由现有 advisory xact lock + 唯一约束兜底。 |
| I4 | 0001 的 `GRANT SELECT, INSERT ON audit_events TO zhiwei_app` | 表级授权覆盖新列，无需新增授权；RLS policy（optional-workspace 形态）不变。 |
| I5 | 0001~0004 迁移文件只读、digest 契约不静默修改 | 本扩展只**追加** `audit_schema_version` 分派，不改 v1 公式；0001 的 `workspaces` policy 不修改（见 §4）。 |
| I6 | 冻结的 canonical_event audit 路径（`CanonicalUnitOfWork._append_audit_and_outbox`）行为不变 | 继续写 v1 行（audit_schema_version=1）；其 digest/outbox 行为与现有测试完全一致。 |
| I7 | 新 typed audit 记录与 mutation 同事务提交/回滚；denied mutation 独立 fail-closed 事务 | 由 `append_audit`（同事务）与 `append_fail_closed_audit`（独立事务）两条路径分别保证。 |
| I8 | `zhiwei_app` 对 audit_events 的 INSERT 保持可用（deny audit 也需要写 audit） | 现有 INSERT 授权已覆盖；不做收紧。 |

## 3. 候选结构化 schema

### 3.1 `audit_events` 新列（新迁移 `0005_audit_structured`，down_revision=`0004_refresh_fencing`）

```sql
ALTER TABLE audit_events
  ADD COLUMN audit_schema_version  INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN effective_identity_ref TEXT NULL,
  ADD COLUMN resource_version      INTEGER NULL,
  ADD COLUMN decision_id           TEXT NULL,
  ADD COLUMN policy_revision       TEXT NULL,
  ADD COLUMN decision_reason       TEXT NULL,
  ADD COLUMN result                TEXT NULL,
  ADD COLUMN request_id            TEXT NULL,
  ADD COLUMN trace_id              TEXT NULL;

ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_audit_schema_version
  CHECK (audit_schema_version IN (1, 2));

-- v2 行完整性（fail closed）：有效身份 / resource version / result / request / trace 必填；
-- decision_id/revision/reason 允许 NULL——T3 冻结契约：fail-closed 本地拒绝绝不伪造 OPA metadata
ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_v2_complete
  CHECK (audit_schema_version = 1 OR (
    effective_identity_ref IS NOT NULL AND resource_version IS NOT NULL
    AND result IS NOT NULL AND request_id IS NOT NULL AND trace_id IS NOT NULL));
```

`result` 取值约束：`allowed | denied | failed`（CHECK `IN (...)`，v1 行为 NULL 不检查）。

### 3.2 `AuditEventData`（`persistence/events.py`）扩展

```python
class AuditEventData(BaseModel):
    # 既有字段不变；新增均为可选 + audit_schema_version（默认 1）
    audit_schema_version: int = Field(default=1, ge=1)
    effective_identity_ref: str | None = None
    resource_version: int | None = None
    decision_id: str | None = None
    policy_revision: str | None = None
    decision_reason: str | None = None
    result: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
```

- `build_audit_digest`：`audit_schema_version == 1` → 原公式（保持逐字节一致）；`== 2` → 覆盖全部 v2 语义字段；
- `audit_data_from_row`：映射新列；`verify_audit_chain`：逐行版本分派，其余逻辑不变。

### 3.3 typed audit 记录（新模块 `src/zhiwei/identity/audit.py`）

```python
class AuditRecord(BaseModel):        # frozen, extra="forbid"；audit_schema_version 固定 2
    organization_id: UUID
    workspace_id: UUID | None
    action: str                      # 如 "organization.create", "workspace.member.add"
    resource_type: str               # 如 "organization" / "workspace" / "membership"
    resource_id: UUID
    resource_version: int
    actor_ref: str                   # 发起主体（authenticated principal）
    effective_identity_ref: str      # OPA 实际评估的身份（S1 无 delegation 时 = actor_ref；S2/S4 扩展）
    decision_id: str | None          # fail-closed 本地拒绝为 None，绝不伪造
    policy_revision: str | None
    decision_reason: str
    result: Literal["allowed", "denied", "failed"]
    request_id: str
    trace_id: str
    payload_digest: str              # 业务变更的规范化 digest
```

两条路径（不复制 digest/outbox 实现，复用 `build_audit_digest` / `verify_audit_chain` /
`AuditEvent` / `OutboxMessage` / advisory lock 设施）：

- `append_audit(session, context, record)`：在**当前事务内**追加 audit 行 + 同事务 outbox 消息；
- `append_fail_closed_audit(sessions, context, record)`：denied mutation 的**独立事务**（`tenant_session`
  式：begin + SET LOCAL GUC + commit），保证拒绝也必可审计。

`unit_of_work.py` 将审计追加核心提取为共享模块级函数（v1 canonical 路径行为不变，
冻结测试逐字节验证），`identity/audit.py` 与 canonical 路径共用同一实现。

### 3.4 outbox

typed audit 的同事务 outbox 消息 payload = 结构化非敏感字段（action/result/decision_id/
revision/request_id/trace_id/resource），与 `canonical.event.committed` 平级新增
`audit.decision` topic；不放入任何 token/cookie/authorization header/secret（RED 冻结）。

## 4. RLS 契约解读：`workspaces` 表 policy 不改动

T4 RED 契约原文：「workspace table 同时要求 organization_id + workspace_id」。

**最小反例（若按字面实现）**：`list_workspaces` 是平台 org 级操作，事务 context 只有
`zhiwei.organization_id`（`TenantRepository._require_org_level` 禁止 workspace 级 context
执行 org 级操作，`repositories.py:616`）。若 `workspaces` 表 policy 硬性要求 workspace GUC，
org 级列表恒返回 0 行，`create_workspace` 的 INSERT 恒被 WITH CHECK 拒绝 → 平台自身
org 管理流程失效，T1 冻结的 `test_memberships.py` 直接全红。

**既有 policy（0001）已满足契约的可执行语义**，行为已实证（PG 17.6，zhiwei_app）：

| 场景 | 结果 |
| --- | --- |
| 无 GUC | SELECT 0 行；INSERT 报 RLS 错误；UPDATE 0 行 |
| 仅 org GUC | 只看到该 org 的 workspaces（org 列表需要）；其他表：org 级数据可见、workspace 级不可见 |
| org + 匹配 ws GUC | 只看到该 org 下该 workspace |
| org + 不匹配 ws GUC（org/workspace 不一致） | 0 行 |
| identity-global 表（principals/external_identities/auth_sessions 等） | 无 RLS，不套用租户 GUC |

**建议**：保持 0001 `workspaces` policy 不变（org 匹配 + ws 匹配或为空 + 不一致拒绝），
把上述行为矩阵冻结进 RED 测试；不新增迁移修改 0001 语义。请设计/验收方确认此解读。

## 5. 旧 schema / backfill 与 migration chain 影响

- 新迁移 `0005_audit_structured`，`down_revision = 0004_refresh_fencing`；0001~0004 逐字节不动。
- **存量集群升级**：既有 audit 行 `audit_schema_version=1`、新列 NULL（列均可空/DEFAULT 1），
  旧 digest 仍按 v1 公式验证，不重算、不迁移数据。
- **全新初始化集群**：upgrade base→head 正常建立全部新列。
- **downgrade 可逆**：0005 downgrade 先 drop 两个 CHECK，再 drop 新列；`workspaces` 相关不动。
  在专用 `zhiwei_test` 库上验证 upgrade/downgrade 往返（含 0004 既有数据形态）。
- 需要写 v2 行时：`INSERT` 授权（表级）已覆盖；无需改 grants。

## 6. 必须扩展的文件白名单（等待确认）

```text
新增：
  migrations/versions/0005_audit_structured.py
  src/zhiwei/identity/audit.py
  tests/security/tenancy/test_rls.py
  tests/security/tenancy/test_pool.py
  tests/security/tenancy/test_idor.py
  tests/security/tenancy/test_audit.py
  docs/handoffs/s1-t4-design-gap.md
  docs/handoffs/s1-t4.md（交接报告）

修改（仅限本报告 §2/§3 范围内）：
  src/zhiwei/persistence/models.py        # AuditEvent 新列映射
  src/zhiwei/persistence/events.py        # AuditEventData / build_audit_digest / audit_data_from_row / verify_audit_chain
  src/zhiwei/persistence/unit_of_work.py  # 审计追加核心提取为共享函数（v1 行为不变）
  src/zhiwei/persistence/tenant.py        # 既有白名单；仅按需调整（预计不需要改动）

明确不改：
  migrations/versions/0001~0004、tests/ 既有文件（GREEN 阶段锁定）、evals/、
  docs/AGENTS.md 等治理文件、src/zhiwei/policy/（T3 冻结）、src/zhiwei/api/、
  src/zhiwei/identity/commands.py（审计接线留待 T5/T6 API/SCIM 落地时统一接入）。
```

## 7. 其他待确认点

1. `AuditRecord.resource_version` 为 typed 命令必填；canonical_event 路径保持 v1（run 资源的
   version 语义由 `payload_digest=event.event_digest` 表达，不硬造 version）。
2. `result` 枚举 `allowed | denied | failed`；v2 行 CHECK 强制非空。
3. S1 无 delegation，`effective_identity_ref = actor_ref` 是诚实取值（不是缺省绕过）；
   S2/S4 引入 delegation 后从 PolicyInput 的 delegation 链取真实有效身份。
4. org 级（workspace_id NULL）审计链的 advisory lock 键用 organization_id，与 workspace 级
   链（锁键 workspace_id）天然分域，二者无共享链竞争。
