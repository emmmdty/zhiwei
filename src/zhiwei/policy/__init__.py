"""S1-T3 policy 层：RBAC/OPA 授权边界。

角色→权限映射的唯一事实实现是 `policies/zhiwei/authz.rego`；本包只提供严格类型
输入（roles/input）、OPA 传输与有界缓存（client）和 PEP 编排（enforcement）。
"""

from zhiwei.policy.client import OPAClient, PolicyDecision
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.input import (
    Actor,
    Delegation,
    EffectiveIdentity,
    PolicyInput,
    RequestContext,
    ResourceContext,
    ResourceRef,
    RoleBinding,
)
from zhiwei.policy.roles import (
    LEGACY_ROLE_ALIASES,
    ORG_SCOPED_ROLES,
    RESOURCE_ACTIONS,
    WORKSPACE_SCOPED_ROLES,
    Action,
    Classification,
    Purpose,
    ResourceType,
    Risk,
    Role,
    RoleScope,
    normalize_role,
)

__all__ = [
    "LEGACY_ROLE_ALIASES",
    "ORG_SCOPED_ROLES",
    "RESOURCE_ACTIONS",
    "WORKSPACE_SCOPED_ROLES",
    "Action",
    "Actor",
    "Classification",
    "Delegation",
    "EffectiveIdentity",
    "OPAClient",
    "PolicyDecision",
    "PolicyEnforcer",
    "PolicyInput",
    "Purpose",
    "RequestContext",
    "ResourceContext",
    "ResourceRef",
    "ResourceType",
    "Risk",
    "Role",
    "RoleBinding",
    "RoleScope",
    "normalize_role",
]
