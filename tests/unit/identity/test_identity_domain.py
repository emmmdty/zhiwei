"""S1-T1 RED：Principal / ExternalIdentity / Membership / Group 领域契约。

冻结的事实源：
- DATA_MODEL §2：Principal(kind=user|service_account|agent_identity)、ExternalIdentity(principal_id,
  issuer, subject)、Membership / WorkspaceMembership / Group / GroupMember；
- PERMISSIONS §1：外部稳定键为 OIDC `(issuer, subject)`，不是 email；
- 总设计 §9.1：AgentIdentity 不能交互登录；SCIM disable 使新请求立即拒绝；
- 边界裁决 S1-T1：Principal 可属多 Organization；Membership 与 WorkspaceMembership 分离；
  role bindings 不跨 org/workspace；disabled Principal 不能获得新的 membership；
  GroupMember 重试幂等；domain model 不可原地静默修改。

本文件只测 domain + command 契约，用内存 fake repository 隔离数据库。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from zhiwei.contracts.time import utc_now
from zhiwei.identity.commands import (
    ExternalIdentityConflictError,
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
    ExternalIdentity,
    Group,
    GroupMember,
    Membership,
    Principal,
    PrincipalKind,
    PrincipalStatus,
    WorkspaceMembership,
)


class FakeIdentityRepository:
    """IdentityRepository 的内存替身；命令契约不绑定 SQLAlchemy。"""

    def __init__(self) -> None:
        self.principals: dict[UUID, Principal] = {}
        self.external_identities: dict[tuple[str, str], ExternalIdentity] = {}
        self.memberships: dict[tuple[UUID, UUID], Membership] = {}
        self.workspace_memberships: dict[tuple[UUID, UUID], WorkspaceMembership] = {}
        self.groups: dict[UUID, Group] = {}
        self.group_members: dict[tuple[UUID, UUID], GroupMember] = {}

    async def create_principal(
        self,
        principal_id: UUID,
        *,
        kind: PrincipalKind,
        status: PrincipalStatus = PrincipalStatus.ACTIVE,
    ) -> Principal:
        principal = Principal(id=principal_id, kind=kind, status=status, created_at=utc_now())
        self.principals[principal_id] = principal
        return principal

    async def get_principal(self, principal_id: UUID) -> Principal | None:
        return self.principals.get(principal_id)

    async def disable_principal(self, principal_id: UUID) -> Principal | None:
        principal = self.principals.get(principal_id)
        if principal is None:
            return None
        disabled = principal.disable()
        self.principals[principal_id] = disabled
        return disabled

    async def bind_external_identity(
        self, *, issuer: str, subject: str, principal_id: UUID
    ) -> ExternalIdentity:
        key = (issuer, subject)
        if key in self.external_identities:
            raise ExternalIdentityConflictError("external identity already bound to another principal")
        identity = ExternalIdentity(issuer=issuer, subject=subject, principal_id=principal_id)
        self.external_identities[key] = identity
        return identity

    async def get_external_identity(
        self, *, issuer: str, subject: str
    ) -> ExternalIdentity | None:
        return self.external_identities.get((issuer, subject))

    async def add_membership(
        self,
        *,
        principal_id: UUID,
        organization_id: UUID,
        role_bindings: frozenset[str],
    ) -> Membership:
        membership = Membership(
            principal_id=principal_id,
            organization_id=organization_id,
            role_bindings=role_bindings,
        )
        self.memberships[(principal_id, organization_id)] = membership
        return membership

    async def get_membership(
        self, *, principal_id: UUID, organization_id: UUID
    ) -> Membership | None:
        return self.memberships.get((principal_id, organization_id))

    async def remove_membership(self, *, principal_id: UUID, organization_id: UUID) -> bool:
        return self.memberships.pop((principal_id, organization_id), None) is not None

    async def add_workspace_membership(
        self,
        *,
        principal_id: UUID,
        organization_id: UUID,
        workspace_id: UUID,
        role_bindings: frozenset[str],
    ) -> WorkspaceMembership:
        membership = WorkspaceMembership(
            principal_id=principal_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            role_bindings=role_bindings,
        )
        self.workspace_memberships[(principal_id, workspace_id)] = membership
        return membership

    async def get_workspace_membership(
        self, *, principal_id: UUID, workspace_id: UUID
    ) -> WorkspaceMembership | None:
        return self.workspace_memberships.get((principal_id, workspace_id))

    async def create_group(self, group_id: UUID, *, organization_id: UUID, name: str) -> Group:
        group = Group(
            id=group_id,
            organization_id=organization_id,
            name=name,
            schema_version=1,
            created_at=utc_now(),
        )
        self.groups[group_id] = group
        return group

    async def get_group(self, group_id: UUID, *, organization_id: UUID) -> Group | None:
        return self.groups.get(group_id)

    async def add_group_member(
        self, *, group_id: UUID, organization_id: UUID, principal_id: UUID
    ) -> GroupMember:
        key = (group_id, principal_id)
        existing = self.group_members.get(key)
        if existing is not None:
            return existing
        member = GroupMember(
            group_id=group_id,
            organization_id=organization_id,
            principal_id=principal_id,
            created_at=utc_now(),
        )
        self.group_members[key] = member
        return member

    async def get_group_member(
        self, *, group_id: UUID, organization_id: UUID, principal_id: UUID
    ) -> GroupMember | None:
        return self.group_members.get((group_id, principal_id))

    async def list_group_members(
        self, *, group_id: UUID, organization_id: UUID
    ) -> list[GroupMember]:
        return [
            member
            for (owner_group_id, _), member in self.group_members.items()
            if owner_group_id == group_id
        ]


# --------------------------------------------------------------------------- Principal


def test_principal_accepts_three_legal_kinds() -> None:
    for kind in (
        PrincipalKind.USER,
        PrincipalKind.SERVICE_ACCOUNT,
        PrincipalKind.AGENT_IDENTITY,
    ):
        principal = Principal(id=uuid4(), kind=kind, created_at=utc_now())
        assert principal.kind is kind


def test_principal_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        Principal(id=uuid4(), kind="root", created_at=utc_now())  # type: ignore[arg-type]


def test_principal_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        Principal(
            id=uuid4(),
            kind=PrincipalKind.USER,
            status="deleted",  # type: ignore[arg-type]
            created_at=utc_now(),
        )


def test_principal_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError):
        Principal(
            id=uuid4(),
            kind=PrincipalKind.USER,
            created_at=datetime(2026, 1, 1, 12, 0, 0),
        )


def test_agent_identity_cannot_login_interactively() -> None:
    agent = Principal(
        id=uuid4(), kind=PrincipalKind.AGENT_IDENTITY, created_at=utc_now()
    )
    assert agent.supports_interactive_login is False


def test_user_supports_interactive_login() -> None:
    user = Principal(id=uuid4(), kind=PrincipalKind.USER, created_at=utc_now())
    assert user.supports_interactive_login is True


def test_service_account_does_not_support_interactive_login() -> None:
    service_account = Principal(
        id=uuid4(), kind=PrincipalKind.SERVICE_ACCOUNT, created_at=utc_now()
    )
    assert service_account.supports_interactive_login is False


def test_disable_returns_a_new_instance_and_keeps_the_original() -> None:
    principal = Principal(id=uuid4(), kind=PrincipalKind.USER, created_at=utc_now())
    disabled = principal.disable()
    assert disabled is not principal
    assert disabled.id == principal.id
    assert disabled.kind is principal.kind
    assert disabled.status is PrincipalStatus.DISABLED
    assert principal.status is PrincipalStatus.ACTIVE


def test_disable_is_idempotent() -> None:
    principal = Principal(id=uuid4(), kind=PrincipalKind.USER, created_at=utc_now())
    assert principal.disable().disable().status is PrincipalStatus.DISABLED


def test_principal_domain_model_is_frozen() -> None:
    principal = Principal(id=uuid4(), kind=PrincipalKind.USER, created_at=utc_now())
    with pytest.raises(ValidationError):
        principal.status = PrincipalStatus.DISABLED  # type: ignore[misc]


# --------------------------------------------------------------------------- ExternalIdentity


def test_external_identity_stable_key_is_issuer_and_subject() -> None:
    identity = ExternalIdentity(
        issuer="https://idp.example.com", subject="a1b2c3", principal_id=uuid4()
    )
    assert identity.stable_key == ("https://idp.example.com", "a1b2c3")


def test_external_identity_does_not_use_email_as_key() -> None:
    assert "email" not in ExternalIdentity.model_fields
    with pytest.raises(ValidationError):
        ExternalIdentity(
            issuer="https://idp.example.com",
            subject="a1b2c3",
            principal_id=uuid4(),
            email="alice@example.com",  # type: ignore[call-arg]
        )


def test_external_identity_requires_nonempty_issuer_and_subject() -> None:
    with pytest.raises(ValidationError):
        ExternalIdentity(issuer="", subject="a1b2c3", principal_id=uuid4())
    with pytest.raises(ValidationError):
        ExternalIdentity(issuer="https://idp.example.com", subject="", principal_id=uuid4())


def test_external_identity_domain_model_is_frozen() -> None:
    identity = ExternalIdentity(
        issuer="https://idp.example.com", subject="a1b2c3", principal_id=uuid4()
    )
    with pytest.raises(ValidationError):
        identity.subject = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------- Membership


def test_membership_is_organization_scoped_only() -> None:
    membership = Membership(
        principal_id=uuid4(),
        organization_id=uuid4(),
        role_bindings=frozenset({"member", "approver"}),
    )
    assert membership.organization_id is not None
    assert "workspace_id" not in Membership.model_fields


def test_workspace_membership_requires_organization_and_workspace_scope() -> None:
    membership = WorkspaceMembership(
        principal_id=uuid4(),
        organization_id=uuid4(),
        workspace_id=uuid4(),
        role_bindings=frozenset({"builder"}),
    )
    assert membership.organization_id is not None
    assert membership.workspace_id is not None
    with pytest.raises(ValidationError):
        WorkspaceMembership(  # type: ignore[call-arg]
            principal_id=uuid4(), workspace_id=uuid4()
        )
    with pytest.raises(ValidationError):
        WorkspaceMembership(  # type: ignore[call-arg]
            principal_id=uuid4(), organization_id=uuid4()
        )


def test_principal_can_belong_to_multiple_organizations() -> None:
    principal_id, org_a, org_b = uuid4(), uuid4(), uuid4()
    membership_a = Membership(
        principal_id=principal_id, organization_id=org_a, role_bindings=frozenset({"member"})
    )
    membership_b = Membership(
        principal_id=principal_id, organization_id=org_b, role_bindings=frozenset({"owner"})
    )
    assert (membership_a.principal_id, membership_a.organization_id) == (principal_id, org_a)
    assert (membership_b.principal_id, membership_b.organization_id) == (principal_id, org_b)


def test_group_is_organization_scoped() -> None:
    group = Group(
        id=uuid4(),
        organization_id=uuid4(),
        name="Finance",
        schema_version=1,
        created_at=utc_now(),
    )
    assert group.organization_id is not None
    with pytest.raises(ValidationError):
        Group(  # type: ignore[call-arg]
            id=uuid4(), name="Finance", schema_version=1, created_at=utc_now()
        )


# --------------------------------------------------------------------------- Commands


@pytest.mark.asyncio
async def test_create_user_creates_user_principal_and_binds_identity() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    assert principal.kind is PrincipalKind.USER
    assert principal.status is PrincipalStatus.ACTIVE
    bound = await repository.get_external_identity(
        issuer="https://idp.example.com", subject="alice"
    )
    assert bound == ExternalIdentity(
        issuer="https://idp.example.com", subject="alice", principal_id=principal.id
    )


@pytest.mark.asyncio
async def test_create_user_rejects_reused_external_identity() -> None:
    repository = FakeIdentityRepository()
    await create_user(repository, issuer="https://idp.example.com", subject="alice")
    with pytest.raises(ExternalIdentityConflictError):
        await create_user(repository, issuer="https://idp.example.com", subject="alice")


@pytest.mark.asyncio
async def test_disable_principal_command_lifecycle() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    disabled = await disable_principal(repository, principal.id)
    assert disabled.status is PrincipalStatus.DISABLED
    assert (await repository.get_principal(principal.id)) == disabled
    with pytest.raises(PrincipalNotFoundError):
        await disable_principal(repository, uuid4())


@pytest.mark.asyncio
async def test_disabled_principal_cannot_gain_new_membership() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    await disable_principal(repository, principal.id)
    with pytest.raises(PrincipalDisabledError):
        await add_org_membership(
            repository, principal_id=principal.id, organization_id=uuid4()
        )
    with pytest.raises(PrincipalDisabledError):
        await add_workspace_membership(
            repository,
            principal_id=principal.id,
            organization_id=uuid4(),
            workspace_id=uuid4(),
        )
    with pytest.raises(PrincipalDisabledError):
        await add_group_member(
            repository,
            group_id=uuid4(),
            organization_id=uuid4(),
            principal_id=principal.id,
        )


@pytest.mark.asyncio
async def test_principal_can_join_multiple_organizations() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    org_a, org_b = uuid4(), uuid4()
    membership_a = await add_org_membership(
        repository, principal_id=principal.id, organization_id=org_a
    )
    membership_b = await add_org_membership(
        repository, principal_id=principal.id, organization_id=org_b
    )
    assert (membership_a.principal_id, membership_a.organization_id) == (principal.id, org_a)
    assert (membership_b.principal_id, membership_b.organization_id) == (principal.id, org_b)
    assert (await repository.get_membership(principal_id=principal.id, organization_id=org_a)) is not None
    assert (await repository.get_membership(principal_id=principal.id, organization_id=org_b)) is not None


@pytest.mark.asyncio
async def test_role_bindings_do_not_cross_organization_and_workspace_scope() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    organization_id, workspace_id = uuid4(), uuid4()
    membership = await add_org_membership(
        repository,
        principal_id=principal.id,
        organization_id=organization_id,
        role_bindings=frozenset({"owner"}),
    )
    workspace_membership = await add_workspace_membership(
        repository,
        principal_id=principal.id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        role_bindings=frozenset({"builder"}),
    )
    assert membership.role_bindings == frozenset({"owner"})
    assert workspace_membership.role_bindings == frozenset({"builder"})
    assert "builder" not in membership.role_bindings
    assert "owner" not in workspace_membership.role_bindings


@pytest.mark.asyncio
async def test_remove_org_membership_is_idempotent() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    organization_id = uuid4()
    await add_org_membership(
        repository, principal_id=principal.id, organization_id=organization_id
    )
    assert await remove_org_membership(
        repository, principal_id=principal.id, organization_id=organization_id
    ) is True
    assert await remove_org_membership(
        repository, principal_id=principal.id, organization_id=organization_id
    ) is False


@pytest.mark.asyncio
async def test_group_member_add_is_idempotent_on_retry() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    group_id, organization_id = uuid4(), uuid4()
    await create_group(repository, group_id=group_id, organization_id=organization_id, name="Finance")
    first = await add_group_member(
        repository,
        group_id=group_id,
        organization_id=organization_id,
        principal_id=principal.id,
    )
    second = await add_group_member(
        repository,
        group_id=group_id,
        organization_id=organization_id,
        principal_id=principal.id,
    )
    assert first == second
    members = await repository.list_group_members(
        group_id=group_id, organization_id=organization_id
    )
    assert members == [first]


@pytest.mark.asyncio
async def test_disabled_principal_can_still_be_removed_from_membership() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    organization_id = uuid4()
    await add_org_membership(
        repository, principal_id=principal.id, organization_id=organization_id
    )
    await disable_principal(repository, principal.id)
    assert await remove_org_membership(
        repository, principal_id=principal.id, organization_id=organization_id
    ) is True
