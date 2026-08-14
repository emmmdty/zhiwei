"""S1-T1 CONTRACT REPAIR RED：identity domain / commands 契约。

上位契约（设计/验收方裁决）：
- 冻结总设计 §3.1：Organization → Workspace → Group/Membership，Group 是 Workspace scope；
- docs/API.md §1：所有 mutation 要求非空 Idempotency-Key；重复 key + 相同 payload 返回原结果，
  不同 payload 冲突（接入 S0 idempotency 基础，不另造机制）；
- docs/API.md §2 + T1 plan：Organization/Workspace 必须是 identity domain frozen models，
  commands 层提供 Organization/Workspace application commands；
- ActorContext：principal_id 必填、organization_id 可空（首登无组织）、workspace_id 非空时
  organization_id 必须非空；
- bootstrap 命令原子创建 Organization + 创建者 Owner Membership（OIDC 身份来源留 T2）。

本文件只测 domain + command 契约，用内存 fake repository 隔离数据库。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from zhiwei.contracts.time import utc_now
from zhiwei.identity.commands import (
    CommandOutcome,
    ExternalIdentityConflictError,
    IdempotencyRequest,
    IdempotencyResult,
    PrincipalDisabledError,
    PrincipalNotFoundError,
    add_group_member,
    add_org_membership,
    add_workspace_membership,
    create_group,
    create_organization,
    create_user,
    create_workspace,
    disable_principal,
    remove_org_membership,
)
from zhiwei.identity.domain import (
    ActorContext,
    ExternalIdentity,
    Group,
    GroupMember,
    Membership,
    Organization,
    Principal,
    PrincipalKind,
    PrincipalStatus,
    Workspace,
    WorkspaceMembership,
)
from zhiwei.persistence.repositories import IdempotencyConflict


class FakeIdentityRepository:
    """IdentityRepository 的内存替身；命令契约不绑定 SQLAlchemy。"""

    def __init__(self) -> None:
        self.principals: dict[UUID, Principal] = {}
        self.external_identities: dict[tuple[str, str], ExternalIdentity] = {}
        self.organizations: dict[UUID, Organization] = {}
        self.workspaces: dict[UUID, Workspace] = {}
        self.memberships: dict[tuple[UUID, UUID], Membership] = {}
        self.workspace_memberships: dict[tuple[UUID, UUID], WorkspaceMembership] = {}
        self.groups: dict[UUID, Group] = {}
        self.group_members: dict[tuple[UUID, UUID], GroupMember] = {}
        self.idempotency: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}

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

    async def claim_idempotency(
        self,
        *,
        scope: str,
        key: str,
        request_digest: str,
        response: dict[str, Any],
    ) -> IdempotencyResult:
        record = self.idempotency.get((scope, key))
        if record is not None:
            stored_digest, stored_response = record
            if stored_digest != request_digest:
                raise IdempotencyConflict("idempotency key was already used for another request")
            return IdempotencyResult(created=False, response=stored_response)
        self.idempotency[(scope, key)] = (request_digest, response)
        return IdempotencyResult(created=True, response=response)

    async def create_organization(self, organization_id: UUID, *, status: str) -> Organization:
        organization = Organization(id=organization_id, status=status, created_at=utc_now())
        self.organizations[organization_id] = organization
        return organization

    async def get_organization(self, organization_id: UUID) -> Organization | None:
        return self.organizations.get(organization_id)

    async def create_workspace(
        self, workspace_id: UUID, *, organization_id: UUID, name: str
    ) -> Workspace:
        workspace = Workspace(
            id=workspace_id, organization_id=organization_id, name=name, created_at=utc_now()
        )
        self.workspaces[workspace_id] = workspace
        return workspace

    async def list_workspaces(self, *, organization_id: UUID) -> list[Workspace]:
        return [
            workspace
            for workspace in self.workspaces.values()
            if workspace.organization_id == organization_id
        ]

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

    async def create_group(
        self, group_id: UUID, *, organization_id: UUID, workspace_id: UUID, name: str
    ) -> Group:
        group = Group(
            id=group_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            name=name,
            schema_version=1,
            created_at=utc_now(),
        )
        self.groups[group_id] = group
        return group

    async def get_group(
        self, group_id: UUID, *, organization_id: UUID, workspace_id: UUID
    ) -> Group | None:
        group = self.groups.get(group_id)
        if group is None or group.organization_id != organization_id or group.workspace_id != workspace_id:
            return None
        return group

    async def list_groups(
        self, *, organization_id: UUID, workspace_id: UUID
    ) -> list[Group]:
        return [
            group
            for group in self.groups.values()
            if group.organization_id == organization_id and group.workspace_id == workspace_id
        ]

    async def add_group_member(
        self, *, group_id: UUID, organization_id: UUID, workspace_id: UUID, principal_id: UUID
    ) -> GroupMember:
        key = (group_id, principal_id)
        existing = self.group_members.get(key)
        if existing is not None:
            return existing
        member = GroupMember(
            group_id=group_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            created_at=utc_now(),
        )
        self.group_members[key] = member
        return member

    async def get_group_member(
        self, *, group_id: UUID, organization_id: UUID, workspace_id: UUID, principal_id: UUID
    ) -> GroupMember | None:
        member = self.group_members.get((group_id, principal_id))
        if member is None or member.organization_id != organization_id or member.workspace_id != workspace_id:
            return None
        return member

    async def list_group_members(
        self, *, group_id: UUID, organization_id: UUID, workspace_id: UUID
    ) -> list[GroupMember]:
        return [
            member
            for (owner_group_id, _), member in self.group_members.items()
            if owner_group_id == group_id
            and member.organization_id == organization_id
            and member.workspace_id == workspace_id
        ]


def _idempotency(key: str = "request-key", digest_digit: str = "1") -> IdempotencyRequest:
    return IdempotencyRequest(key=key, request_digest="sha256:" + digest_digit * 64)


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


# --------------------------------------------------------------------------- Organization / Workspace


def test_organization_is_frozen_domain_model() -> None:
    organization = Organization(id=uuid4(), status="active", created_at=utc_now())
    assert organization.id is not None
    assert organization.status == "active"
    with pytest.raises(ValidationError):
        organization.status = "disabled"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Organization(id=uuid4(), created_at=utc_now(), tenant_id=uuid4())  # type: ignore[call-arg]


def test_workspace_is_frozen_domain_model() -> None:
    workspace = Workspace(
        id=uuid4(), organization_id=uuid4(), name="Sales", created_at=utc_now()
    )
    assert workspace.organization_id is not None
    with pytest.raises(ValidationError):
        workspace.name = "Eng"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Workspace(id=uuid4(), name="Sales", created_at=utc_now())  # type: ignore[call-arg]


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


# --------------------------------------------------------------------------- Group（Workspace scope）


def test_group_requires_organization_and_workspace_scope() -> None:
    group = Group(
        id=uuid4(),
        organization_id=uuid4(),
        workspace_id=uuid4(),
        name="Finance",
        schema_version=1,
        created_at=utc_now(),
    )
    assert group.organization_id is not None
    assert group.workspace_id is not None
    with pytest.raises(ValidationError):
        Group(  # type: ignore[call-arg]
            id=uuid4(),
            organization_id=uuid4(),
            name="Finance",
            schema_version=1,
            created_at=utc_now(),
        )
    with pytest.raises(ValidationError):
        Group(  # type: ignore[call-arg]
            id=uuid4(), workspace_id=uuid4(), name="Finance", created_at=utc_now()
        )


def test_group_member_requires_organization_workspace_and_group_scope() -> None:
    member = GroupMember(
        group_id=uuid4(),
        organization_id=uuid4(),
        workspace_id=uuid4(),
        principal_id=uuid4(),
        created_at=utc_now(),
    )
    assert member.organization_id is not None
    assert member.workspace_id is not None
    with pytest.raises(ValidationError):
        GroupMember(  # type: ignore[call-arg]
            group_id=uuid4(),
            organization_id=uuid4(),
            principal_id=uuid4(),
            created_at=utc_now(),
        )


def test_same_name_groups_in_different_workspaces_of_same_org_are_distinct() -> None:
    organization_id, first_workspace, second_workspace = uuid4(), uuid4(), uuid4()
    first = Group(
        id=uuid4(),
        organization_id=organization_id,
        workspace_id=first_workspace,
        name="Finance",
        created_at=utc_now(),
    )
    second = Group(
        id=uuid4(),
        organization_id=organization_id,
        workspace_id=second_workspace,
        name="Finance",
        created_at=utc_now(),
    )
    assert first.name == second.name
    assert (first.organization_id, first.workspace_id) != (second.organization_id, second.workspace_id)


# --------------------------------------------------------------------------- ActorContext


def test_actor_context_requires_principal_id() -> None:
    with pytest.raises(ValidationError):
        ActorContext(organization_id=uuid4())  # type: ignore[call-arg]


def test_actor_context_allows_principal_without_organization() -> None:
    actor = ActorContext(principal_id=uuid4())
    assert actor.organization_id is None
    assert actor.workspace_id is None


def test_actor_context_workspace_requires_organization() -> None:
    with pytest.raises(ValidationError):
        ActorContext(principal_id=uuid4(), workspace_id=uuid4())
    actor = ActorContext(
        principal_id=uuid4(), organization_id=uuid4(), workspace_id=uuid4()
    )
    assert actor.organization_id is not None


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
async def test_create_organization_bootstraps_org_and_owner_membership() -> None:
    repository = FakeIdentityRepository()
    owner = await create_user(repository, issuer="https://idp.example.com", subject="alice")
    organization_id = uuid4()
    outcome = await create_organization(
        repository, organization_id=organization_id, owner_principal_id=owner.id
    )
    assert outcome.created is True
    assert outcome.response == {"id": str(organization_id), "status": "active"}
    organization = await repository.get_organization(organization_id)
    assert organization is not None
    membership = await repository.get_membership(
        principal_id=owner.id, organization_id=organization_id
    )
    assert membership is not None
    assert membership.role_bindings == frozenset({"owner"})


@pytest.mark.asyncio
async def test_create_organization_is_idempotent_on_replay() -> None:
    repository = FakeIdentityRepository()
    owner = await create_user(repository, issuer="https://idp.example.com", subject="alice")
    organization_id = uuid4()
    first = await create_organization(
        repository,
        organization_id=organization_id,
        owner_principal_id=owner.id,
        idempotency=_idempotency(),
    )
    replayed = await create_organization(
        repository,
        organization_id=organization_id,
        owner_principal_id=owner.id,
        idempotency=_idempotency(),
    )
    assert first.created is True
    assert replayed.created is False
    assert replayed.response == first.response
    assert len(repository.organizations) == 1
    assert len(repository.memberships) == 1


@pytest.mark.asyncio
async def test_member_add_conflicting_payload_rejected() -> None:
    """org 级 mutation 的幂等键空间稳定（(org, scope, key)）：同 key + 不同 digest 冲突。

    bootstrap 不适用冲突断言：S0 idempotency 键空间含 organization_id，不同 org 的
    bootstrap 是独立幂等域，同 key + 不同 payload 不会污染既有数据。
    """
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    organization_id = uuid4()
    await add_org_membership(
        repository,
        principal_id=principal.id,
        organization_id=organization_id,
        idempotency=_idempotency(),
    )
    with pytest.raises(IdempotencyConflict):
        await add_org_membership(
            repository,
            principal_id=principal.id,
            organization_id=organization_id,
            role_bindings=frozenset({"owner"}),
            idempotency=_idempotency(digest_digit="2"),
        )


@pytest.mark.asyncio
async def test_create_workspace_command_is_idempotent_on_replay() -> None:
    repository = FakeIdentityRepository()
    owner = await create_user(repository, issuer="https://idp.example.com", subject="alice")
    organization_id = uuid4()
    await create_organization(
        repository, organization_id=organization_id, owner_principal_id=owner.id
    )
    workspace_id = uuid4()
    first = await create_workspace(
        repository,
        organization_id=organization_id,
        workspace_id=workspace_id,
        name="Sales",
        idempotency=_idempotency(),
    )
    replayed = await create_workspace(
        repository,
        organization_id=organization_id,
        workspace_id=workspace_id,
        name="Sales",
        idempotency=_idempotency(),
    )
    assert first.created is True
    assert first.response == {
        "id": str(workspace_id),
        "organization_id": str(organization_id),
        "name": "Sales",
    }
    assert replayed.created is False
    assert replayed.response == first.response
    assert len(repository.workspaces) == 1


@pytest.mark.asyncio
async def test_disabled_principal_cannot_gain_new_membership() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    await disable_principal(repository, principal.id)
    organization_id, workspace_id = uuid4(), uuid4()
    with pytest.raises(PrincipalDisabledError):
        await add_org_membership(
            repository, principal_id=principal.id, organization_id=organization_id
        )
    with pytest.raises(PrincipalDisabledError):
        await add_workspace_membership(
            repository,
            principal_id=principal.id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
    with pytest.raises(PrincipalDisabledError):
        await add_group_member(
            repository,
            group_id=uuid4(),
            organization_id=organization_id,
            workspace_id=workspace_id,
            principal_id=principal.id,
        )


@pytest.mark.asyncio
async def test_principal_can_join_multiple_organizations() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    org_a, org_b = uuid4(), uuid4()
    outcome_a = await add_org_membership(
        repository, principal_id=principal.id, organization_id=org_a
    )
    outcome_b = await add_org_membership(
        repository, principal_id=principal.id, organization_id=org_b
    )
    assert outcome_a.created is True
    assert outcome_b.created is True
    assert (outcome_a.response["principal_id"], outcome_a.response["organization_id"]) == (
        str(principal.id),
        str(org_a),
    )
    assert (outcome_b.response["principal_id"], outcome_b.response["organization_id"]) == (
        str(principal.id),
        str(org_b),
    )
    assert (
        await repository.get_membership(principal_id=principal.id, organization_id=org_a)
    ) is not None
    assert (
        await repository.get_membership(principal_id=principal.id, organization_id=org_b)
    ) is not None


@pytest.mark.asyncio
async def test_role_bindings_do_not_cross_organization_and_workspace_scope() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    organization_id, workspace_id = uuid4(), uuid4()
    outcome = await add_org_membership(
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
    assert outcome.response["role_bindings"] == ["owner"]
    assert workspace_membership.role_bindings == frozenset({"builder"})
    assert "builder" not in outcome.response["role_bindings"]
    assert "owner" not in workspace_membership.role_bindings


@pytest.mark.asyncio
async def test_member_add_and_remove_are_idempotent_on_replay() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    organization_id = uuid4()
    first = await add_org_membership(
        repository,
        principal_id=principal.id,
        organization_id=organization_id,
        role_bindings=frozenset({"member"}),
        idempotency=_idempotency(),
    )
    replayed = await add_org_membership(
        repository,
        principal_id=principal.id,
        organization_id=organization_id,
        role_bindings=frozenset({"member"}),
        idempotency=_idempotency(),
    )
    assert first.created is True
    assert replayed.created is False
    assert replayed.response == first.response
    assert len(repository.memberships) == 1

    removal = await remove_org_membership(
        repository,
        principal_id=principal.id,
        organization_id=organization_id,
        idempotency=_idempotency(key="remove-key"),
    )
    assert removal.created is True
    assert len(repository.memberships) == 0
    removal_replay = await remove_org_membership(
        repository,
        principal_id=principal.id,
        organization_id=organization_id,
        idempotency=_idempotency(key="remove-key"),
    )
    assert removal_replay.created is False


@pytest.mark.asyncio
async def test_group_member_add_is_idempotent_on_retry() -> None:
    repository = FakeIdentityRepository()
    principal = await create_user(
        repository, issuer="https://idp.example.com", subject="alice"
    )
    organization_id, workspace_id = uuid4(), uuid4()
    group_id = uuid4()
    await create_group(
        repository,
        group_id=group_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        name="Finance",
    )
    first = await add_group_member(
        repository,
        group_id=group_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        principal_id=principal.id,
    )
    second = await add_group_member(
        repository,
        group_id=group_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        principal_id=principal.id,
    )
    assert first == second
    members = await repository.list_group_members(
        group_id=group_id, organization_id=organization_id, workspace_id=workspace_id
    )
    assert members == [first]


@pytest.mark.asyncio
async def test_same_name_groups_in_different_workspaces_of_same_org_allowed() -> None:
    repository = FakeIdentityRepository()
    organization_id, first_workspace, second_workspace = uuid4(), uuid4(), uuid4()
    await create_group(
        repository,
        group_id=uuid4(),
        organization_id=organization_id,
        workspace_id=first_workspace,
        name="Finance",
    )
    await create_group(
        repository,
        group_id=uuid4(),
        organization_id=organization_id,
        workspace_id=second_workspace,
        name="Finance",
    )
    assert len(repository.groups) == 2


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
    removal = await remove_org_membership(
        repository, principal_id=principal.id, organization_id=organization_id
    )
    assert removal.created is True


@pytest.mark.asyncio
async def test_command_outcome_is_frozen() -> None:
    outcome = CommandOutcome(created=True, response={"id": str(uuid4())})
    with pytest.raises(ValidationError):
        outcome.created = False  # type: ignore[misc]
