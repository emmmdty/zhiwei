# S1-T3 授权规则唯一事实实现。
#
# 事实源：docs/PERMISSIONS.md §3.1 冻结矩阵（角色×资源×动作）、§3.2 职责分离、
# §4 PEP 失败关闭、总设计 §9.1/§9.2。Python 层（src/zhiwei/policy/）只做严格类型
# 输入与边界拒绝，不复制这里的任何权限判断。
#
# 语义：
#   allow = 矩阵 cell 命中（角色并集） AND 无 hard deny AND 无 SoD deny
#           AND 无 ABAC 上下文 deny AND 无 delegation deny
#   默认拒绝：未列 resource/action/role 组合一律 deny。
# 输入 schema 见 src/zhiwei/policy/input.py（未知枚举由边界拒绝，这里再兜底）。
package zhiwei.authz

default allow := false
default reason := "default_deny:no_rule_matched"

# ---------- 冻结 RBAC 矩阵（docs/PERMISSIONS.md §3.1）----------
# resource -> action -> 允许的角色集合；矩阵 cell 是角色并集的超集，hard deny/
# SoD/上下文门在 cell 之上收窄。
matrix := {
    "org": {
        "manage": {"org_owner"},
        "delegate": {"org_owner"},
        "config_security": {"security_admin"},
        "manage_workspace_members": {"workspace_admin"},
        "read_self": {"member"},
        "read_audit": {"auditor"},
    },
    "workspace_policy": {
        "configure": {"org_owner"},
        "configure_security_egress": {"security_admin"},
        "configure_workspace": {"workspace_admin"},
        "read": {"agent_builder", "auditor"},
        "read_memory_policy": {"memory_steward"},
        "read_approval_policy": {"approver"},
        "read_applicable": {"member"},
        "export": {"auditor"},
    },
    "knowledge_source": {
        "delegate": {"org_owner"},
        "classify_egress": {"security_admin"},
        "suspend": {"security_admin"},
        "manage": {"workspace_admin"},
        "bind_draft": {"agent_builder"},
        "debug": {"agent_builder"},
        "read": {"memory_steward"},
        "read_published": {"member"},
        "read_provenance": {"auditor"},
    },
    "capability_version": {
        "import_check_test": {"capability_publisher"},
        "admit_low_medium": {"capability_publisher"},
        "review_high_critical": {"security_admin"},
        "suspend": {"security_admin"},
        "revoke": {"security_admin"},
        "bind_workspace": {"workspace_admin"},
        "bind_draft": {"agent_builder"},
        "browse": {"member"},
        "read_admission_audit": {"auditor"},
    },
    "connection_secret": {
        "revoke": {"security_admin", "workspace_admin"},
        "read_security_metadata": {"security_admin"},
        "define_credential_requirement": {"capability_publisher"},
        "create_workspace_connection": {"workspace_admin"},
        "rotate": {"workspace_admin"},
        "create_own": {"agent_builder", "member"},
        "revoke_own": {"agent_builder", "member"},
        "read_status_fingerprint": {"auditor"},
    },
    "agent_draft": {
        "reject_suspend_security": {"security_admin"},
        "read": {"workspace_admin"},
        "delegate_builder": {"workspace_admin"},
        "create_edit_run": {"agent_builder"},
        "read_version_gate": {"auditor"},
    },
    "agent_publish": {
        "request": {"agent_builder"},
        "review_publish": {"workspace_admin"},
        "rollback": {"workspace_admin"},
        "veto_hard_gate": {"security_admin"},
        "read_manifest": {"auditor"},
    },
    "run_case_artifact": {
        "break_glass_incident": {"security_admin"},
        "manage_lifecycle": {"workspace_admin"},
        "run_sandbox": {"agent_builder"},
        "read": {"agent_builder", "auditor"},
        "read_case_memory": {"memory_steward"},
        "read_minimal_approval_context": {"approver"},
        "run_published": {"member"},
        "manage_visible_cases": {"member"},
        "export": {"auditor"},
    },
    "team_memory": {
        "quarantine": {"security_admin"},
        "revoke": {"security_admin", "memory_steward"},
        "configure_policy": {"workspace_admin"},
        "submit_candidate": {"agent_builder"},
        "confirm": {"memory_steward"},
        "correct": {"memory_steward"},
        "conflict": {"memory_steward"},
        "submit_own_candidate": {"member"},
        "read_authorized": {"member"},
        "read_provenance": {"auditor"},
    },
    "tool_approval": {
        "emergency_revoke": {"security_admin"},
        "configure_approver_group": {"workspace_admin"},
        "request": {"agent_builder", "member"},
        "approve": {"approver"},
        "reject": {"approver"},
        "replace": {"approver"},
        "read_record": {"auditor"},
    },
}

# 角色绑定作用域（矩阵的 org/workspace 标注）：workspace 角色必须绑在匹配的
# workspace 上，org 角色绑在 org 上；跨作用域/跨 workspace 的绑定不产生权限。
org_scoped_roles := {"org_owner", "security_admin", "capability_publisher",
    "memory_steward", "approver", "member", "auditor"}
workspace_scoped_roles := {"workspace_admin", "agent_builder"}

actor_has_role(role) if {
    some binding in input.actor.roles
    binding.name == role
    binding.organization_id == input.organization_id
    binding.scope == "org"
    role in org_scoped_roles
}

actor_has_role(role) if {
    some binding in input.actor.roles
    binding.name == role
    binding.organization_id == input.organization_id
    binding.workspace_id == input.workspace_id
    binding.scope == "workspace"
    role in workspace_scoped_roles
    input.workspace_id != null
}

# 矩阵 cell 命中：任一角色命中即算（并集），未列组合因查找失败而 deny
matrix_cell_allowed if {
    some role in matrix[input.resource.type][input.action]
    actor_has_role(role)
}

# ---------- hard deny（§3.2：并集也不能翻转；hard deny 永远优先）----------
# Security Admin 对 Tool Approval 的矩阵 cell 是 hard deny：即使同时持 Approver
# 角色也不能做业务批准（只保留 emergency_revoke）
hard_deny contains "security_admin_business_approval" if {
    input.resource.type == "tool_approval"
    input.action in {"approve", "reject", "replace"}
    actor_has_role("security_admin")
}

# ---------- SoD：AgentVersion 发布复核（§3.2 / PERMISSIONS.md:65）----------
# 复核人必须不同于最后一个内容作者；Owner/Workspace Admin 参与编辑同样不能自审。
# last_content_author 由 PEP 记录为触发编辑的有效主体（MC-7：不是执行 agent 的 id）。
sod_deny contains "self_review_last_content_author" if {
    input.resource.type == "agent_publish"
    input.action == "review_publish"
    input.resource_context.last_content_author_principal_id == input.actor.principal_id
}

sod_deny contains "self_review_last_content_author_via_effective" if {
    input.resource.type == "agent_publish"
    input.action == "review_publish"
    input.effective_identity != null
    input.resource_context.last_content_author_principal_id == input.effective_identity.principal_id
}

# ---------- 身份上下文：agent 必须携带 effective_identity ----------
# PERMISSIONS.md:9-10 双身份记录：agent 执行缺失有效主体会让下面所有
# via_effective SoD 规则失效（只比 agent 自身），Rego 兜底拒绝（边界同样拒绝）。
context_deny contains "agent_without_effective_identity" if {
    input.actor.kind == "agent_identity"
    input.effective_identity == null
}

# ---------- SoD：ApprovalRequest（§3.2 / PERMISSIONS.md:66）----------
# 发起人、代表其运行的 AgentIdentity、修改过 input 的人都不能批准同一请求；
# 当事人集合由 PEP 从权威记录解析（modifier 记录为触发修改的有效主体）。
approval_parties contains input.resource_context.requester_principal_id

approval_parties contains input.resource_context.agent_identity_principal_id if {
    input.resource_context.agent_identity_principal_id != null
}

approval_parties contains m if {
    some m in input.resource_context.modifier_principal_ids
}

sod_deny contains "approval_by_party" if {
    input.resource.type == "tool_approval"
    input.action in {"approve", "reject", "replace"}
    input.actor.principal_id in approval_parties
}

sod_deny contains "approval_by_party_via_effective" if {
    input.resource.type == "tool_approval"
    input.action in {"approve", "reject", "replace"}
    input.effective_identity != null
    input.effective_identity.principal_id in approval_parties
}

# ---------- SoD：high/critical CapabilityVersion 双人双控（§3.2 / PERMISSIONS.md:67）----------
# 需要 Capability Publisher + Security Admin 两个**不同主体**；角色并集、双 Security
# Admin、发布者缺 Publisher 角色都不算双控。publisher_roles 由 PEP 从权威 memberships
# 解析（本 input 只携带证据，不再查库）。
sod_deny contains "capability_dual_control_required" if {
    input.resource.type == "capability_version"
    input.action == "review_high_critical"
    not capability_dual_control_satisfied
}

capability_dual_control_satisfied if {
    input.resource_context.publisher_principal_id != null
    input.resource_context.publisher_principal_id != input.actor.principal_id
    input.effective_identity == null
    "capability_publisher" in input.resource_context.publisher_roles
}

capability_dual_control_satisfied if {
    input.resource_context.publisher_principal_id != null
    input.resource_context.publisher_principal_id != input.actor.principal_id
    input.effective_identity != null
    input.effective_identity.principal_id != input.resource_context.publisher_principal_id
    "capability_publisher" in input.resource_context.publisher_roles
}

# ---------- ABAC 上下文门：purpose / classification / risk / own ----------
# purpose 是硬性输入：缺失/空/未定义一律拒绝，绝不默认成 general
# （Rego 里只有 false/undefined 是假值，"" 是真值，必须显式判空）
context_deny contains "purpose_missing" if {
    not input.purpose
}

context_deny contains "purpose_missing" if {
    input.purpose == ""
}

# 未知 purpose 词汇（边界拒绝之外的 Rego 兜底，与 unknown_classification/risk 对齐）
context_deny contains "unknown_purpose" if {
    input.purpose != null
    not (input.purpose in {"general", "compliance", "security", "audit"})
}

classification_ladder := {"PUBLIC": 1, "INTERNAL": 2, "CONFIDENTIAL": 3, "RESTRICTED": 4}

# 提供 classification 时必须提供 ceiling（无法证明不越级 → 拒绝）
context_deny contains "classification_ceiling_missing" if {
    input.classification != null
    input.context.classification_ceiling == null
}

# 资源分级不得高于 workspace/org 的 ceiling
context_deny contains "classification_above_ceiling" if {
    input.classification != null
    input.context.classification_ceiling != null
    classification_ladder[input.classification] > classification_ladder[input.context.classification_ceiling]
}

# 未知分级/风险值（原始 input 直达 Rego 的纵深防御；边界同样拒绝）
context_deny contains "unknown_classification" if {
    input.classification != null
    not classification_ladder[input.classification]
}

context_deny contains "unknown_risk" if {
    input.risk != null
    not (input.risk in {"low", "medium", "high", "critical"})
}

# 风险门：低中风险准入不能用于 high/critical；二审只对 high/critical 有效
context_deny contains "admit_high_risk_without_dual_control" if {
    input.resource.type == "capability_version"
    input.action == "admit_low_medium"
    input.risk in {"high", "critical"}
}

context_deny contains "review_on_low_medium_risk" if {
    input.resource.type == "capability_version"
    input.action == "review_high_critical"
    input.risk in {"low", "medium"}
}

context_deny contains "capability_gate_requires_risk" if {
    input.resource.type == "capability_version"
    input.action in {"admit_low_medium", "review_high_critical"}
    input.risk == null
}

# own 语义：仅本人资源（矩阵 own 标注的 cell）
own_actions := {
    {"type": "org", "action": "read_self"},
    {"type": "connection_secret", "action": "create_own"},
    {"type": "connection_secret", "action": "revoke_own"},
    {"type": "team_memory", "action": "submit_own_candidate"},
}

context_deny contains "not_owner" if {
    own_actions[{"type": input.resource.type, "action": input.action}]
    input.resource_context.owner_principal_id != input.actor.principal_id
}

context_deny contains "not_owner_via_effective" if {
    own_actions[{"type": input.resource.type, "action": input.action}]
    input.effective_identity != null
    input.resource_context.owner_principal_id != input.effective_identity.principal_id
}

# ---------- delegation（交集公式的 delegation budget/scope 维）----------
# 委托只收窄不扩权：链上**每一跳**都必须精确覆盖 (resource.action)（any/some
# 一跳命中会放行部分覆盖链，属 fail-open——独立验收最小反例）、未过期、非自授；
# 要求委托上下文（context.requires_delegation，PEP 从执行方式推导）而无链 → 拒绝。
# 链上环检测/终止界属于 ADR-008（S2 运行时），不在本层。
delegation_deny contains "delegation_required_missing" if {
    input.context.requires_delegation == true
    count(input.delegation) == 0
}

# 过期必须按真实时刻比较：expires_at 带 UTC 偏移时（如 01:00+02:00 实为前一天
# 23:00Z）字符串比较会判成未过期，故统一解析到纳秒刻度再比较。
delegation_deny contains "delegation_expired" if {
    some d in input.delegation
    time.parse_rfc3339_ns(d.expires_at) <= time.parse_rfc3339_ns(input.context.now)
}

# 链交集：任一跳未覆盖即拒绝。覆盖要求 scope 精确且 expires_at 可解析、严格未
# 过期；expires_at 缺失/不可解析使该跳不覆盖 → not delegation_hop_covers 为真 →
# 拒绝，对畸形原始输入 fail closed。
delegation_deny contains "delegation_scope_mismatch" if {
    count(input.delegation) > 0
    some d in input.delegation
    not delegation_hop_covers(d)
}

delegation_deny contains "delegation_self_grant" if {
    some d in input.delegation
    d.granted_by_principal_id == input.actor.principal_id
}

# 自授同样以有效身份为准：agent 背后的人类与 grantor 相同也是自授（只比 actor
# principal 会让 agent 借他人委托链自证授权）。
delegation_deny contains "delegation_self_grant" if {
    some d in input.delegation
    input.effective_identity != null
    d.granted_by_principal_id == input.effective_identity.principal_id
}

# 正向覆盖判定：scope 精确等于 resource.action 且 expires_at 严格晚于 now。写成
# 正向是为了让不可解析的 expires_at 令该跳**不覆盖**（not 对 undefined 为真 → 触发
# mismatch deny），而不是因规则未定义而放行。
delegation_hop_covers(d) if {
    d.scope == concat(".", [input.resource.type, input.action])
    time.parse_rfc3339_ns(d.expires_at) > time.parse_rfc3339_ns(input.context.now)
}

# ---------- bootstrap：USER 首次创建 org / 既有目标重放候选（S1-T4 三轮修复）----------
# 独立 org/create 动作，不向 org.manage 开放（矩阵不变，org_owner 专属）。
# 仅 kind=user + 无任何角色绑定可进入，且满足其一：
#   - active org 集合为空 → 首次 bootstrap（任意 target 可进入命令层）；
#   - target organization_id ∈ active org 集合 → 既有目标的重放候选。
# 候选 2 只允许进入 application command——精确重放（同 owner 幂等 scope + 同
# request digest）由命令层判定，不复制幂等逻辑到 Rego。service account / agent
# identity / 带绑定 / target 不在集合内的主体一律拒绝。
# active_organization_ids 由 PEP 从权威 memberships 解析，非 caller 自述。
# 集合字段缺失的原始 input（如 org/manage 直接请求）不得引发求值错误：
# count(undefined) 本身是 undefined → 两个候选都未定义 → deny（fail closed）；
# 候选 2 在 count>0 之后才做成员判定，绝不让 `x in undefined` 进入求值器。
bootstrap_org_create if {
    input.resource.type == "org"
    input.action == "create"
    input.actor.kind == "user"
    count(input.actor.roles) == 0
    count(input.actor.active_organization_ids) == 0
}

bootstrap_org_create if {
    input.resource.type == "org"
    input.action == "create"
    input.actor.kind == "user"
    count(input.actor.roles) == 0
    count(input.actor.active_organization_ids) > 0
    input.organization_id in input.actor.active_organization_ids
}

allow if {
    bootstrap_org_create
    count(hard_deny) == 0
    count(sod_deny) == 0
    count(context_deny) == 0
    count(delegation_deny) == 0
}

# ---------- 决策与 reason ----------
allow if {
    matrix_cell_allowed
    count(hard_deny) == 0
    count(sod_deny) == 0
    count(context_deny) == 0
    count(delegation_deny) == 0
}

allowed_via_roles contains role if {
    some role in matrix[input.resource.type][input.action]
    actor_has_role(role)
}

deny_details := {d |
    some k in hard_deny
    d := concat("", ["hard_deny:", k])
} | {d |
    some k in sod_deny
    d := concat("", ["sod_deny:", k])
} | {d |
    some k in context_deny
    d := concat("", ["context_deny:", k])
} | {d |
    some k in delegation_deny
    d := concat("", ["delegation_deny:", k])
}

reason := "allow:org_create_bootstrap" if {
    allow
    bootstrap_org_create
}

reason := concat("", ["allow:", concat(",", sort([r | some r in allowed_via_roles]))]) if {
    allow
    not bootstrap_org_create
}

reason := concat("", ["deny:", concat(";", sort(deny_details))]) if {
    not allow
    count(deny_details) > 0
}
