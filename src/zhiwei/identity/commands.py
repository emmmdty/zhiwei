"""Identity commands：principal / membership / group 变更，fail-closed 前置检查。

命令只依赖 IdentityRepositoryProtocol，不绑定数据库实现。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID, uuid4

from zhiwei.identity.domain import (
    ExternalIdentity,
    ExternalIdentityConflictError,
    Group,
    GroupMember,
    IdentityCommandError,
    Membership,
    Principal,
    PrincipalDisabledError,
    PrincipalKind,
    PrincipalNotFoundError,
    PrincipalStatus,
    WorkspaceMembership,
)

__all__ = [
    "ExternalIdentityConflictError",
    "IdentityCommandError",
    "PrincipalDisabledError",
    "PrincipalNotFoundError",
    "add_group_member",
    "add_org_membership",
    "add_workspace_membership",
    "create_group",
    "create_user",
    "disable_principal",
    "remove_org_membership",
]


class IdentityRepositoryProtocol(Protocol):
    """命令层依赖的最小 repository 接口（内存 fake 与 PostgreSQL 实现共用）。"""

    async def get_principal(self, principal_id: UUID) -> Principal | None: ...

    async def create_principal(
        self,
        principal_id: UUID,
        *,
        kind: PrincipalKind,
        status: PrincipalStatus = PrincipalStatus.ACTIVE,
    ) -> Principal: ...

    async def disable_principal(self, principal_id: UUID) -> Principal | None: ...

    async def get_external_identity(
        self, *, issuer: str, subject: str
    ) -> ExternalIdentity | None: ...

    async def bind_external_identity(
        self, *, issuer: str, subject: str, principal_id: UUID
    ) -> ExternalIdentity: ...

    async def add_membership(
        self,
        *,
        principal_id: UUID,
        organization_id: UUID,
        role_bindings: frozenset[str],
    ) -> Membership: ...

    async def remove_membership(
        self, *, principal_id: UUID, organization_id: UUID
    ) -> bool: ...

    async def add_workspace_membership(
        self,
        *,
        principal_id: UUID,
        organization_id: UUID,
        workspace_id: UUID,
        role_bindings: frozenset[str],
    ) -> WorkspaceMembership: ...

    async def create_group(
        self, group_id: UUID, *, organization_id: UUID, name: str
    ) -> Group: ...

    async def add_group_member(
        self, *, group_id: UUID, organization_id: UUID, principal_id: UUID
    ) -> GroupMember: ...


async def create_user(
    repository: IdentityRepositoryProtocol, *, issuer: str, subject: str
) -> Principal:
    """创建 User 主体并绑定 OIDC 外部身份；(issuer, subject) 已被占用时冲突。"""
    if await repository.get_external_identity(issuer=issuer, subject=subject) is not None:
        raise ExternalIdentityConflictError(
            "external identity is already bound to another principal"
        )
    principal = await repository.create_principal(
        uuid4(), kind=PrincipalKind.USER, status=PrincipalStatus.ACTIVE
    )
    await repository.bind_external_identity(
        issuer=issuer, subject=subject, principal_id=principal.id
    )
    return principal


async def disable_principal(
    repository: IdentityRepositoryProtocol, principal_id: UUID
) -> Principal:
    """禁用 principal；SCIM/OIDC 层的拒绝语义在 T2/T5 接入。"""
    if await repository.get_principal(principal_id) is None:
        raise PrincipalNotFoundError("principal not found")
    disabled = await repository.disable_principal(principal_id)
    # get 与 disable 之间的删除窗口只可能是并发移除；fail closed，不返回 None
    if disabled is None:
        raise PrincipalNotFoundError("principal not found")
    return disabled


async def _require_active(
    repository: IdentityRepositoryProtocol, principal_id: UUID
) -> Principal:
    principal = await repository.get_principal(principal_id)
    if principal is None:
        raise PrincipalNotFoundError("principal not found")
    if principal.is_disabled:
        raise PrincipalDisabledError("disabled principals cannot gain new memberships")
    return principal


async def add_org_membership(
    repository: IdentityRepositoryProtocol,
    *,
    principal_id: UUID,
    organization_id: UUID,
    role_bindings: frozenset[str] = frozenset(),
) -> Membership:
    await _require_active(repository, principal_id)
    return await repository.add_membership(
        principal_id=principal_id,
        organization_id=organization_id,
        role_bindings=role_bindings,
    )


async def remove_org_membership(
    repository: IdentityRepositoryProtocol, *, principal_id: UUID, organization_id: UUID
) -> bool:
    """移除 Organization membership；disabled principal 仍可被移除（清理语义）。"""
    return await repository.remove_membership(
        principal_id=principal_id, organization_id=organization_id
    )


async def add_workspace_membership(
    repository: IdentityRepositoryProtocol,
    *,
    principal_id: UUID,
    organization_id: UUID,
    workspace_id: UUID,
    role_bindings: frozenset[str] = frozenset(),
) -> WorkspaceMembership:
    await _require_active(repository, principal_id)
    return await repository.add_workspace_membership(
        principal_id=principal_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        role_bindings=role_bindings,
    )


async def create_group(
    repository: IdentityRepositoryProtocol,
    *,
    group_id: UUID | None = None,
    organization_id: UUID,
    name: str,
) -> Group:
    return await repository.create_group(
        group_id or uuid4(), organization_id=organization_id, name=name
    )


async def add_group_member(
    repository: IdentityRepositoryProtocol,
    *,
    group_id: UUID,
    organization_id: UUID,
    principal_id: UUID,
) -> GroupMember:
    """加入分组；重复执行幂等（返回既有成员行）。"""
    await _require_active(repository, principal_id)
    return await repository.add_group_member(
        group_id=group_id,
        organization_id=organization_id,
        principal_id=principal_id,
    )
