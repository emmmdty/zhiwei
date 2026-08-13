"""S1 identity：Principal / ExternalIdentity / Membership / Group 的 domain、commands 与 repositories。"""

from __future__ import annotations

from zhiwei.identity.commands import (
    ExternalIdentityConflictError,
    IdentityCommandError,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    add_group_member,
    add_org_membership,
    add_workspace_membership,
    create_group,
    create_user,
    disable_principal,
    remove_org_membership,
)
from zhiwei.identity.domain import (
    ActorContext,
    ExternalIdentity,
    Group,
    GroupMember,
    Membership,
    Principal,
    PrincipalKind,
    PrincipalStatus,
    WorkspaceMembership,
)
from zhiwei.identity.repositories import IdentityRepository

__all__ = [
    "ActorContext",
    "ExternalIdentity",
    "ExternalIdentityConflictError",
    "Group",
    "GroupMember",
    "IdentityCommandError",
    "IdentityRepository",
    "Membership",
    "Principal",
    "PrincipalDisabledError",
    "PrincipalKind",
    "PrincipalNotFoundError",
    "PrincipalStatus",
    "WorkspaceMembership",
    "add_group_member",
    "add_org_membership",
    "add_workspace_membership",
    "create_group",
    "create_user",
    "disable_principal",
    "remove_org_membership",
]
