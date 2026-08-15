"""S1-T3 授权边界词汇：冻结枚举与 schema 描述。

本模块只提供严格类型和协议边界（角色/资源/动作/分级/风险/purpose 词汇、
资源→动作 schema、角色作用域分类、历史别名），**不包含任何角色→权限映射**：
授权语义的唯一事实实现是 `policies/zhiwei/authz.rego`（docs/PERMISSIONS.md §3.1
冻结矩阵）。在这里复制权限判断属于第二套实现，是禁止的。

命名对齐 docs/PERMISSIONS.md §3.1 的冻结矩阵与总设计 §9.1。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class Role(StrEnum):
    """冻结角色集（docs/PERMISSIONS.md §3.1）。"""

    ORG_OWNER = "org_owner"
    SECURITY_ADMIN = "security_admin"
    CAPABILITY_PUBLISHER = "capability_publisher"
    WORKSPACE_ADMIN = "workspace_admin"
    AGENT_BUILDER = "agent_builder"
    MEMORY_STEWARD = "memory_steward"
    APPROVER = "approver"
    MEMBER = "member"
    AUDITOR = "auditor"


class RoleScope(StrEnum):
    """角色绑定作用域：矩阵标注的 org/workspace 二选一，own 属于动作语义。"""

    ORG = "org"
    WORKSPACE = "workspace"


# 矩阵里只有 Workspace Admin 与 Agent Builder 是 workspace 作用域角色；
# 其余角色绑在 org 作用域（member 两种作用域都不参与本分层，见 Rego 的绑定检查）。
WORKSPACE_SCOPED_ROLES: frozenset[Role] = frozenset(
    {Role.WORKSPACE_ADMIN, Role.AGENT_BUILDER}
)
ORG_SCOPED_ROLES: frozenset[Role] = frozenset(set(Role) - WORKSPACE_SCOPED_ROLES)


class ResourceType(StrEnum):
    """矩阵行（资源）。列出的类型是 S1 授权边界全部资源；未列资源默认拒绝。"""

    ORG = "org"
    WORKSPACE_POLICY = "workspace_policy"
    KNOWLEDGE_SOURCE = "knowledge_source"
    CAPABILITY_VERSION = "capability_version"
    CONNECTION_SECRET = "connection_secret"
    AGENT_DRAFT = "agent_draft"
    AGENT_PUBLISH = "agent_publish"
    RUN_CASE_ARTIFACT = "run_case_artifact"
    TEAM_MEMORY = "team_memory"
    TOOL_APPROVAL = "tool_approval"


class Action(StrEnum):
    """矩阵动作词汇（行内自然语言的机读分解）。与 Rego 侧一致；未知动作边界拒绝。"""

    # org（Org、IdP、SCIM、角色绑定）
    MANAGE = "manage"
    DELEGATE = "delegate"
    CONFIG_SECURITY = "config_security"
    MANAGE_WORKSPACE_MEMBERS = "manage_workspace_members"
    READ_SELF = "read_self"
    READ_AUDIT = "read_audit"
    # workspace_policy
    CONFIGURE = "configure"
    CONFIGURE_SECURITY_EGRESS = "configure_security_egress"
    CONFIGURE_WORKSPACE = "configure_workspace"
    READ = "read"
    READ_MEMORY_POLICY = "read_memory_policy"
    READ_APPROVAL_POLICY = "read_approval_policy"
    READ_APPLICABLE = "read_applicable"
    EXPORT = "export"
    # knowledge_source（workspace_admin 的 创建/同步/授权/禁用 复用 Action.MANAGE）
    CLASSIFY_EGRESS = "classify_egress"
    SUSPEND = "suspend"
    BIND_DRAFT = "bind_draft"
    DEBUG = "debug"
    READ_PUBLISHED = "read_published"
    READ_PROVENANCE = "read_provenance"
    # capability_version
    IMPORT_CHECK_TEST = "import_check_test"
    ADMIT_LOW_MEDIUM = "admit_low_medium"
    REVIEW_HIGH_CRITICAL = "review_high_critical"
    REVOKE = "revoke"
    BIND_WORKSPACE = "bind_workspace"
    BROWSE = "browse"
    READ_ADMISSION_AUDIT = "read_admission_audit"
    # connection_secret
    READ_SECURITY_METADATA = "read_security_metadata"
    DEFINE_CREDENTIAL_REQUIREMENT = "define_credential_requirement"
    CREATE_WORKSPACE_CONNECTION = "create_workspace_connection"
    ROTATE = "rotate"
    CREATE_OWN = "create_own"
    REVOKE_OWN = "revoke_own"
    READ_STATUS_FINGERPRINT = "read_status_fingerprint"
    # agent_draft
    REJECT_SUSPEND_SECURITY = "reject_suspend_security"
    DELEGATE_BUILDER = "delegate_builder"
    CREATE_EDIT_RUN = "create_edit_run"
    READ_VERSION_GATE = "read_version_gate"
    # agent_publish
    REQUEST = "request"
    REVIEW_PUBLISH = "review_publish"
    ROLLBACK = "rollback"
    VETO_HARD_GATE = "veto_hard_gate"
    READ_MANIFEST = "read_manifest"
    # run_case_artifact
    BREAK_GLASS_INCIDENT = "break_glass_incident"
    MANAGE_LIFECYCLE = "manage_lifecycle"
    RUN_SANDBOX = "run_sandbox"
    READ_CASE_MEMORY = "read_case_memory"
    READ_MINIMAL_APPROVAL_CONTEXT = "read_minimal_approval_context"
    RUN_PUBLISHED = "run_published"
    MANAGE_VISIBLE_CASES = "manage_visible_cases"
    # team_memory
    QUARANTINE = "quarantine"
    CONFIGURE_POLICY = "configure_policy"
    SUBMIT_CANDIDATE = "submit_candidate"
    CONFIRM = "confirm"
    CORRECT = "correct"
    CONFLICT = "conflict"
    SUBMIT_OWN_CANDIDATE = "submit_own_candidate"
    READ_AUTHORIZED = "read_authorized"
    # tool_approval
    EMERGENCY_REVOKE = "emergency_revoke"
    CONFIGURE_APPROVER_GROUP = "configure_approver_group"
    APPROVE = "approve"
    REJECT = "reject"
    REPLACE = "replace"
    READ_RECORD = "read_record"


# (resource, action) schema：每个资源的合法动作集（矩阵行转写）。
# 这只是 schema 校验（哪些动作存在），不含角色；角色映射只在 Rego。
RESOURCE_ACTIONS: Mapping[ResourceType, frozenset[Action]] = {
    ResourceType.ORG: frozenset({
        Action.MANAGE, Action.DELEGATE, Action.CONFIG_SECURITY,
        Action.MANAGE_WORKSPACE_MEMBERS, Action.READ_SELF, Action.READ_AUDIT,
    }),
    ResourceType.WORKSPACE_POLICY: frozenset({
        Action.CONFIGURE, Action.CONFIGURE_SECURITY_EGRESS,
        Action.CONFIGURE_WORKSPACE, Action.READ, Action.READ_MEMORY_POLICY,
        Action.READ_APPROVAL_POLICY, Action.READ_APPLICABLE, Action.EXPORT,
    }),
    ResourceType.KNOWLEDGE_SOURCE: frozenset({
        Action.DELEGATE, Action.CLASSIFY_EGRESS, Action.SUSPEND, Action.MANAGE,
        Action.BIND_DRAFT, Action.DEBUG, Action.READ, Action.READ_PUBLISHED,
        Action.READ_PROVENANCE,
    }),
    ResourceType.CAPABILITY_VERSION: frozenset({
        Action.IMPORT_CHECK_TEST, Action.ADMIT_LOW_MEDIUM,
        Action.REVIEW_HIGH_CRITICAL, Action.SUSPEND, Action.REVOKE,
        Action.BIND_WORKSPACE, Action.BIND_DRAFT, Action.BROWSE,
        Action.READ_ADMISSION_AUDIT,
    }),
    ResourceType.CONNECTION_SECRET: frozenset({
        Action.REVOKE, Action.READ_SECURITY_METADATA,
        Action.DEFINE_CREDENTIAL_REQUIREMENT, Action.CREATE_WORKSPACE_CONNECTION,
        Action.ROTATE, Action.CREATE_OWN, Action.REVOKE_OWN,
        Action.READ_STATUS_FINGERPRINT,
    }),
    ResourceType.AGENT_DRAFT: frozenset({
        Action.REJECT_SUSPEND_SECURITY, Action.READ, Action.DELEGATE_BUILDER,
        Action.CREATE_EDIT_RUN, Action.READ_VERSION_GATE,
    }),
    ResourceType.AGENT_PUBLISH: frozenset({
        Action.REQUEST, Action.REVIEW_PUBLISH, Action.ROLLBACK,
        Action.VETO_HARD_GATE, Action.READ_MANIFEST,
    }),
    ResourceType.RUN_CASE_ARTIFACT: frozenset({
        Action.BREAK_GLASS_INCIDENT, Action.MANAGE_LIFECYCLE,
        Action.RUN_SANDBOX, Action.READ, Action.READ_CASE_MEMORY,
        Action.READ_MINIMAL_APPROVAL_CONTEXT, Action.RUN_PUBLISHED,
        Action.MANAGE_VISIBLE_CASES, Action.EXPORT,
    }),
    ResourceType.TEAM_MEMORY: frozenset({
        Action.QUARANTINE, Action.REVOKE, Action.CONFIGURE_POLICY,
        Action.SUBMIT_CANDIDATE, Action.CONFIRM, Action.CORRECT,
        Action.CONFLICT, Action.SUBMIT_OWN_CANDIDATE, Action.READ_AUTHORIZED,
        Action.READ_PROVENANCE,
    }),
    ResourceType.TOOL_APPROVAL: frozenset({
        Action.EMERGENCY_REVOKE, Action.CONFIGURE_APPROVER_GROUP,
        Action.REQUEST, Action.APPROVE, Action.REJECT, Action.REPLACE,
        Action.READ_RECORD,  # Auditor 列「只读记录」
    }),
}


class Classification(StrEnum):
    """数据分级阶梯（docs/PERMISSIONS.md §7）。"""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class Risk(StrEnum):
    """capability/effect 风险等级（矩阵低中/高/关键）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Purpose(StrEnum):
    """S1 授权边界的 purpose 词汇（缺失/未知一律边界拒绝，绝不默认 general）。"""

    GENERAL = "general"
    COMPLIANCE = "compliance"
    SECURITY = "security"
    AUDIT = "audit"


# T1/T2 时代 memberships 里写入的历史角色字符串（矩阵命名前的自由字符串）。
# 别名表是唯一受认可的历史映射；除此之外任何字符串都按未知角色拒绝（fail closed）。
# docs/PERMISSIONS.md §3 明确「产品中的 Builder 就是 Agent Builder」。
LEGACY_ROLE_ALIASES: Mapping[str, Role] = {
    "owner": Role.ORG_OWNER,
    "builder": Role.AGENT_BUILDER,
}


def normalize_role(value: str) -> Role:
    """把成员绑定字符串规范化为冻结角色；未知值抛 ValueError（边界拒绝）。"""
    if value in LEGACY_ROLE_ALIASES:
        return LEGACY_ROLE_ALIASES[value]
    try:
        return Role(value)
    except ValueError as exc:
        raise ValueError(f"unknown role: {value!r}") from exc
