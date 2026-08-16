"""Identity application commands。

- 所有 mutation 接入 S0 idempotency 基础（claim_idempotency），不另造第二套机制；
- 重复 Idempotency-Key + 相同 request digest 返回原结果；不同 digest 抛 IdempotencyConflict；
- 命令只依赖 IdentityRepositoryProtocol，不绑定数据库实现；
- Organization bootstrap 原子创建 Organization + 创建者 Owner Membership。
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.canonical import digest as canonical_digest
from zhiwei.identity.domain import (
    BootstrapClaimConflict,
    ExternalIdentity,
    ExternalIdentityConflictError,
    Group,
    GroupMember,
    IdentityCommandError,
    Membership,
    NameConflictError,
    Organization,
    OrganizationExistsError,
    Principal,
    PrincipalDisabledError,
    PrincipalKind,
    PrincipalNotFoundError,
    PrincipalStatus,
    ResourceConflictError,
    Workspace,
    WorkspaceMembership,
)
from zhiwei.persistence.repositories import IdempotencyConflict, IdempotencyLookup

IDEMPOTENCY_SCOPE_ORGANIZATION_CREATE = "organization.create"
IDEMPOTENCY_SCOPE_WORKSPACE_CREATE = "organization.workspace.create"
IDEMPOTENCY_SCOPE_MEMBER_ADD = "organization.member.add"
IDEMPOTENCY_SCOPE_MEMBER_REMOVE = "organization.member.remove"
IDEMPOTENCY_SCOPE_GROUP_CREATE = "workspace.group.create"

__all__ = [
    "BootstrapClaimConflict",
    "CommandOutcome",
    "ExternalIdentityConflictError",
    "IdempotencyRequest",
    "IdempotencyResult",
    "IdentityCommandError",
    "NameConflictError",
    "OrganizationExistsError",
    "PrincipalDisabledError",
    "PrincipalNotFoundError",
    "ResourceConflictError",
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
    """规范化请求 digest：method + path + body，复用项目 RFC 8785/JCS + NFC canonical 实现。

    键序无关（JCS 排序）且 NFC/NFD 等价文本 digest 相同；method/path/body 真正不同时
    digest 必然不同。
    """
    return canonical_digest({"method": method.upper(), "path": path, "body": body})


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
    ) -> tuple[bool, Organization]: ...

    async def claim_organization_bootstrap(
        self, principal_id: UUID, organization_id: UUID
    ) -> bool: ...

    async def get_organization(self, organization_id: UUID) -> Organization | None: ...

    async def create_workspace(
        self, workspace_id: UUID, *, organization_id: UUID, name: str
    ) -> tuple[bool, Workspace]: ...

    async def list_workspaces(self, *, organization_id: UUID) -> list[Workspace]: ...

    async def lookup_idempotency(
        self, *, scope: str, key: str
    ) -> IdempotencyLookup | None: ...

    async def add_membership(
        self,
        *,
        principal_id: UUID,
        organization_id: UUID,
        role_bindings: frozenset[str],
    ) -> tuple[bool, Membership]: ...

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
    ) -> tuple[bool, Group]: ...

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


def _organization_scope(owner_principal_id: UUID) -> str:
    """bootstrap 幂等域绑定 owner：精确重放必须由创建者本人发起。

    S0 idempotency 键空间为 (org, workspace, scope, key)；把 owner 编入 scope 后，
    他人重放创建者请求会命中不同的幂等域 → 只读查询无记录 → 拒绝（租户接管防护）。
    """
    return f"{IDEMPOTENCY_SCOPE_ORGANIZATION_CREATE}:{owner_principal_id}"


async def _existing_organization_outcome(
    repository: IdentityRepositoryProtocol,
    *,
    organization_id: UUID,
    owner_principal_id: UUID,
    idempotency: IdempotencyRequest | None,
    response: dict[str, Any],
) -> CommandOutcome:
    """既有组织的 bootstrap 路径：只读幂等查询 + owner 匹配，绝不写 membership/claim。

    无幂等键、无匹配记录（猜测 victim UUID 的接管尝试）、或请求不是创建者本人的
    精确重放——一律 OrganizationExistsError（API 映射 403）。
    """
    if idempotency is None:
        raise OrganizationExistsError("organization already exists")
    lookup = await repository.lookup_idempotency(
        scope=_organization_scope(owner_principal_id), key=idempotency.key
    )
    if lookup is None:
        raise OrganizationExistsError("organization already exists")
    if lookup.request_digest != idempotency.request_digest:
        raise IdempotencyConflict("idempotency key was already used for another request")
    return CommandOutcome(created=False, response=lookup.response)


async def create_organization(
    repository: IdentityRepositoryProtocol,
    *,
    organization_id: UUID,
    owner_principal_id: UUID,
    idempotency: IdempotencyRequest | None = None,
) -> CommandOutcome:
    """Organization bootstrap：原子创建 Organization、bootstrap claim 与 Owner Membership。

    INSERT ... RETURNING 原子区分「本次创建」与「组织已存在」：组织已存在时绝不先写
    Owner membership（租户接管防护），只走只读幂等重放路径；仅本次确实创建新 org 时
    调用持久 bootstrap claim（S1-T4 四轮：一个 principal 最多 claim 一个 bootstrap
    org，membership 删除不重置资格）。claim=false（同一 principal 已 claim 不同
    target）抛 BootstrapClaimConflict，使刚插入的 organization 整体回滚——API 层映射
    403 且不写 failed 审计（loser target 无合法 audit FK scope，pre-tenant 例外）。
    """
    response = {"id": str(organization_id), "status": "active"}
    created, _ = await repository.create_organization(organization_id, status="active")
    if not created:
        return await _existing_organization_outcome(
            repository,
            organization_id=organization_id,
            owner_principal_id=owner_principal_id,
            idempotency=idempotency,
            response=response,
        )
    claimed = await repository.claim_organization_bootstrap(
        owner_principal_id, organization_id
    )
    if not claimed:
        raise BootstrapClaimConflict(
            "principal has already bootstrapped another organization"
        )
    await repository.add_membership(
        principal_id=owner_principal_id,
        organization_id=organization_id,
        role_bindings=frozenset({"owner"}),
    )
    if idempotency is not None:
        result = await repository.claim_idempotency(
            scope=_organization_scope(owner_principal_id),
            key=idempotency.key,
            request_digest=idempotency.request_digest,
            response=response,
        )
        if not result.created:
            return CommandOutcome(created=False, response=result.response)
    return CommandOutcome(created=True, response=response)


async def _existing_resource_outcome(
    repository: IdentityRepositoryProtocol,
    *,
    scope: str,
    idempotency: IdempotencyRequest | None,
    response: dict[str, Any],
) -> CommandOutcome:
    """既有资源（id 已存在）路径：只读幂等查询，绝不写新幂等记录。

    新幂等键 → ResourceConflictError（409）；记录存在但 digest 不同 → IdempotencyConflict
    （409）；记录存在且 digest 一致 → 原始重放（200，返回首次响应）。
    """
    if idempotency is None:
        raise ResourceConflictError("resource already exists")
    lookup = await repository.lookup_idempotency(scope=scope, key=idempotency.key)
    if lookup is None:
        raise ResourceConflictError("resource already exists")
    if lookup.request_digest != idempotency.request_digest:
        raise IdempotencyConflict("idempotency key was already used for another request")
    return CommandOutcome(created=False, response=lookup.response)


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
    created, _ = await repository.create_workspace(
        workspace_id, organization_id=organization_id, name=name
    )
    if not created:
        return await _existing_resource_outcome(
            repository,
            scope=IDEMPOTENCY_SCOPE_WORKSPACE_CREATE,
            idempotency=idempotency,
            response=response,
        )
    if idempotency is not None:
        result = await repository.claim_idempotency(
            scope=IDEMPOTENCY_SCOPE_WORKSPACE_CREATE,
            key=idempotency.key,
            request_digest=idempotency.request_digest,
            response=response,
        )
        if not result.created:
            return CommandOutcome(created=False, response=result.response)
    return CommandOutcome(created=True, response=response)


async def add_org_membership(
    repository: IdentityRepositoryProtocol,
    *,
    principal_id: UUID,
    organization_id: UUID,
    role_bindings: frozenset[str] = frozenset(),
    idempotency: IdempotencyRequest | None = None,
) -> CommandOutcome:
    """添加 Organization membership；旧请求重放绝不再写 membership（ABA 防护）。

    mutation 前先做只读幂等判断：同 digest 返回原结果、异 digest 抛冲突，两者都不触发
    INSERT/DELETE；无记录才继续 mutation，成功后 claim。
    """
    if idempotency is not None:
        lookup = await repository.lookup_idempotency(
            scope=IDEMPOTENCY_SCOPE_MEMBER_ADD, key=idempotency.key
        )
        if lookup is not None:
            if lookup.request_digest != idempotency.request_digest:
                raise IdempotencyConflict(
                    "idempotency key was already used for another request"
                )
            return CommandOutcome(created=False, response=lookup.response)
    await _require_active(repository, principal_id)
    response = {
        "principal_id": str(principal_id),
        "organization_id": str(organization_id),
        "role_bindings": sorted(role_bindings),
    }
    created, _ = await repository.add_membership(
        principal_id=principal_id,
        organization_id=organization_id,
        role_bindings=role_bindings,
    )
    if not created:
        return await _existing_resource_outcome(
            repository,
            scope=IDEMPOTENCY_SCOPE_MEMBER_ADD,
            idempotency=idempotency,
            response=response,
        )
    if idempotency is not None:
        result = await repository.claim_idempotency(
            scope=IDEMPOTENCY_SCOPE_MEMBER_ADD,
            key=idempotency.key,
            request_digest=idempotency.request_digest,
            response=response,
        )
        if not result.created:
            return CommandOutcome(created=False, response=result.response)
    return CommandOutcome(created=True, response=response)


async def remove_org_membership(
    repository: IdentityRepositoryProtocol,
    *,
    principal_id: UUID,
    organization_id: UUID,
    idempotency: IdempotencyRequest | None = None,
) -> CommandOutcome:
    """移除 Organization membership；disabled principal 仍可被移除（清理语义）。

    旧 DELETE 重放绝不再删 membership（ABA 防护）：请求完成后重新添加的 membership
    必须保留，重放只返回原结果。
    """
    if idempotency is not None:
        lookup = await repository.lookup_idempotency(
            scope=IDEMPOTENCY_SCOPE_MEMBER_REMOVE, key=idempotency.key
        )
        if lookup is not None:
            if lookup.request_digest != idempotency.request_digest:
                raise IdempotencyConflict(
                    "idempotency key was already used for another request"
                )
            return CommandOutcome(created=False, response=lookup.response)
    response = {
        "principal_id": str(principal_id),
        "organization_id": str(organization_id),
    }
    await repository.remove_membership(principal_id=principal_id, organization_id=organization_id)
    if idempotency is not None:
        result = await repository.claim_idempotency(
            scope=IDEMPOTENCY_SCOPE_MEMBER_REMOVE,
            key=idempotency.key,
            request_digest=idempotency.request_digest,
            response=response,
        )
        if not result.created:
            return CommandOutcome(created=False, response=result.response)
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
    created, _ = await repository.create_group(
        group_id, organization_id=organization_id, workspace_id=workspace_id, name=name
    )
    if not created:
        return await _existing_resource_outcome(
            repository,
            scope=IDEMPOTENCY_SCOPE_GROUP_CREATE,
            idempotency=idempotency,
            response=response,
        )
    if idempotency is not None:
        result = await repository.claim_idempotency(
            scope=IDEMPOTENCY_SCOPE_GROUP_CREATE,
            key=idempotency.key,
            request_digest=idempotency.request_digest,
            response=response,
        )
        if not result.created:
            return CommandOutcome(created=False, response=result.response)
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
