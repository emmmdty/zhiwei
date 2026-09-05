# S1-T3 冻结授权契约（A 档，由 design/acceptance 冻结，RED 阶段锁定）。
#
# 本文件是对 docs/PERMISSIONS.md §3.1 冻结矩阵 + §3.2 职责分离 + ABAC 上下文门的
# 独立转写，用于交叉验证 authz.rego 的行为；测试数据与实现分离，实现可任意重写。
# 修改本文件必须先回到 RED 阶段并重新确认预期失败。
#
# 角色/资源/动作命名与 src/zhiwei/policy/roles.py 的枚举一致；Rego 是角色→权限
# 映射的唯一事实实现，Python 层只做输入规范化与边界拒绝。
package zhiwei.authz_test

org_id := "o1"
ws_id := "w1"
ws2_id := "w2"
now := "2026-08-15T00:00:00Z"
later := "2026-08-15T01:00:00Z"

# ---------- 独立转写：冻结 RBAC 矩阵（docs/PERMISSIONS.md §3.1）----------
# [resource, action, role] 三元组：矩阵 cell 语义（不含 ABAC 上下文门）。
# 用三元组而非嵌套对象是为了让 Rego 类型检查接受全量扫表（对象类型不一致会
# 触发 rego_type_error，且三元组更便于独立核对）。
allowed_cells := [
    ["org", "manage", "org_owner"],
    ["org", "delegate", "org_owner"],
    ["org", "config_security", "security_admin"],
    ["org", "manage_workspace_members", "workspace_admin"],
    ["org", "read_self", "member"],
    ["org", "read_audit", "auditor"],
    ["workspace_policy", "configure", "org_owner"],
    ["workspace_policy", "configure_security_egress", "security_admin"],
    ["workspace_policy", "configure_workspace", "workspace_admin"],
    ["workspace_policy", "configure_workspace", "org_owner"],
    ["workspace_policy", "read", "agent_builder"],
    ["workspace_policy", "read", "auditor"],
    ["workspace_policy", "read_memory_policy", "memory_steward"],
    ["workspace_policy", "read_approval_policy", "approver"],
    ["workspace_policy", "read_applicable", "member"],
    ["workspace_policy", "export", "auditor"],
    ["knowledge_source", "delegate", "org_owner"],
    ["knowledge_source", "classify_egress", "security_admin"],
    ["knowledge_source", "suspend", "security_admin"],
    ["knowledge_source", "manage", "workspace_admin"],
    ["knowledge_source", "bind_draft", "agent_builder"],
    ["knowledge_source", "debug", "agent_builder"],
    ["knowledge_source", "read", "memory_steward"],
    ["knowledge_source", "read_published", "member"],
    ["knowledge_source", "read_provenance", "auditor"],
    ["capability_version", "import_check_test", "capability_publisher"],
    ["capability_version", "admit_low_medium", "capability_publisher"],
    ["capability_version", "review_high_critical", "security_admin"],
    ["capability_version", "suspend", "security_admin"],
    ["capability_version", "revoke", "security_admin"],
    ["capability_version", "bind_workspace", "workspace_admin"],
    ["capability_version", "bind_draft", "agent_builder"],
    ["capability_version", "browse", "member"],
    ["capability_version", "read_admission_audit", "auditor"],
    ["connection_secret", "revoke", "security_admin"],
    ["connection_secret", "revoke", "workspace_admin"],
    ["connection_secret", "read_security_metadata", "security_admin"],
    ["connection_secret", "define_credential_requirement", "capability_publisher"],
    ["connection_secret", "create_workspace_connection", "workspace_admin"],
    ["connection_secret", "rotate", "workspace_admin"],
    ["connection_secret", "create_own", "agent_builder"],
    ["connection_secret", "create_own", "member"],
    ["connection_secret", "revoke_own", "agent_builder"],
    ["connection_secret", "revoke_own", "member"],
    ["connection_secret", "read_status_fingerprint", "auditor"],
    ["agent_draft", "reject_suspend_security", "security_admin"],
    ["agent_draft", "read", "workspace_admin"],
    ["agent_draft", "delegate_builder", "workspace_admin"],
    ["agent_draft", "create_edit_run", "agent_builder"],
    ["agent_draft", "read_version_gate", "auditor"],
    ["agent_publish", "request", "agent_builder"],
    ["agent_publish", "review_publish", "workspace_admin"],
    ["agent_publish", "rollback", "workspace_admin"],
    ["agent_publish", "veto_hard_gate", "security_admin"],
    ["agent_publish", "read_manifest", "auditor"],
    ["run_case_artifact", "break_glass_incident", "security_admin"],
    ["run_case_artifact", "manage_lifecycle", "workspace_admin"],
    ["run_case_artifact", "run_sandbox", "agent_builder"],
    ["run_case_artifact", "read", "agent_builder"],
    ["run_case_artifact", "read", "auditor"],
    ["run_case_artifact", "read_case_memory", "memory_steward"],
    ["run_case_artifact", "read_minimal_approval_context", "approver"],
    ["run_case_artifact", "run_published", "member"],
    ["run_case_artifact", "manage_visible_cases", "member"],
    ["run_case_artifact", "export", "auditor"],
    ["team_memory", "quarantine", "security_admin"],
    ["team_memory", "revoke", "security_admin"],
    ["team_memory", "revoke", "memory_steward"],
    ["team_memory", "configure_policy", "workspace_admin"],
    ["team_memory", "submit_candidate", "agent_builder"],
    ["team_memory", "confirm", "memory_steward"],
    ["team_memory", "correct", "memory_steward"],
    ["team_memory", "conflict", "memory_steward"],
    ["team_memory", "submit_own_candidate", "member"],
    ["team_memory", "read_authorized", "member"],
    ["team_memory", "read_provenance", "auditor"],
    ["tool_approval", "emergency_revoke", "security_admin"],
    ["tool_approval", "configure_approver_group", "workspace_admin"],
    ["tool_approval", "request", "agent_builder"],
    ["tool_approval", "request", "member"],
    ["tool_approval", "approve", "approver"],
    ["tool_approval", "reject", "approver"],
    ["tool_approval", "replace", "approver"],
    ["tool_approval", "read_record", "auditor"],
]

all_resources := {res | some cell in allowed_cells; res := cell[0]}
all_actions := {res: {a | some cell in allowed_cells; cell[0] == res; a := cell[1]} | some res in all_resources}
all_roles := {"org_owner", "security_admin", "capability_publisher", "workspace_admin",
    "agent_builder", "memory_steward", "approver", "member", "auditor"}
workspace_roles := {"workspace_admin", "agent_builder"}
org_roles := all_roles - workspace_roles

# 未列入矩阵的动作名与资源名（默认拒绝的样例）
unknown_actions := {"delete", "read_secret", "grant_owner", "export_all", "publish_anywhere"}

# ---------- 输入构造 helper ----------
binding(name, scope) := {
    "name": name,
    "scope": scope,
    "organization_id": org_id,
    "workspace_id": ws_id,
} if scope == "workspace"

binding(name, scope) := {
    "name": name,
    "scope": scope,
    "organization_id": org_id,
    "workspace_id": null,
} if scope == "org"

binding_scope(name) := "workspace" if {
    name in workspace_roles
}

binding_scope(name) := "org" if {
    name in org_roles
}

# 扫表用 resource_context：actor=u1 是所有者、不是任何 SoD party、有独立发布者 u9。
# 使矩阵扫表只测 cell 语义，不被上下文门干扰。
sweep_context(res) := {
    "requester_principal_id": "u9",
    "modifier_principal_ids": [],
    "agent_identity_principal_id": null,
    "owner_principal_id": "u1",
    "last_content_author_principal_id": "u9",
    "publisher_principal_id": "u9",
    "publisher_roles": ["capability_publisher"],
}

sweep_risk(res, act) := "high" if {
    res == "capability_version"
    act == "review_high_critical"
}

sweep_risk(res, act) := "low" if {
    res == "capability_version"
    act != "review_high_critical"
}

sweep_risk(res, act) := null if {
    res != "capability_version"
}

sweep_input(res, act, name, scope) := {
    "organization_id": org_id,
    "workspace_id": ws_id,
    "actor": {"principal_id": "u1", "kind": "user", "roles": [binding(name, scope)]},
    "effective_identity": null,
    "resource": {"type": res, "id": "r1", "version": "v1"},
    "action": act,
    "purpose": "general",
    "classification": null,
    "risk": sweep_risk(res, act),
    "delegation": [],
    "resource_context": sweep_context(res),
    "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
}

eval_allows(res, act, name, scope) if {
    data.zhiwei.authz.allow == true
    with input as sweep_input(res, act, name, scope)
}

eval_denies(res, act, name, scope) if {
    data.zhiwei.authz.allow != true
    with input as sweep_input(res, act, name, scope)
}

# ---------- 矩阵全量扫表 ----------
# 每个 cell 内的角色必须 allow
test_matrix_allow_sweep if {
    some cell in allowed_cells
    eval_allows(cell[0], cell[1], cell[2], binding_scope(cell[2]))
}

# 每个 cell 外的角色必须 deny（并集语义：任一角色命中 cell 即 allow，但未列组合永远 deny）
test_matrix_deny_sweep if {
    some res in all_resources
    some act in all_actions[res]
    some name in all_roles
    not allowed_cell(res, act, name)
    eval_denies(res, act, name, binding_scope(name))
}

allowed_cell(res, act, name) if {
    some cell in allowed_cells
    cell[0] == res
    cell[1] == act
    cell[2] == name
}

# 未列入矩阵的动作：任何角色都 deny
test_unlisted_action_denied if {
    some res in all_resources
    some act in unknown_actions
    some name in all_roles
    eval_denies(res, act, name, binding_scope(name))
}

# 未列入矩阵的资源类型：任何动作任何角色都 deny
test_unlisted_resource_denied if {
    some act in ["manage", "request", "approve", "read"]
    some name in all_roles
    eval_denies("unlisted_resource", act, name, binding_scope(name))
}

# ---------- 角色绑定作用域 ----------
# workspace 角色绑定在 workspace W1 不授权 W2 的动作
test_workspace_scope_isolated_by_workspace if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws2_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("workspace_admin", "workspace")]},
        "effective_identity": null,
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "review_publish",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("agent_publish"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# org 角色必须绑在 org 作用域；绑到 workspace 上不授权 org 动作
test_org_role_bound_at_workspace_scope_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("org_owner", "workspace")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# workspace 角色绑到 org 作用域不授权 workspace 动作
test_workspace_role_bound_at_org_scope_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("agent_builder", "org")]},
        "effective_identity": null,
        "resource": {"type": "agent_draft", "id": "r1", "version": "v1"},
        "action": "create_edit_run",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("agent_draft"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 未知角色名（原始 input 直达 Rego 的纵深防御；Python 边界同样拒绝）
test_unknown_role_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("superuser", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- 多角色并集与 hard deny（PERMISSIONS.md §3.2）----------
# 并集：任一角色命中 cell 即 allow
test_multi_role_union_allows_each_cell if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [
            binding("agent_builder", "workspace"),
            binding("auditor", "org"),
        ]},
        "effective_identity": null,
        "resource": {"type": "run_case_artifact", "id": "r1", "version": "v1"},
        "action": "export",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("run_case_artifact"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# hard deny：Security Admin 永远不作业务批准——即使同时持 Approver 角色
test_security_admin_plus_approver_cannot_approve if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [
            binding("security_admin", "org"),
            binding("approver", "org"),
        ]},
        "effective_identity": null,
        "resource": {"type": "tool_approval", "id": "r1", "version": "v1"},
        "action": "approve",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("tool_approval"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

test_security_admin_alone_cannot_approve if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("security_admin", "org")]},
        "effective_identity": null,
        "resource": {"type": "tool_approval", "id": "r1", "version": "v1"},
        "action": "approve",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("tool_approval"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

test_approver_alone_can_approve if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("approver", "org")]},
        "effective_identity": null,
        "resource": {"type": "tool_approval", "id": "r1", "version": "v1"},
        "action": "approve",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("tool_approval"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- SoD：Agent Builder 不能发布自己最后编辑的版本 ----------
# 发布复核人 == 最后一个内容作者 -> deny
test_publisher_cannot_review_own_last_edit if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("workspace_admin", "workspace")]},
        "effective_identity": null,
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "review_publish",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": "u1",
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 不同主体复核 -> allow
test_review_by_different_principal_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("workspace_admin", "workspace")]},
        "effective_identity": null,
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "review_publish",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": "u9",
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# Agent Builder 可以 request 自己最后编辑的版本（禁止的是发布复核，不是 request）
test_builder_can_request_own_version_publish if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("agent_builder", "workspace")]},
        "effective_identity": null,
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "request",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": "u1",
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# SoD 按主体生效，不按「正在用的帽子」：Builder 兼任 Workspace Admin 也不能
# 发布自己最后编辑的版本（多角色并集不绕过分离约束，PERMISSIONS.md:70）
test_builder_with_workspace_admin_hat_cannot_publish_own_last_edit if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [
            binding("agent_builder", "workspace"),
            binding("workspace_admin", "workspace"),
        ]},
        "effective_identity": null,
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "review_publish",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": "u1",
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 互补正例：同一主体双角色，但最后编辑者是别人 → 允许复核
test_builder_with_workspace_admin_hat_can_review_others_edit if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [
            binding("agent_builder", "workspace"),
            binding("workspace_admin", "workspace"),
        ]},
        "effective_identity": null,
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "review_publish",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": "u9",
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 有效身份（agent 背后的用户）是最后编辑者 -> deny（编辑经 agent 完成也算本人）
test_effective_identity_self_review_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "p-agent-1", "kind": "agent_identity", "roles": [
            {"name": "workspace_admin", "scope": "workspace", "organization_id": org_id, "workspace_id": ws_id},
        ]},
        "effective_identity": {"principal_id": "u1", "kind": "user"},
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "review_publish",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": "u1",
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- SoD：不能批准自己发起/修改 input/代表执行的 ApprovalRequest ----------
# 发起人自批
test_requester_cannot_approve if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("approver", "org")]},
        "effective_identity": null,
        "resource": {"type": "tool_approval", "id": "r1", "version": "v1"},
        "action": "approve",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": "u1",
            "modifier_principal_ids": ["u8"],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 修改过 input 的人不能批准（修改历史记录的是触发修改的有效用户）
test_input_modifier_cannot_approve if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("approver", "org")]},
        "effective_identity": null,
        "resource": {"type": "tool_approval", "id": "r1", "version": "v1"},
        "action": "approve",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": "u1",
            "modifier_principal_ids": ["u8", "u2"],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 代表执行请求的 AgentIdentity 不能批准
test_agent_identity_cannot_approve if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "p-agent-1", "kind": "agent_identity", "roles": [binding("approver", "org")]},
        "effective_identity": null,
        "resource": {"type": "tool_approval", "id": "r1", "version": "v1"},
        "action": "approve",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": "u1",
            "modifier_principal_ids": [],
            "agent_identity_principal_id": "p-agent-1",
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 有效身份（agent 背后的用户）是请求方也不能批准
test_effective_identity_requester_cannot_approve if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "p-agent-1", "kind": "agent_identity", "roles": [binding("approver", "org")]},
        "effective_identity": {"principal_id": "u1", "kind": "user"},
        "resource": {"type": "tool_approval", "id": "r1", "version": "v1"},
        "action": "approve",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": "u1",
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 干净第三方 Approver 可以批准
test_clean_approver_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u3", "kind": "user", "roles": [binding("approver", "org")]},
        "effective_identity": null,
        "resource": {"type": "tool_approval", "id": "r1", "version": "v1"},
        "action": "approve",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": "u1",
            "modifier_principal_ids": ["u8"],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- SoD：high/critical CapabilityVersion 必须 Publisher + Security Admin 双人双控 ----------
# 同一主体同时持有两个角色 -> deny
test_dual_control_same_principal_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("security_admin", "org")]},
        "effective_identity": null,
        "resource": {"type": "capability_version", "id": "r1", "version": "v1"},
        "action": "review_high_critical",
        "purpose": "general",
        "classification": null,
        "risk": "high",
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": "u1",
            "publisher_roles": ["capability_publisher", "security_admin"],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 两个不同主体但发布侧没有 Publisher 角色（双 Security Admin）-> deny
test_dual_control_publisher_missing_role_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("security_admin", "org")]},
        "effective_identity": null,
        "resource": {"type": "capability_version", "id": "r1", "version": "v1"},
        "action": "review_high_critical",
        "purpose": "general",
        "classification": null,
        "risk": "high",
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": "u9",
            "publisher_roles": ["security_admin"],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 缺少发布者证据（原始 input 直达）-> deny
test_dual_control_missing_publisher_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("security_admin", "org")]},
        "effective_identity": null,
        "resource": {"type": "capability_version", "id": "r1", "version": "v1"},
        "action": "review_high_critical",
        "purpose": "general",
        "classification": null,
        "risk": "high",
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 合法双控：不同主体，发布者持 Publisher 角色，复核者持 Security Admin -> allow
test_dual_control_valid_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("security_admin", "org")]},
        "effective_identity": null,
        "resource": {"type": "capability_version", "id": "r1", "version": "v1"},
        "action": "review_high_critical",
        "purpose": "general",
        "classification": null,
        "risk": "high",
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": "u9",
            "publisher_roles": ["capability_publisher"],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 风险门：低中风险准入不能用于 high/critical；二审只对 high/critical 有效
test_admit_high_risk_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u9", "kind": "user", "roles": [binding("capability_publisher", "org")]},
        "effective_identity": null,
        "resource": {"type": "capability_version", "id": "r1", "version": "v1"},
        "action": "admit_low_medium",
        "purpose": "general",
        "classification": null,
        "risk": "high",
        "delegation": [],
        "resource_context": sweep_context("capability_version"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

test_review_on_low_risk_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("security_admin", "org")]},
        "effective_identity": null,
        "resource": {"type": "capability_version", "id": "r1", "version": "v1"},
        "action": "review_high_critical",
        "purpose": "general",
        "classification": null,
        "risk": "low",
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": null,
            "publisher_principal_id": "u9",
            "publisher_roles": ["capability_publisher"],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# capability_version 无风险字段（原始 input 直达）-> deny
test_capability_gate_requires_risk if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u9", "kind": "user", "roles": [binding("capability_publisher", "org")]},
        "effective_identity": null,
        "resource": {"type": "capability_version", "id": "r1", "version": "v1"},
        "action": "admit_low_medium",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("capability_version"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- purpose ----------
# 缺失 purpose（原始 input 直达）-> deny，不默认 general
test_missing_purpose_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 空 purpose -> deny
test_empty_purpose_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- classification ----------
# 资源分级高于工作区/组织 ceiling -> deny
test_classification_above_ceiling_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("auditor", "org")]},
        "effective_identity": null,
        "resource": {"type": "run_case_artifact", "id": "r1", "version": "v1"},
        "action": "export",
        "purpose": "general",
        "classification": "CONFIDENTIAL",
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("run_case_artifact"),
        "context": {"now": now, "classification_ceiling": "PUBLIC", "requires_delegation": false},
    }
}

# 提供 classification 但无 ceiling -> deny（无法证明不越级）
test_classification_without_ceiling_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("auditor", "org")]},
        "effective_identity": null,
        "resource": {"type": "run_case_artifact", "id": "r1", "version": "v1"},
        "action": "export",
        "purpose": "general",
        "classification": "CONFIDENTIAL",
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("run_case_artifact"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 分级不高于 ceiling -> allow
test_classification_within_ceiling_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("auditor", "org")]},
        "effective_identity": null,
        "resource": {"type": "run_case_artifact", "id": "r1", "version": "v1"},
        "action": "export",
        "purpose": "general",
        "classification": "CONFIDENTIAL",
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("run_case_artifact"),
        "context": {"now": now, "classification_ceiling": "CONFIDENTIAL", "requires_delegation": false},
    }
}

# 未知分级值（原始 input 直达）-> deny
test_unknown_classification_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("auditor", "org")]},
        "effective_identity": null,
        "resource": {"type": "run_case_artifact", "id": "r1", "version": "v1"},
        "action": "export",
        "purpose": "general",
        "classification": "TOP_SECRET",
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("run_case_artifact"),
        "context": {"now": now, "classification_ceiling": "RESTRICTED", "requires_delegation": false},
    }
}

# 未知风险值（原始 input 直达）-> deny
test_unknown_risk_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u9", "kind": "user", "roles": [binding("capability_publisher", "org")]},
        "effective_identity": null,
        "resource": {"type": "capability_version", "id": "r1", "version": "v1"},
        "action": "admit_low_medium",
        "purpose": "general",
        "classification": null,
        "risk": "critical2",
        "delegation": [],
        "resource_context": sweep_context("capability_version"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- own 语义：仅本人资源 ----------
test_own_action_non_owner_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("member", "org")]},
        "effective_identity": null,
        "resource": {"type": "connection_secret", "id": "r1", "version": "v1"},
        "action": "revoke_own",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": "u1",
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

test_own_action_owner_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("member", "org")]},
        "effective_identity": null,
        "resource": {"type": "connection_secret", "id": "r1", "version": "v1"},
        "action": "revoke_own",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": "u1",
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# 有效身份也是本人时被 own 门拦截（agent 代执行）
test_own_action_via_effective_identity_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "p-agent-1", "kind": "agent_identity", "roles": [binding("member", "org")]},
        "effective_identity": {"principal_id": "u2", "kind": "user"},
        "resource": {"type": "connection_secret", "id": "r1", "version": "v1"},
        "action": "revoke_own",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": "u1",
            "last_content_author_principal_id": null,
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- delegation ----------
# 要求委托上下文但无链 -> deny
test_delegation_required_but_missing_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 已过期委托 -> deny
test_delegation_expired_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "org.manage", "expires_at": "2026-08-14T00:00:00Z"},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 委托 scope 不覆盖 (resource, action) -> deny
test_delegation_scope_mismatch_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "workspace_policy.configure", "expires_at": later},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 自己给自己开委托 -> deny
test_delegation_self_grant_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u2", "scope": "org.manage", "expires_at": later},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 有效委托（scope 覆盖、未过期、他人授予）-> allow
test_delegation_valid_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "org.manage", "expires_at": later},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# ---------- delegation 链交集（独立验收最小反例）----------
# 链上每一跳都必须精确覆盖当前 (resource, action)：两跳 [org.manage,
# org.delegate] 请求 org.manage 必须 deny——不能用 any/some 一跳代表整条链有效。
test_delegation_chain_partial_scope_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "org.manage", "expires_at": later},
            {"granted_by_principal_id": "u1", "scope": "org.delegate", "expires_at": later},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 互补正例：两跳都精确覆盖 org.manage 才 allow（链交集语义的允许侧）
test_delegation_chain_all_hops_cover_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "org.manage", "expires_at": later},
            {"granted_by_principal_id": "u1", "scope": "org.manage", "expires_at": later},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 链上任意一跳过期 -> 整链 deny
test_delegation_chain_any_hop_expired_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "org.manage", "expires_at": later},
            {"granted_by_principal_id": "u1", "scope": "org.manage", "expires_at": "2026-08-14T00:00:00Z"},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 时间必须按真实时刻比较：expires_at = 01:00+02:00 实际是 08-14T23:00Z，
# 已过期必须 deny（字符串比较会把带正偏移的字符串判成晚于 now）
test_delegation_offset_expiry_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "org.manage", "expires_at": "2026-08-15T01:00:00+02:00"},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 互补正例：带偏移但确实未过期 -> allow（03:00+02:00 = 01:00Z > now）
test_delegation_offset_future_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u2", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "org.manage", "expires_at": "2026-08-15T03:00:00+02:00"},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 有效身份自授：agent 背后的用户是 grantor -> deny（不能只比较 actor principal）
test_delegation_self_grant_via_effective_identity_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "p-agent-1", "kind": "agent_identity",
                  "roles": [binding("org_owner", "org")]},
        "effective_identity": {"principal_id": "u2", "kind": "user"},
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u2", "scope": "org.manage", "expires_at": later},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# 互补正例：agent 的有效身份与 grantor 不同 -> allow
test_delegation_effective_identity_not_grantor_allowed if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "p-agent-1", "kind": "agent_identity",
                  "roles": [binding("org_owner", "org")]},
        "effective_identity": {"principal_id": "u3", "kind": "user"},
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [
            {"granted_by_principal_id": "u9", "scope": "org.manage", "expires_at": later},
        ],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": true},
    }
}

# ---------- 默认拒绝与 reason ----------
# 空 input -> 默认拒绝
test_empty_input_denied if {
    data.zhiwei.authz.allow == false
    with input as {}
}

# deny 决策必须携带非空 reason
test_deny_reason_non_empty if {
    data.zhiwei.authz.reason != ""
    with input as sweep_input("org", "manage", "member", "org")
}

# allow 决策必须携带非空 reason
test_allow_reason_non_empty if {
    data.zhiwei.authz.reason != ""
    with input as sweep_input("org", "manage", "org_owner", "org")
}

# 未列动作的 reason 应说明默认拒绝
test_default_deny_reason_identifiable if {
    data.zhiwei.authz.reason == "default_deny:no_rule_matched"
    with input as sweep_input("org", "delete", "org_owner", "org")
}

# 矩阵外角色（原始 input 直达）拒绝时 reason 非空（未知角色名 -> 默认拒绝路径）
test_unknown_role_deny_reason_non_empty if {
    data.zhiwei.authz.reason != ""
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("superuser", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- 纵深防御：agent 主体必须携带 effective_identity ----------
# PERMISSIONS.md:9-10 双身份记录 + §3.2 多角色不绕过分离约束：agent 执行时背后的
# 有效主体缺失会让 via_effective SoD 规则全部失效（只比 agent 自身），必须拒绝。
test_agent_without_effective_identity_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "p-agent-1", "kind": "agent_identity", "roles": [
            binding("workspace_admin", "workspace"),
        ]},
        "effective_identity": null,
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "review_publish",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": "u9",
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# agent 携带 effective_identity（与最后编辑者不同）仍按 matrix 判发布复核
test_agent_with_effective_identity_can_review if {
    data.zhiwei.authz.allow == true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "p-agent-1", "kind": "agent_identity", "roles": [
            binding("workspace_admin", "workspace"),
        ]},
        "effective_identity": {"principal_id": "u2", "kind": "user"},
        "resource": {"type": "agent_publish", "id": "r1", "version": "v1"},
        "action": "review_publish",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": {
            "requester_principal_id": null,
            "modifier_principal_ids": [],
            "agent_identity_principal_id": null,
            "owner_principal_id": null,
            "last_content_author_principal_id": "u9",
            "publisher_principal_id": null,
            "publisher_roles": [],
        },
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- 纵深防御：未知 purpose 词汇拒绝 ----------
# 边界（input.py）已拒绝未知 purpose；Rego 兜底覆盖绕过边界的原始 input 路径。
test_unknown_purpose_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [binding("org_owner", "org")]},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "everything",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}

# ---------- 三轮修复：org/create bootstrap（USER 首次创建 / 既有目标重放候选）----------
# 规则语义：仅 kind=user + 无角色绑定可进入；且满足其一：
#   - active org 集合为空 → 首次 bootstrap（任意 target 可进入命令层）；
#   - target organization_id 位于 active org 集合 → 既有目标的重放候选
#     （只允许进入 application command；最终精确重放由 owner-bound idempotency
#     key + request digest 校验决定，不复制幂等逻辑到 Rego）。
# 其余主体（service account / agent / 带绑定 / target 不在集合内）一律 deny。
other_org := "o2"
bootstrap_input(kind, roles, active_orgs) := {
    "organization_id": org_id,
    "workspace_id": null,
    "actor": {"principal_id": "u1", "kind": kind, "roles": roles,
              "active_organization_ids": active_orgs},
    "effective_identity": null,
    "resource": {"type": "org", "id": "r-new", "version": "1"},
    "action": "create",
    "purpose": "general",
    "classification": null,
    "risk": null,
    "delegation": [],
    "resource_context": sweep_context("org"),
    "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
}

# 无 active org 的 USER（无任何绑定）→ allow（首次 bootstrap）
test_bootstrap_eligible_user_allowed if {
    data.zhiwei.authz.allow == true
    with input as bootstrap_input("user", [], [])
}

# target 位于 active org 集合的 USER（无绑定）→ allow（既有目标的重放候选）
test_bootstrap_replay_candidate_target_in_active_set_allowed if {
    data.zhiwei.authz.allow == true
    with input as bootstrap_input("user", [], [org_id])
}

# 多 org 集合中 target 恰好在集合内 → allow（确定性集合成员判定，与排序无关）
test_bootstrap_replay_candidate_multi_org_set_allowed if {
    data.zhiwei.authz.allow == true
    with input as bootstrap_input("user", [], [other_org, org_id])
}

# 已有 active org 但 target 不在集合内 → deny（禁止创建新组织）
test_bootstrap_user_with_active_org_new_target_denied if {
    data.zhiwei.authz.allow != true
    with input as bootstrap_input("user", [], [other_org])
}

# 带角色绑定的 USER（有成员身份）→ deny
test_bootstrap_user_with_bindings_denied if {
    data.zhiwei.authz.allow != true
    with input as bootstrap_input("user", [binding("member", "org")], [])
}

# service account → deny
test_bootstrap_service_account_denied if {
    data.zhiwei.authz.allow != true
    with input as bootstrap_input("service_account", [], [])
}

# agent identity（即使有有效主体）→ deny
test_bootstrap_agent_identity_denied if {
    data.zhiwei.authz.allow != true
    with input as bootstrap_input("agent_identity", [], [])
}

# org/manage 不因 bootstrap 规则放宽：无角色主体管理已有组织仍 deny
test_org_manage_roleless_denied if {
    data.zhiwei.authz.allow != true
    with input as {
        "organization_id": org_id,
        "workspace_id": ws_id,
        "actor": {"principal_id": "u1", "kind": "user", "roles": [],
                  "active_organization_ids": []},
        "effective_identity": null,
        "resource": {"type": "org", "id": "r1", "version": "v1"},
        "action": "manage",
        "purpose": "general",
        "classification": null,
        "risk": null,
        "delegation": [],
        "resource_context": sweep_context("org"),
        "context": {"now": now, "classification_ceiling": null, "requires_delegation": false},
    }
}
