"""S1-T5 SCIM 应用服务：User/Group 必需子集与 membership 生命周期。

冻结契约（docs/handoffs/s1-t5-design.md §1-§10）：
- User create/update/disable；外部稳定键 (issuer, subject)（PERMISSIONS §1），
  issuer = 部署期固定（ZHIWEI_OIDC_ISSUER，组合时注入），externalId ≡ subject；
  disable 只改 principals.status，不删除历史 actor 引用（external_identities /
  memberships / audit 行永不删除）；
- Group create + member reconciliation：externalId ≡ displayName ≡ group name
  （workspace scope），PUT replace 双向 diff（add 缺失 + remove 多余），幂等零
  副作用；
- **本层不实现授权**：policy gate（api.policy_gate.authorize_mutation）由 api 层
  在调用本服务之前完成（policy 先于事务，S1-T5 设计 §6）；本层只做业务数据访问
  与域转换；
- identity-global 写入走 IdentityStore（identity 引擎 + identity 事务），tenant
  写入走 IdentityRepository（app 引擎 + tenant_session 事务）——沿用 T2/T1 分层，
  不建第二套数据访问路径。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.identity.commands import (
    add_group_member,
    create_group,
)
from zhiwei.identity.domain import (
    ExternalIdentityConflictError,
    PrincipalKind,
    PrincipalNotFoundError,
    PrincipalStatus,
)
from zhiwei.identity.repositories import IdentityRepository, IdentityStore
from zhiwei.persistence.tenant import TenantContext, tenant_session

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScimMeta(_Frozen):
    resourceType: str
    created: datetime
    lastModified: datetime
    location: str


class ScimUserResource(_Frozen):
    schemas: list[str]
    id: UUID
    externalId: str
    userName: str
    active: bool
    meta: ScimMeta


class ScimMember(_Frozen):
    value: UUID


class ScimGroupResource(_Frozen):
    schemas: list[str]
    id: UUID
    externalId: str
    displayName: str
    members: list[ScimMember]
    meta: ScimMeta


class ScimGroupList(_Frozen):
    schemas: list[str]
    totalResults: int
    itemsPerPage: int
    startIndex: int
    Resources: list[ScimGroupResource]


def _user_meta(principal_id: UUID, created_at: datetime) -> ScimMeta:
    # lastModified 恒等于 created_at：principals 无 updated_at 列（设计 §13 偏差登记）。
    return ScimMeta(
        resourceType="User",
        created=created_at,
        lastModified=created_at,
        location=f"/scim/v2/Users/{principal_id}",
    )


def _group_meta(group_id: UUID, created_at: datetime) -> ScimMeta:
    return ScimMeta(
        resourceType="Group",
        created=created_at,
        lastModified=created_at,
        location=f"/scim/v2/Groups/{group_id}",
    )


class ScimService:
    """SCIM 子集应用服务；业务异常原样上抛（api 层映射 SCIM 错误体）。"""

    def __init__(
        self,
        *,
        identity_sessions: async_sessionmaker[AsyncSession],
        app_sessions: async_sessionmaker[AsyncSession],
        issuer: str,
    ) -> None:
        self._identity_sessions = identity_sessions
        self._app_sessions = app_sessions
        self._issuer = issuer

    # ---------------------------------------------------------------- Users

    async def create_user(
        self, *, principal_id: UUID, external_id: str, active: bool
    ) -> ScimUserResource:
        """创建 User principal + (issuer, external_id) 绑定；同一 identity 事务。

        预生成 principal_id 供 PEP 先于事务的 resource_id（设计 §7）。
        active=False → 同一事务内建后禁用（资源终态 active=False）。

        直接调用 IdentityStore 方法（不经过 create_user 命令）：命令层类型签名是
        IdentityRepositoryProtocol（god-protocol 含 tenant 方法），IdentityStore 只
        实现 identity-global 子集，经命令层会触发 pyright 协议不符；本层内联与
        命令等价的三步逻辑（get_external_identity → create_principal → bind）。
        """
        async with self._identity_sessions() as session, session.begin():
            store = IdentityStore(session)
            if (
                await store.get_external_identity(
                    issuer=self._issuer, subject=external_id
                )
                is not None
            ):
                raise ExternalIdentityConflictError(
                    "external identity is already bound to another principal"
                )
            principal = await store.create_principal(
                principal_id,
                kind=PrincipalKind.USER,
                status=PrincipalStatus.ACTIVE,
            )
            await store.bind_external_identity(
                issuer=self._issuer, subject=external_id, principal_id=principal.id
            )
            if not active:
                await store.set_principal_status(principal.id, PrincipalStatus.DISABLED)
            created_at = principal.created_at
        return ScimUserResource(
            schemas=[USER_SCHEMA],
            id=principal_id,
            externalId=external_id,
            userName=external_id,
            active=active,
            meta=_user_meta(principal_id, created_at),
        )

    async def get_user(self, principal_id: UUID) -> ScimUserResource | None:
        async with self._identity_sessions() as session:
            store = IdentityStore(session)
            principal = await store.get_principal(principal_id)
            if principal is None:
                return None
            identity = await store.get_external_identity_by_principal(principal_id)
            if identity is None:
                # 非 SCIM 供给主体（无外部身份绑定）不在 SCIM 面上暴露
                return None
        return ScimUserResource(
            schemas=[USER_SCHEMA],
            id=principal_id,
            externalId=identity.subject,
            userName=identity.subject,
            active=principal.status is PrincipalStatus.ACTIVE,
            meta=_user_meta(principal_id, principal.created_at),
        )

    async def set_user_status(
        self, principal_id: UUID, *, active: bool
    ) -> tuple[ScimUserResource, bool]:
        """状态迁移（enable/disable）；changed=False 表示目标状态已一致（零写入）。

        直接调用 IdentityStore 方法（不经 set_principal_status 命令）：同 create_user
        的协议不符理由；内联 read-then-CAS 的单写者语义（与命令等价）。
        """
        status = PrincipalStatus.ACTIVE if active else PrincipalStatus.DISABLED
        async with self._identity_sessions() as session, session.begin():
            store = IdentityStore(session)
            principal = await store.get_principal(principal_id)
            if principal is None:
                raise PrincipalNotFoundError("principal not found")
            if principal.status is status:
                changed = False
            else:
                updated = await store.set_principal_status(principal_id, status)
                if updated is None:
                    raise PrincipalNotFoundError("principal not found")
                principal = updated
                changed = True
            identity = await store.get_external_identity_by_principal(principal_id)
            if identity is None:
                raise _ScimUnprovisionedError(
                    f"principal {principal_id} has no external identity"
                )
        return (
            ScimUserResource(
                schemas=[USER_SCHEMA],
                id=principal_id,
                externalId=identity.subject,
                userName=identity.subject,
                active=principal.status is PrincipalStatus.ACTIVE,
                meta=_user_meta(principal_id, principal.created_at),
            ),
            changed,
        )

    # ---------------------------------------------------------------- Groups

    async def create_group(
        self,
        *,
        group_id: UUID,
        organization_id: UUID,
        workspace_id: UUID,
        name: str,
        member_ids: list[UUID],
    ) -> ScimGroupResource:
        """Workspace 内创建 Group + 初始成员集（幂等 add）；同一 tenant 事务原子。

        重名（同 workspace）→ NameConflictError（api 映射 409 uniqueness）；任何
        成员非法（不存在/disabled）→ 异常退出 tenant_session 整体回滚，零残留。
        """
        context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
        async with tenant_session(self._app_sessions, context) as session:
            repository = IdentityRepository(session, context)
            outcome = await create_group(
                repository,
                group_id=group_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                name=name,
            )
            if not outcome.created:
                raise _ScimUnprovisionedError("group create did not insert (unexpected)")
            for member_id in sorted(member_ids):
                await add_group_member(
                    repository,
                    group_id=group_id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    principal_id=member_id,
                )
            group = await repository.get_group(
                group_id, organization_id=organization_id, workspace_id=workspace_id
            )
            assert group is not None
            members = await repository.list_group_members(
                group_id=group_id, organization_id=organization_id, workspace_id=workspace_id
            )
            created_at = group.created_at
        return ScimGroupResource(
            schemas=[GROUP_SCHEMA],
            id=group_id,
            externalId=name,
            displayName=name,
            members=[ScimMember(value=m.principal_id) for m in members],
            meta=_group_meta(group_id, created_at),
        )

    async def get_group(
        self, *, group_id: UUID, organization_id: UUID, workspace_id: UUID
    ) -> ScimGroupResource | None:
        context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
        async with tenant_session(self._app_sessions, context) as session:
            repository = IdentityRepository(session, context)
            group = await repository.get_group(
                group_id, organization_id=organization_id, workspace_id=workspace_id
            )
            if group is None:
                return None
            members = await repository.list_group_members(
                group_id=group_id, organization_id=organization_id, workspace_id=workspace_id
            )
            created_at = group.created_at
        return ScimGroupResource(
            schemas=[GROUP_SCHEMA],
            id=group_id,
            externalId=group.name,
            displayName=group.name,
            members=[ScimMember(value=m.principal_id) for m in members],
            meta=_group_meta(group_id, created_at),
        )

    async def list_groups(
        self,
        *,
        organization_id: UUID,
        workspace_id: UUID,
        start_index: int,
        count: int,
    ) -> ScimGroupList:
        """基础分页（RFC 7644 §3.4.2 ListResponse 五字段）；无 filter（api 层拒绝）。"""
        context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
        async with tenant_session(self._app_sessions, context) as session:
            repository = IdentityRepository(session, context)
            groups = await repository.list_groups(
                organization_id=organization_id, workspace_id=workspace_id
            )
            resources: list[ScimGroupResource] = []
            for group in groups:
                members = await repository.list_group_members(
                    group_id=group.id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                )
                resources.append(
                    ScimGroupResource(
                        schemas=[GROUP_SCHEMA],
                        id=group.id,
                        externalId=group.name,
                        displayName=group.name,
                        members=[ScimMember(value=m.principal_id) for m in members],
                        meta=_group_meta(group.id, group.created_at),
                    )
                )
        total = len(resources)
        page = resources[start_index - 1 : start_index - 1 + count]
        return ScimGroupList(
            schemas=[LIST_SCHEMA],
            totalResults=total,
            itemsPerPage=len(page),
            startIndex=start_index,
            Resources=page,
        )

    async def reconcile_group(
        self,
        *,
        group_id: UUID,
        organization_id: UUID,
        workspace_id: UUID,
        name: str,
        member_ids: list[UUID],
    ) -> tuple[ScimGroupResource, bool] | None:
        """PUT replace 成员 reconciliation：diff add 缺失 + remove 多余，同一事务。

        changed=False（diff 为空）→ 零写入、零审计（幂等重放语义，与 T4 一致）；
        displayName 不可改名：调用方（api 层）已校验 name == 当前 group name。
        返回 None 表示 group 不存在（api 映射 404）。
        """
        context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
        async with tenant_session(self._app_sessions, context) as session:
            repository = IdentityRepository(session, context)
            group = await repository.get_group(
                group_id, organization_id=organization_id, workspace_id=workspace_id
            )
            if group is None:
                return None
            current = {
                m.principal_id
                for m in await repository.list_group_members(
                    group_id=group_id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                )
            }
            target = set(member_ids)
            to_add = target - current
            to_remove = current - target
            for member_id in sorted(to_add):
                await add_group_member(
                    repository,
                    group_id=group_id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    principal_id=member_id,
                )
            for member_id in sorted(to_remove):
                await repository.remove_group_member(
                    group_id=group_id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    principal_id=member_id,
                )
            members = await repository.list_group_members(
                group_id=group_id, organization_id=organization_id, workspace_id=workspace_id
            )
            created_at = group.created_at
        resource = ScimGroupResource(
            schemas=[GROUP_SCHEMA],
            id=group_id,
            externalId=name,
            displayName=name,
            members=[ScimMember(value=m.principal_id) for m in members],
            meta=_group_meta(group_id, created_at),
        )
        return resource, bool(to_add or to_remove)


class _ScimUnprovisionedError(RuntimeError):
    """内部哨兵：SCIM 面上不应出现的未供给状态（组未创建 / 主体无外部身份）。

    api 层不单独映射：只出现在不应触达的路径（组创建异常 / 非 SCIM 主体状态
    迁移），按业务异常上抛（500 场景，SCIM 形状保证覆盖 SCIM 控制内拒绝）。
    """
