"""Identity application commands。

- 所有 mutation 接入 S0 idempotency 基础（claim_idempotency），不另造第二套机制；
- 重复 Idempotency-Key + 相同 request digest 返回原结果；不同 digest 抛 IdempotencyConflict；
- 命令只依赖 IdentityRepositoryProtocol，不绑定数据库实现；
- Organization bootstrap 原子创建 Organization + 创建者 Owner Membership。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.identity.domain import (
    ExternalIdentity,
    ExternalIdentityConflictError,
    Group,
    GroupMember,
    IdentityCommandError,
    Membership,
    NameConflictError,
    Organization,
    Principal,
    PrincipalDisabledError,
    PrincipalKind,
    PrincipalNotFoundError,
    PrincipalStatus,
    Workspace,
    WorkspaceMembership,
)

IDEMPOTENCY_SCOPE_ORGANIZATION_CREATE = "organization.create"
IDEMPOTENCY_SCOPE_WORKSPACE_CREATE = "organization.workspace.create"
IDEMPOTENCY_SCOPE_MEMBER_ADD = "organization.member.add"
IDEMPOTENCY_SCOPE_MEMBER_REMOVE = "organization.member.remove"
IDEMPOTENCY_SCOPE_GROUP_CREATE = "workspace.group.create"

__all__ = [
    "CommandOutcome",
    "ExternalIdentityConflictError",
    "IdempotencyRequest",
    "IdempotencyResult",
    "IdentityCommandError",
    "NameConflictError",
    "PrincipalDisabledError",
    "PrincipalNotFoundError",
    "add_group_member",
    "add_org_membership",
    "add_workspace_membership",
    "canonical_request_digest",
    "create_group",
    "create_organization",
    "create_user",
    "create_workspace",
    "disable_principal",
    "remove_org_membership",
]


class IdempotencyRequest(BaseModel):
    """API 层的幂等键与规范化请求 digest。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    request_digest: str


class IdempotencyResult(BaseModel):
    """claim_idempotency 结果；created=False 表示命中既有记录（重放）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created: bool
    response: dict[str, Any]


class CommandOutcome(BaseModel):
    """mutation 命令结果：created=True 为首次执行，False 为幂等重放；response 两路径一致。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created: bool
    response: dict[str, Any]


def canonical_request_digest(method: str, path: str, body: dict[str, Any]) -> str:
    """规范化请求 digest：method + path + body，重复 key 的不同请求必然产生不同 digest。"""
    canonical = json.dumps(
        {"method": method.upper(), "path": path, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

    async def claim_idempotency(
        self,
        *,
        scope: str,
        key: str,
        request_digest: str,
        response: dict[str, Any],
    ) -> IdempotencyResult: ...

    async def create_organization(
        self, organization_id: UUID, *, status: str
    ) -> Organization: ...

    async def get_organization(self, organization_id: UUID) -> Organization | None: ...

    async def create_workspace(
        self, workspace_id: UUID, *, organization_id: UUID, name: str
    ) -> Workspace: ...

    async def list_workspaces(self, *, organization_id: UUID) -> list[Workspace]: ...

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
        self,
        group_id: UUID,
        *,
        organization_id: UUID,
        workspace_id: UUID,
        name: str,
    ) -> Group: ...

    async def add_group_member(
        self,
        *,
        group_id: UUID,
        organization_id: UUID,
        workspace_id: UUID,
        principal_id: UUID,
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


async def _replay_or_none(
    repository: IdentityRepositoryProtocol,
    *,
    scope: str,
    idempotency: IdempotencyRequest | None,
    response: dict[str, Any],
) -> CommandOutcome | None:
    """幂等声明（资源 insert 之后调用，bootstrap 的 claim 依赖 org 行已存在）。

    重放时返回既有结果；无幂等键或首次声明返回 None 表示继续返回 created 结果。
    """
    if idempotency is None:
        return None
    result = await repository.claim_idempotency(
        scope=scope,
        key=idempotency.key,
        request_digest=idempotency.request_digest,
        response=response,
    )
    if result.created:
        return None
    return CommandOutcome(created=False, response=result.response)


async def create_organization(
    repository: IdentityRepositoryProtocol,
    *,
    organization_id: UUID,
    owner_principal_id: UUID,
    idempotency: IdempotencyRequest | None = None,
) -> CommandOutcome:
    """Organization bootstrap：原子创建 Organization 与创建者的 Owner Membership。"""
    response = {"id": str(organization_id), "status": "active"}
    await repository.create_organization(organization_id, status="active")
    await repository.add_membership(
        principal_id=owner_principal_id,
        organization_id=organization_id,
        role_bindings=frozenset({"owner"}),
    )
    replayed = await _replay_or_none(
        repository,
        scope=IDEMPOTENCY_SCOPE_ORGANIZATION_CREATE,
        idempotency=idempotency,
        response=response,
    )
    if replayed is not None:
        return replayed
    return CommandOutcome(created=True, response=response)


async def create_workspace(
    repository: IdentityRepositoryProtocol,
    *,
    organization_id: UUID,
    workspace_id: UUID,
    name: str,
    idempotency: IdempotencyRequest | None = None,
) -> CommandOutcome:
    response = {
        "id": str(workspace_id),
        "organization_id": str(organization_id),
        "name": name,
    }
    await repository.create_workspace(workspace_id, organization_id=organization_id, name=name)
    replayed = await _replay_or_none(
        repository,
        scope=IDEMPOTENCY_SCOPE_WORKSPACE_CREATE,
        idempotency=idempotency,
        response=response,
    )
    if replayed is not None:
        return replayed
    return CommandOutcome(created=True, response=response)


async def add_org_membership(
    repository: IdentityRepositoryProtocol,
    *,
    principal_id: UUID,
    organization_id: UUID,
    role_bindings: frozenset[str] = frozenset(),
    idempotency: IdempotencyRequest | None = None,
) -> CommandOutcome:
    await _require_active(repository, principal_id)
    response = {
        "principal_id": str(principal_id),
        "organization_id": str(organization_id),
        "role_bindings": sorted(role_bindings),
    }
    await repository.add_membership(
        principal_id=principal_id,
        organization_id=organization_id,
        role_bindings=role_bindings,
    )
    replayed = await _replay_or_none(
        repository,
        scope=IDEMPOTENCY_SCOPE_MEMBER_ADD,
        idempotency=idempotency,
        response=response,
    )
    if replayed is not None:
        return replayed
    return CommandOutcome(created=True, response=response)


async def remove_org_membership(
    repository: IdentityRepositoryProtocol,
    *,
    principal_id: UUID,
    organization_id: UUID,
    idempotency: IdempotencyRequest | None = None,
) -> CommandOutcome:
    """移除 Organization membership；disabled principal 仍可被移除（清理语义）。"""
    response = {
        "principal_id": str(principal_id),
        "organization_id": str(organization_id),
    }
    await repository.remove_membership(principal_id=principal_id, organization_id=organization_id)
    replayed = await _replay_or_none(
        repository,
        scope=IDEMPOTENCY_SCOPE_MEMBER_REMOVE,
        idempotency=idempotency,
        response=response,
    )
    if replayed is not None:
        return replayed
    return CommandOutcome(created=True, response=response)


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
    group_id: UUID,
    organization_id: UUID,
    workspace_id: UUID,
    name: str,
    idempotency: IdempotencyRequest | None = None,
) -> CommandOutcome:
    """Workspace 内创建 Group；名称唯一范围是 Workspace。"""
    response = {
        "id": str(group_id),
        "organization_id": str(organization_id),
        "workspace_id": str(workspace_id),
        "name": name,
    }
    await repository.create_group(
        group_id, organization_id=organization_id, workspace_id=workspace_id, name=name
    )
    replayed = await _replay_or_none(
        repository,
        scope=IDEMPOTENCY_SCOPE_GROUP_CREATE,
        idempotency=idempotency,
        response=response,
    )
    if replayed is not None:
        return replayed
    return CommandOutcome(created=True, response=response)


async def add_group_member(
    repository: IdentityRepositoryProtocol,
    *,
    group_id: UUID,
    organization_id: UUID,
    workspace_id: UUID,
    principal_id: UUID,
) -> GroupMember:
    """加入分组；重复执行幂等（返回既有成员行）。"""
    await _require_active(repository, principal_id)
    return await repository.add_group_member(
        group_id=group_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
    )


__all__.append("IdentityRepositoryProtocol")
