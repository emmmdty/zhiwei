"""Tenant-explicit identity repositories.

Principal / ExternalIdentity 是 identity-global（无需 tenant context）；memberships、
workspace_memberships、groups、group_members 是 tenant-owned，repository 必须显式携带
tenant predicate，RLS 只是纵深防御（PERMISSIONS §5、总设计 §9.2）。

mutation 所需的资源 insert 使用 ON CONFLICT DO NOTHING：幂等命令的重放不会因资源已存在
而炸掉，由 claim_idempotency 判定首次执行还是重放。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.identity.commands import IdempotencyResult
from zhiwei.identity.domain import (
    ExternalIdentity,
    ExternalIdentityConflictError,
    Group,
    GroupMember,
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
from zhiwei.persistence.models import (
    ExternalIdentity as ExternalIdentityRow,
)
from zhiwei.persistence.models import (
    Group as GroupRow,
)
from zhiwei.persistence.models import (
    GroupMember as GroupMemberRow,
)
from zhiwei.persistence.models import (
    Membership as MembershipRow,
)
from zhiwei.persistence.models import (
    Organization as OrganizationRow,
)
from zhiwei.persistence.models import (
    Principal as PrincipalRow,
)
from zhiwei.persistence.models import (
    Workspace as WorkspaceRow,
)
from zhiwei.persistence.models import (
    WorkspaceMembership as WorkspaceMembershipRow,
)
from zhiwei.persistence.repositories import IdempotencyLookup, TenantRepository
from zhiwei.persistence.tenant import (
    TenantContext,
    TenantContextRequired,
    TenantScopeError,
)


class IdentityStore:
    """identity-global 数据访问（S1-T2：独立 zhiwei_identity 角色 + 独立 engine）。

    principals / external_identities / auth_sessions / oidc_login_attempts /
    secret_envelopes 不再经 zhiwei_app；本类只操作 identity 引擎的会话，
    不触碰任何 tenant-owned 表（组织/工作区/membership 发现走 SECURITY DEFINER 函数）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_principal(
        self,
        principal_id: UUID,
        *,
        kind: PrincipalKind,
        status: PrincipalStatus = PrincipalStatus.ACTIVE,
    ) -> Principal:
        row = PrincipalRow(
            id=principal_id, kind=kind.value, status=status.value, schema_version=1
        )
        self._session.add(row)
        await self._session.flush()
        return self._to_principal(row)

    async def get_principal(self, principal_id: UUID) -> Principal | None:
        row = await self._session.get(PrincipalRow, principal_id)
        return None if row is None else self._to_principal(row)

    async def disable_principal(self, principal_id: UUID) -> Principal | None:
        row = (
            await self._session.execute(
                update(PrincipalRow)
                .where(PrincipalRow.id == principal_id)
                .values(status=PrincipalStatus.DISABLED.value)
                .returning(PrincipalRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self._to_principal(row)

    async def bind_external_identity(
        self, *, issuer: str, subject: str, principal_id: UUID
    ) -> ExternalIdentity:
        row = (
            await self._session.execute(
                insert(ExternalIdentityRow)
                .values(issuer=issuer, subject=subject, principal_id=principal_id)
                .on_conflict_do_nothing(constraint="pk_external_identities")
                .returning(ExternalIdentityRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ExternalIdentityConflictError(
                "external identity is already bound to another principal"
            )
        return self._to_external_identity(row)

    async def get_external_identity(
        self, *, issuer: str, subject: str
    ) -> ExternalIdentity | None:
        row = await self._session.get(ExternalIdentityRow, {"issuer": issuer, "subject": subject})
        return None if row is None else self._to_external_identity(row)

    async def set_principal_status(
        self, principal_id: UUID, status: PrincipalStatus
    ) -> Principal | None:
        """状态切换（SCIM enable/disable 双向；S1-T5）。既有列级授权 UPDATE(status)
        覆盖（0003 zhiwei_identity）；disable_principal 保持不动（T1 冻结路径）。"""
        row = (
            await self._session.execute(
                update(PrincipalRow)
                .where(PrincipalRow.id == principal_id)
                .values(status=status.value)
                .returning(PrincipalRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self._to_principal(row)

    async def get_external_identity_by_principal(
        self, principal_id: UUID
    ) -> ExternalIdentity | None:
        """按 principal 读外部身份绑定（GET /Users/{id} 的 userName；S1-T5）。

        principal_id 非唯一键（schema 未建索引），确定性取 issuer,subject 排序
        首行；S1 SCIM 每 principal 只绑定一条。仅 SELECT，zhiwei_identity 既有
        表级 SELECT 授权覆盖。
        """
        row = (
            await self._session.execute(
                select(ExternalIdentityRow)
                .where(ExternalIdentityRow.principal_id == principal_id)
                .order_by(ExternalIdentityRow.issuer, ExternalIdentityRow.subject)
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else self._to_external_identity(row)

    @staticmethod
    def _to_principal(row: PrincipalRow) -> Principal:
        return Principal(
            id=row.id,
            kind=PrincipalKind(row.kind),
            status=PrincipalStatus(row.status),
            schema_version=row.schema_version,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_external_identity(row: ExternalIdentityRow) -> ExternalIdentity:
        return ExternalIdentity(issuer=row.issuer, subject=row.subject, principal_id=row.principal_id)


class IdentityRepository:
    """tenant-owned 身份数据访问（zhiwei_app 引擎）。

    S1-T2 角色分离后，principals / external_identities 的直接访问已撤销：
    principal 最小字段只能通过窄 SECURITY DEFINER 函数 zhiwei_principal_snapshot
    查询（支撑 T1 disabled 双保险）；identity-global 写入走 IdentityStore
    （zhiwei_identity 引擎）。
    """

    def __init__(self, session: AsyncSession, context: TenantContext | None) -> None:
        self._session = session
        self._context = context
        self._tenant = TenantRepository(session, context)

    # ------------------------------------------------------------------ principal 最小字段（窄函数）

    async def get_principal(self, principal_id: UUID) -> Principal | None:
        """经 SECURITY DEFINER 窄函数读取 kind/status 等最小字段，不直接访问表。"""
        result = await self._session.execute(
            text(
                "SELECT id, kind, status, schema_version, created_at "
                "FROM public.zhiwei_principal_snapshot(:pid)"
            ),
            {"pid": principal_id},
        )
        row = result.mappings().first()
        if row is None:
            return None
        return Principal(
            id=row["id"],
            kind=PrincipalKind(row["kind"]),
            status=PrincipalStatus(row["status"]),
            schema_version=row["schema_version"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------ 显式拒绝的 identity-global 写入
    #
    # 命令层协议是 identity-global + tenant 的并集；zhiwei_app 角色已撤销这些表的
    # 直接权限，这些方法在此显式失败（fail closed），identity-global 写入走
    # IdentityStore（zhiwei_identity 引擎）。

    async def create_principal(
        self,
        principal_id: UUID,
        *,
        kind: PrincipalKind,
        status: PrincipalStatus = PrincipalStatus.ACTIVE,
    ) -> Principal:
        raise NotImplementedError(
            "zhiwei_app cannot create principals; use IdentityStore on the identity engine"
        )

    async def disable_principal(self, principal_id: UUID) -> Principal | None:
        raise NotImplementedError(
            "zhiwei_app cannot disable principals; use IdentityStore on the identity engine"
        )

    async def set_principal_status(
        self, principal_id: UUID, status: PrincipalStatus
    ) -> Principal | None:
        raise NotImplementedError(
            "zhiwei_app cannot set principal status; use IdentityStore on the identity engine"
        )

    async def bind_external_identity(
        self, *, issuer: str, subject: str, principal_id: UUID
    ) -> ExternalIdentity:
        raise NotImplementedError(
            "zhiwei_app cannot bind external identities; use IdentityStore on the identity engine"
        )

    async def get_external_identity(
        self, *, issuer: str, subject: str
    ) -> ExternalIdentity | None:
        raise NotImplementedError(
            "zhiwei_app cannot read external identities; use IdentityStore on the identity engine"
        )

    # ------------------------------------------------------------------ organization / workspace

    async def create_organization(
        self, organization_id: UUID, *, status: str
    ) -> tuple[bool, Organization]:
        """INSERT ... RETURNING 原子区分「本次创建」与「组织已存在」，无先查后插 TOCTOU。"""
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_org_level(context)
        inserted = (
            await self._session.execute(
                insert(OrganizationRow)
                .values(
                    id=organization_id,
                    status=status,
                    retention_policy={},
                    schema_version=1,
                )
                .on_conflict_do_nothing(constraint="pk_organizations")
                .returning(OrganizationRow.id)
            )
        ).scalar_one_or_none()
        row = await self._session.get(OrganizationRow, organization_id)
        if row is None:
            raise RuntimeError("organization is not visible in tenant context after create")
        return inserted is not None, self._to_organization(row)

    async def get_organization(self, organization_id: UUID) -> Organization | None:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_org_level(context)
        row = await self._session.get(OrganizationRow, organization_id)
        return None if row is None else self._to_organization(row)

    async def create_workspace(
        self, workspace_id: UUID, *, organization_id: UUID, name: str
    ) -> tuple[bool, Workspace]:
        """INSERT ... RETURNING 原子区分创建/id 冲突；名称冲突由唯一约束报错转换。"""
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_org_level(context)
        try:
            inserted = (
                await self._session.execute(
                    insert(WorkspaceRow)
                    .values(
                        id=workspace_id,
                        organization_id=organization_id,
                        name=name,
                        classification_ceiling="PUBLIC",
                        budget_policy={},
                        schema_version=1,
                    )
                    .on_conflict_do_nothing(constraint="pk_workspaces")
                    .returning(WorkspaceRow.id)
                )
            ).scalar_one_or_none()
        except IntegrityError as error:
            # 名称唯一约束（uq_workspaces_org_name）命中：同一组织内重名
            raise NameConflictError(
                "workspace name is already taken in this organization"
            ) from error
        row = (
            await self._session.execute(
                select(WorkspaceRow).where(WorkspaceRow.id == workspace_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NameConflictError("workspace name is already taken in this organization")
        return inserted is not None, self._to_workspace(row)

    async def list_workspaces(self, *, organization_id: UUID) -> list[Workspace]:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_org_level(context)
        rows = await self._session.execute(
            select(WorkspaceRow)
            .where(WorkspaceRow.organization_id == organization_id)
            .order_by(WorkspaceRow.name)
        )
        return [self._to_workspace(row) for row in rows.scalars()]

    async def claim_idempotency(
        self,
        *,
        scope: str,
        key: str,
        request_digest: str,
        response: dict[str, object],
    ) -> IdempotencyResult:
        """委托 S0 idempotency 基础，只做应用层结果类型转换。"""
        result = await self._tenant.claim_idempotency(
            scope=scope,
            key=key,
            request_digest=request_digest,
            response=response,
        )
        return IdempotencyResult(created=result.created, response=result.response)

    async def claim_organization_bootstrap(
        self, principal_id: UUID, organization_id: UUID
    ) -> bool:
        """经窄 SECURITY DEFINER 函数声明 bootstrap claim（identity-global 最终围栏）。

        organization_bootstrap_claims 不给任何角色直接表权限（0008），本方法是唯一
        调用点：函数内部以 transaction-level advisory lock 按 principal 串行化，
        claim 已存在且 target 相同 → True；不同 → False；返回 False 时调用方必须
        抛 BootstrapClaimConflict，异常退出 tenant_session 后事务整体回滚。
        """
        result = await self._session.execute(
            text(
                "SELECT public.zhiwei_claim_organization_bootstrap(:principal_id, "
                ":organization_id)"
            ),
            {
                "principal_id": principal_id,
                "organization_id": organization_id,
            },
        )
        return bool(result.scalar_one())

    async def lookup_idempotency(
        self, *, scope: str, key: str
    ) -> IdempotencyLookup | None:
        """只读幂等查询：委托 S0 基础，不写入任何记录（既有资源路径专用）。"""
        return await self._tenant.lookup_idempotency(scope=scope, key=key)

    # ------------------------------------------------------------------ memberships

    async def add_membership(
        self,
        *,
        principal_id: UUID,
        organization_id: UUID,
        role_bindings: frozenset[str],
    ) -> tuple[bool, Membership]:
        """INSERT ... RETURNING 原子区分新 membership 与已存在 membership。"""
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_org_level(context)
        await self._require_active(principal_id)
        inserted = (
            await self._session.execute(
                insert(MembershipRow)
                .values(
                    principal_id=principal_id,
                    organization_id=organization_id,
                    role_bindings=sorted(role_bindings),
                )
                .on_conflict_do_nothing(constraint="pk_memberships")
                .returning(MembershipRow.principal_id)
            )
        ).scalar_one_or_none()
        row = (
            await self._session.execute(
                select(MembershipRow).where(
                    MembershipRow.principal_id == principal_id,
                    MembershipRow.organization_id == organization_id,
                )
            )
        ).scalar_one()
        return inserted is not None, self._to_membership(row)

    async def get_membership(
        self, *, principal_id: UUID, organization_id: UUID
    ) -> Membership | None:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_org_level(context)
        row = (
            await self._session.execute(
                select(MembershipRow).where(
                    MembershipRow.principal_id == principal_id,
                    MembershipRow.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._to_membership(row)

    async def list_memberships(self, *, organization_id: UUID) -> list[Membership]:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_org_level(context)
        rows = await self._session.execute(
            select(MembershipRow)
            .where(MembershipRow.organization_id == organization_id)
            .order_by(MembershipRow.principal_id)
        )
        return [self._to_membership(row) for row in rows.scalars()]

    async def remove_membership(
        self, *, principal_id: UUID, organization_id: UUID
    ) -> bool:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_org_level(context)
        result = await self._session.execute(
            delete(MembershipRow)
            .where(
                MembershipRow.principal_id == principal_id,
                MembershipRow.organization_id == organization_id,
            )
            .returning(MembershipRow.principal_id)
        )
        return result.scalar_one_or_none() is not None

    async def add_workspace_membership(
        self,
        *,
        principal_id: UUID,
        organization_id: UUID,
        workspace_id: UUID,
        role_bindings: frozenset[str],
    ) -> WorkspaceMembership:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_workspace(workspace_id, context)
        await self._require_active(principal_id)
        row = WorkspaceMembershipRow(
            principal_id=principal_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            role_bindings=sorted(role_bindings),
        )
        self._session.add(row)
        await self._session.flush()
        return self._to_workspace_membership(row)

    async def get_workspace_membership(
        self, *, principal_id: UUID, workspace_id: UUID
    ) -> WorkspaceMembership | None:
        context = self._require_context()
        self._require_workspace(workspace_id, context)
        row = (
            await self._session.execute(
                select(WorkspaceMembershipRow).where(
                    WorkspaceMembershipRow.principal_id == principal_id,
                    WorkspaceMembershipRow.workspace_id == workspace_id,
                    WorkspaceMembershipRow.organization_id == context.organization_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._to_workspace_membership(row)

    async def list_workspace_memberships(
        self, *, workspace_id: UUID
    ) -> list[WorkspaceMembership]:
        context = self._require_context()
        self._require_workspace(workspace_id, context)
        rows = await self._session.execute(
            select(WorkspaceMembershipRow)
            .where(
                WorkspaceMembershipRow.workspace_id == workspace_id,
                WorkspaceMembershipRow.organization_id == context.organization_id,
            )
            .order_by(WorkspaceMembershipRow.principal_id)
        )
        return [self._to_workspace_membership(row) for row in rows.scalars()]

    # ------------------------------------------------------------------ groups（Workspace scope）

    async def create_group(
        self, group_id: UUID, *, organization_id: UUID, workspace_id: UUID, name: str
    ) -> tuple[bool, Group]:
        """INSERT ... RETURNING 原子区分创建/id 冲突；名称冲突由唯一约束报错转换。"""
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_workspace(workspace_id, context)
        try:
            inserted = (
                await self._session.execute(
                    insert(GroupRow)
                    .values(
                        id=group_id,
                        organization_id=organization_id,
                        workspace_id=workspace_id,
                        name=name,
                        schema_version=1,
                    )
                    .on_conflict_do_nothing(constraint="pk_groups")
                    .returning(GroupRow.id)
                )
            ).scalar_one_or_none()
        except IntegrityError as error:
            # 名称唯一约束（uq_groups_scope_name）命中：同一 workspace 内重名
            raise NameConflictError("group name is already taken in this workspace") from error
        row = (
            await self._session.execute(
                select(GroupRow).where(
                    GroupRow.id == group_id,
                    GroupRow.organization_id == organization_id,
                    GroupRow.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NameConflictError("group name is already taken in this workspace")
        return inserted is not None, self._to_group(row)

    async def get_group(
        self, group_id: UUID, *, organization_id: UUID, workspace_id: UUID
    ) -> Group | None:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_workspace(workspace_id, context)
        row = (
            await self._session.execute(
                select(GroupRow).where(
                    GroupRow.id == group_id,
                    GroupRow.organization_id == organization_id,
                    GroupRow.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._to_group(row)

    async def list_groups(
        self, *, organization_id: UUID, workspace_id: UUID
    ) -> list[Group]:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_workspace(workspace_id, context)
        rows = await self._session.execute(
            select(GroupRow)
            .where(
                GroupRow.organization_id == organization_id,
                GroupRow.workspace_id == workspace_id,
            )
            .order_by(GroupRow.name)
        )
        return [self._to_group(row) for row in rows.scalars()]

    async def add_group_member(
        self, *, group_id: UUID, organization_id: UUID, workspace_id: UUID, principal_id: UUID
    ) -> GroupMember:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_workspace(workspace_id, context)
        await self._require_active(principal_id)
        row = (
            await self._session.execute(
                insert(GroupMemberRow)
                .values(
                    group_id=group_id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                )
                .on_conflict_do_nothing(constraint="pk_group_members")
                .returning(GroupMemberRow)
            )
        ).scalar_one_or_none()
        if row is not None:
            return self._to_group_member(row)
        existing = (
            await self._session.execute(
                select(GroupMemberRow).where(
                    GroupMemberRow.group_id == group_id,
                    GroupMemberRow.organization_id == organization_id,
                    GroupMemberRow.workspace_id == workspace_id,
                    GroupMemberRow.principal_id == principal_id,
                )
            )
        ).scalar_one()
        return self._to_group_member(existing)

    async def get_group_member(
        self, *, group_id: UUID, organization_id: UUID, workspace_id: UUID, principal_id: UUID
    ) -> GroupMember | None:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_workspace(workspace_id, context)
        row = (
            await self._session.execute(
                select(GroupMemberRow).where(
                    GroupMemberRow.group_id == group_id,
                    GroupMemberRow.organization_id == organization_id,
                    GroupMemberRow.workspace_id == workspace_id,
                    GroupMemberRow.principal_id == principal_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._to_group_member(row)

    async def list_group_members(
        self, *, group_id: UUID, organization_id: UUID, workspace_id: UUID
    ) -> list[GroupMember]:
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_workspace(workspace_id, context)
        rows = await self._session.execute(
            select(GroupMemberRow)
            .where(
                GroupMemberRow.group_id == group_id,
                GroupMemberRow.organization_id == organization_id,
                GroupMemberRow.workspace_id == workspace_id,
            )
            .order_by(GroupMemberRow.principal_id)
        )
        return [self._to_group_member(row) for row in rows.scalars()]

    async def remove_group_member(
        self, *, group_id: UUID, organization_id: UUID, workspace_id: UUID, principal_id: UUID
    ) -> bool:
        """移除 Group 成员（SCIM reconciliation remove 方向；S1-T5）。

        tenant guard 与 add_group_member 同款；重复 remove 返回 False（diff 语义
        幂等）。DELETE 授权由 0009 补授（0002 只给 SELECT, INSERT）。
        """
        context = self._require_context()
        self._require_organization(organization_id, context)
        self._require_workspace(workspace_id, context)
        result = await self._session.execute(
            delete(GroupMemberRow)
            .where(
                GroupMemberRow.group_id == group_id,
                GroupMemberRow.organization_id == organization_id,
                GroupMemberRow.workspace_id == workspace_id,
                GroupMemberRow.principal_id == principal_id,
            )
            .returning(GroupMemberRow.principal_id)
        )
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------ guards

    def _require_context(self) -> TenantContext:
        if self._context is None:
            raise TenantContextRequired("organization context is required")
        return self._context

    @staticmethod
    def _require_organization(organization_id: UUID, context: TenantContext) -> None:
        if organization_id != context.organization_id:
            raise TenantScopeError("organization target does not match tenant context")

    @staticmethod
    def _require_workspace(workspace_id: UUID, context: TenantContext) -> None:
        if context.workspace_id != workspace_id:
            raise TenantScopeError("workspace target does not match tenant context")

    @staticmethod
    def _require_org_level(context: TenantContext) -> None:
        if context.workspace_id is not None:
            raise TenantScopeError(
                "organization scope requires organization-level tenant context"
            )

    async def _require_active(self, principal_id: UUID) -> None:
        principal = await self.get_principal(principal_id)
        if principal is None:
            raise PrincipalNotFoundError("principal not found")
        if principal.is_disabled:
            raise PrincipalDisabledError("disabled principals cannot gain new memberships")

    # ------------------------------------------------------------------ converters

    @staticmethod
    def _to_principal(row: PrincipalRow) -> Principal:
        return Principal(
            id=row.id,
            kind=PrincipalKind(row.kind),
            status=PrincipalStatus(row.status),
            schema_version=row.schema_version,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_external_identity(row: ExternalIdentityRow) -> ExternalIdentity:
        return ExternalIdentity(issuer=row.issuer, subject=row.subject, principal_id=row.principal_id)

    @staticmethod
    def _to_organization(row: OrganizationRow) -> Organization:
        return Organization(
            id=row.id,
            status=row.status,
            policy_ref=row.policy_ref,
            retention_policy=row.retention_policy,
            schema_version=row.schema_version,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_workspace(row: WorkspaceRow) -> Workspace:
        return Workspace(
            id=row.id,
            organization_id=row.organization_id,
            name=row.name,
            classification_ceiling=row.classification_ceiling,
            budget_policy=row.budget_policy,
            schema_version=row.schema_version,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_membership(row: MembershipRow) -> Membership:
        return Membership(
            principal_id=row.principal_id,
            organization_id=row.organization_id,
            role_bindings=frozenset(row.role_bindings),
        )

    @staticmethod
    def _to_workspace_membership(row: WorkspaceMembershipRow) -> WorkspaceMembership:
        return WorkspaceMembership(
            principal_id=row.principal_id,
            organization_id=row.organization_id,
            workspace_id=row.workspace_id,
            role_bindings=frozenset(row.role_bindings),
        )

    @staticmethod
    def _to_group(row: GroupRow) -> Group:
        return Group(
            id=row.id,
            organization_id=row.organization_id,
            workspace_id=row.workspace_id,
            name=row.name,
            schema_version=row.schema_version,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_group_member(row: GroupMemberRow) -> GroupMember:
        return GroupMember(
            group_id=row.group_id,
            organization_id=row.organization_id,
            workspace_id=row.workspace_id,
            principal_id=row.principal_id,
            created_at=row.created_at,
        )
