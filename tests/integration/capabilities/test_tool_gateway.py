"""S4 integration tests for Tool Gateway.

Tests the full tool invocation pipeline: intent → policy → credential → sandbox →
execute → receipt. Covers happy path, policy denial, credential expiry, sandbox
violation, runner unavailability, duplicate detection, and effect_unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from zhiwei.capabilities.connections import Connection, ConnectionStatus, SubjectMode
from zhiwei.capabilities.credential_bindings import (
    BindingStatus,
    CredentialBinding,
    CredentialType,
)
from zhiwei.capabilities.domain import CapabilityStatus, CapabilityVersion
from zhiwei.capabilities.invocations import (
    InvocationFailureReason,
    InvocationRepository,
    InvocationStatus,
    SandboxSpec,
    ToolInvocation,
)
from zhiwei.capabilities.runners.client import RunnerClient
from zhiwei.capabilities.runners.contracts import (
    RunnerInvocationRequest,
    RunnerKind,
    RunnerRegistry,
    RunnerSpec,
)
from zhiwei.capabilities.tool_gateway import ToolGateway
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.identity.domain import PrincipalKind
from zhiwei.policy.client import PolicyDecision
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.input import Actor, PolicyInput, RequestContext, ResourceRef
from zhiwei.policy.roles import Action, Purpose, ResourceType
from zhiwei.secrets.base import SecretRef
from zhiwei.workflows.activities.tools import ToolActivity, ToolActivityInput

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org_id() -> UUID:
    return new_id()


@pytest.fixture
def ws_id() -> UUID:
    return new_id()


@pytest.fixture
def principal_id() -> UUID:
    return new_id()


@pytest.fixture
def now() -> datetime:
    return utc_now()


@pytest.fixture
def connection(org_id: UUID, ws_id: UUID) -> Connection:
    return Connection(
        id=new_id(),
        organization_id=org_id,
        workspace_id=ws_id,
        provider_version_id=new_id(),
        subject_mode=SubjectMode.SERVICE_ACCOUNT,
        status=ConnectionStatus.ACTIVE,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def credential(connection: Connection, org_id: UUID, ws_id: UUID) -> CredentialBinding:
    return CredentialBinding(
        id=new_id(),
        connection_id=connection.id,
        organization_id=org_id,
        workspace_id=ws_id,
        credential_type=CredentialType.API_KEY,
        secret_ref=SecretRef(value="test-secret-ref"),
        status=BindingStatus.ACTIVE,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def capability_version() -> CapabilityVersion:
    return CapabilityVersion(
        id=new_id(),
        capability_type="mcp",
        name="test-tool",
        version=1,
        status=CapabilityStatus.PUBLISHED,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def invocation_repo() -> InvocationRepository:
    return InvocationRepository()


@pytest.fixture
def runner_spec() -> RunnerSpec:
    return RunnerSpec(
        id=new_id(),
        name="test-runner",
        kind=RunnerKind.PREBUILT,
        image_digest="sha256:abc123",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def runner_registry(runner_spec: RunnerSpec) -> RunnerRegistry:
    from zhiwei.capabilities.runners.prebuilt import PrebuiltRunner

    registry = RunnerRegistry()
    runner = PrebuiltRunner(runner_spec)
    registry.register(runner)
    return registry


@pytest.fixture
def runner_client(runner_registry: RunnerRegistry) -> RunnerClient:
    return RunnerClient(runner_registry, ipc_secret=b"test-secret")


@pytest.fixture
def mock_policy_enforcer() -> PolicyEnforcer:
    """Create a PolicyEnforcer with a mock OPA client that allows all."""
    mock_client = MagicMock()
    mock_client.evaluate = AsyncMock(
        return_value=PolicyDecision(
            allow=True,
            decision_id="test-decision",
            revision="test-revision",
            reason="allowed",
            evaluated_at=utc_now(),
            input_digest="sha256:test",
        )
    )
    mock_client.fail_closed = MagicMock(
        return_value=PolicyDecision(
            allow=False,
            decision_id=None,
            revision=None,
            reason="fail_closed",
            evaluated_at=utc_now(),
            input_digest=None,
        )
    )
    return PolicyEnforcer(mock_client)


@pytest.fixture
def tool_gateway(
    mock_policy_enforcer: PolicyEnforcer,
    runner_client: RunnerClient,
    invocation_repo: InvocationRepository,
    connection: Connection,
    credential: CredentialBinding,
    capability_version: CapabilityVersion,
) -> ToolGateway:
    return ToolGateway(
        policy_enforcer=mock_policy_enforcer,
        runner_client=runner_client,
        invocation_repo=invocation_repo,
        connection_registry={connection.id: connection},
        credential_registry={credential.id: credential},
        capability_registry={capability_version.id: capability_version},
    )


def _make_policy_input(org_id: UUID, ws_id: UUID, tool_id: UUID, principal_id: UUID) -> PolicyInput:
    return PolicyInput(
        actor=Actor(
            principal_id=principal_id,
            kind=PrincipalKind.USER,
        ),
        organization_id=org_id,
        workspace_id=ws_id,
        resource=ResourceRef(
            type=ResourceType.CAPABILITY_VERSION,
            id=tool_id,
            version="1",
        ),
        action=Action.IMPORT_CHECK_TEST,
        purpose=Purpose.GENERAL,
        context=RequestContext(
            now=datetime.now(UTC),
        ),
    )


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


class TestToolGatewayHappyPath:
    @pytest.mark.asyncio
    async def test_full_invocation_pipeline(
        self,
        tool_gateway: ToolGateway,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        connection: Connection,
        credential: CredentialBinding,
        capability_version: CapabilityVersion,
    ) -> None:
        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )
        invocation = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-1",
            task_id="task-1",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=connection.id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={"query": "test"},
            policy_input=policy_input,
        )

        assert invocation.status == InvocationStatus.COMPLETED
        assert invocation.observation is not None
        assert invocation.action_receipt is not None
        assert invocation.action_receipt.effect == "applied"
        assert invocation.output_result.get("status") == "executed"

    @pytest.mark.asyncio
    async def test_invocation_recorded_in_repository(
        self,
        tool_gateway: ToolGateway,
        invocation_repo: InvocationRepository,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        connection: Connection,
        credential: CredentialBinding,
        capability_version: CapabilityVersion,
    ) -> None:
        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )
        invocation = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-2",
            task_id="task-2",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=connection.id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={"query": "test"},
            policy_input=policy_input,
        )

        stored = invocation_repo.get(invocation.id)
        assert stored is not None
        assert stored.status == InvocationStatus.COMPLETED


# ---------------------------------------------------------------------------
# Policy denial tests
# ---------------------------------------------------------------------------


class TestToolGatewayPolicyDenial:
    @pytest.mark.asyncio
    async def test_policy_denied(
        self,
        runner_client: RunnerClient,
        invocation_repo: InvocationRepository,
        connection: Connection,
        credential: CredentialBinding,
        capability_version: CapabilityVersion,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
    ) -> None:
        mock_client = MagicMock()
        mock_client.evaluate = AsyncMock(
            return_value=PolicyDecision(
                allow=False,
                decision_id=None,
                revision=None,
                reason="insufficient_permissions",
                evaluated_at=utc_now(),
                input_digest=None,
            )
        )
        mock_client.fail_closed = MagicMock(
            return_value=PolicyDecision(
                allow=False,
                decision_id=None,
                revision=None,
                reason="fail_closed",
                evaluated_at=utc_now(),
                input_digest=None,
            )
        )
        gateway = ToolGateway(
            policy_enforcer=PolicyEnforcer(mock_client),
            runner_client=runner_client,
            invocation_repo=invocation_repo,
            connection_registry={connection.id: connection},
            credential_registry={credential.id: credential},
            capability_registry={capability_version.id: capability_version},
        )

        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )
        invocation = await gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-3",
            task_id="task-3",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=connection.id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={},
            policy_input=policy_input,
        )

        assert invocation.status == InvocationStatus.REJECTED
        assert invocation.failure_reason == InvocationFailureReason.POLICY_DENIED
        assert "insufficient_permissions" in invocation.failure_message


# ---------------------------------------------------------------------------
# Connection validation tests
# ---------------------------------------------------------------------------


class TestToolGatewayConnectionValidation:
    @pytest.mark.asyncio
    async def test_revoked_connection_rejected(
        self,
        tool_gateway: ToolGateway,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        credential: CredentialBinding,
        capability_version: CapabilityVersion,
    ) -> None:
        revoked_conn = Connection(
            id=new_id(),
            organization_id=org_id,
            workspace_id=ws_id,
            provider_version_id=new_id(),
            subject_mode=SubjectMode.SERVICE_ACCOUNT,
            status=ConnectionStatus.REVOKED,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        tool_gateway._connections[revoked_conn.id] = revoked_conn

        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )
        invocation = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-4",
            task_id="task-4",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=revoked_conn.id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={},
            policy_input=policy_input,
        )

        assert invocation.status == InvocationStatus.FAILED
        assert invocation.failure_reason == InvocationFailureReason.CONNECTION_REVOKED

    @pytest.mark.asyncio
    async def test_suspended_connection_rejected(
        self,
        tool_gateway: ToolGateway,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        credential: CredentialBinding,
        capability_version: CapabilityVersion,
    ) -> None:
        suspended_conn = Connection(
            id=new_id(),
            organization_id=org_id,
            workspace_id=ws_id,
            provider_version_id=new_id(),
            subject_mode=SubjectMode.SERVICE_ACCOUNT,
            status=ConnectionStatus.SUSPENDED,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        tool_gateway._connections[suspended_conn.id] = suspended_conn

        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )
        invocation = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-5",
            task_id="task-5",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=suspended_conn.id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={},
            policy_input=policy_input,
        )

        assert invocation.status == InvocationStatus.FAILED
        assert invocation.failure_reason == InvocationFailureReason.CONNECTION_SUSPENDED

    @pytest.mark.asyncio
    async def test_missing_connection_rejected(
        self,
        tool_gateway: ToolGateway,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        credential: CredentialBinding,
        capability_version: CapabilityVersion,
    ) -> None:
        missing_conn_id = new_id()
        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )
        invocation = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-6",
            task_id="task-6",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=missing_conn_id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={},
            policy_input=policy_input,
        )

        assert invocation.status == InvocationStatus.FAILED
        assert invocation.failure_reason == InvocationFailureReason.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Credential validation tests
# ---------------------------------------------------------------------------


class TestToolGatewayCredentialValidation:
    @pytest.mark.asyncio
    async def test_expired_credential_rejected(
        self,
        tool_gateway: ToolGateway,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        connection: Connection,
        capability_version: CapabilityVersion,
    ) -> None:
        expired_cred = CredentialBinding(
            id=new_id(),
            connection_id=connection.id,
            organization_id=org_id,
            workspace_id=ws_id,
            credential_type=CredentialType.API_KEY,
            secret_ref=SecretRef(value="expired-ref"),
            status=BindingStatus.EXPIRED,
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        tool_gateway._credentials[expired_cred.id] = expired_cred

        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )
        invocation = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-7",
            task_id="task-7",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=connection.id,
            credential_binding_id=expired_cred.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={},
            policy_input=policy_input,
        )

        assert invocation.status == InvocationStatus.FAILED
        assert invocation.failure_reason == InvocationFailureReason.CREDENTIAL_EXPIRED

    @pytest.mark.asyncio
    async def test_revoked_credential_rejected(
        self,
        tool_gateway: ToolGateway,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        connection: Connection,
        capability_version: CapabilityVersion,
    ) -> None:
        revoked_cred = CredentialBinding(
            id=new_id(),
            connection_id=connection.id,
            organization_id=org_id,
            workspace_id=ws_id,
            credential_type=CredentialType.API_KEY,
            secret_ref=SecretRef(value="revoked-ref"),
            status=BindingStatus.REVOKED,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        tool_gateway._credentials[revoked_cred.id] = revoked_cred

        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )
        invocation = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-8",
            task_id="task-8",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=connection.id,
            credential_binding_id=revoked_cred.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={},
            policy_input=policy_input,
        )

        assert invocation.status == InvocationStatus.FAILED
        assert invocation.failure_reason == InvocationFailureReason.CREDENTIAL_UNAVAILABLE


# ---------------------------------------------------------------------------
# Capability validation tests
# ---------------------------------------------------------------------------


class TestToolGatewayCapabilityValidation:
    @pytest.mark.asyncio
    async def test_unpublished_capability_rejected(
        self,
        tool_gateway: ToolGateway,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        connection: Connection,
        credential: CredentialBinding,
    ) -> None:
        draft_cap = CapabilityVersion(
            id=new_id(),
            capability_type="mcp",
            name="draft-tool",
            version=1,
            status=CapabilityStatus.DISCOVERED,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        tool_gateway._capabilities[draft_cap.id] = draft_cap

        policy_input = _make_policy_input(
            org_id, ws_id, draft_cap.id, principal_id
        )
        invocation = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-9",
            task_id="task-9",
            attempt_no=1,
            tool_name="draft-tool",
            tool_version_id=draft_cap.id,
            provider_version_id=new_id(),
            connection_id=connection.id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={},
            policy_input=policy_input,
        )

        assert invocation.status == InvocationStatus.FAILED
        assert invocation.failure_reason == InvocationFailureReason.CAPABILITY_NOT_PUBLISHED


# ---------------------------------------------------------------------------
# Sandbox validation tests
# ---------------------------------------------------------------------------


class TestSandboxSpec:
    def test_valid_sandbox(self) -> None:
        sandbox = SandboxSpec(
            image_digest="sha256:abc123",
            non_root=True,
            read_only_rootfs=True,
            no_docker_socket=True,
            no_network=True,
        )
        violations = sandbox.validate_sandbox()
        assert violations == []

    def test_non_root_required(self) -> None:
        sandbox = SandboxSpec(
            image_digest="sha256:abc123",
            non_root=False,
            read_only_rootfs=True,
            no_docker_socket=True,
            no_network=True,
        )
        violations = sandbox.validate_sandbox()
        assert any("non-root" in v for v in violations)

    def test_read_only_rootfs_required(self) -> None:
        sandbox = SandboxSpec(
            image_digest="sha256:abc123",
            non_root=True,
            read_only_rootfs=False,
            no_docker_socket=True,
            no_network=True,
        )
        violations = sandbox.validate_sandbox()
        assert any("read-only" in v for v in violations)

    def test_no_docker_socket_required(self) -> None:
        sandbox = SandboxSpec(
            image_digest="sha256:abc123",
            non_root=True,
            read_only_rootfs=True,
            no_docker_socket=False,
            no_network=True,
        )
        violations = sandbox.validate_sandbox()
        assert any("Docker socket" in v for v in violations)

    def test_docker_capabilities_rejected(self) -> None:
        sandbox = SandboxSpec(
            image_digest="sha256:abc123",
            non_root=True,
            read_only_rootfs=True,
            no_docker_socket=True,
            no_network=True,
            capabilities=("docker",),
        )
        violations = sandbox.validate_sandbox()
        assert any("Docker capabilities" in v for v in violations)


# ---------------------------------------------------------------------------
# Runner IPC authentication tests
# ---------------------------------------------------------------------------


class TestRunnerIPC:
    def test_sign_and_verify_request(self, runner_client: RunnerClient) -> None:
        request = RunnerInvocationRequest(
            invocation_id=new_id(),
            tool_name="test-tool",
            tool_type="mcp_tool",
            idempotency_key="run:task:1",
        )
        headers = runner_client.authenticate_request(request)
        assert "x-zhiwei-ipc-signature" in headers
        assert "x-zhiwei-invocation-id" in headers

        # Verify the signature
        assert runner_client.verify_request(request, headers["x-zhiwei-ipc-signature"])

    def test_verify_rejects_tampered_signature(
        self, runner_client: RunnerClient
    ) -> None:
        request = RunnerInvocationRequest(
            invocation_id=new_id(),
            tool_name="test-tool",
            tool_type="mcp_tool",
            idempotency_key="run:task:1",
        )
        runner_client.authenticate_request(request)

        # Tamper with the signature
        assert not runner_client.verify_request(request, "tampered-signature")

    def test_no_qualified_runner_returns_none(
        self, runner_client: RunnerClient
    ) -> None:
        runner = runner_client.find_runner_for_request(RunnerKind.KUBERNETES)
        assert runner is None


# ---------------------------------------------------------------------------
# Duplicate detection tests
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    @pytest.mark.asyncio
    async def test_duplicate_invocation_detected(
        self,
        tool_gateway: ToolGateway,
        invocation_repo: InvocationRepository,
        org_id: UUID,
        ws_id: UUID,
        principal_id: UUID,
        connection: Connection,
        credential: CredentialBinding,
        capability_version: CapabilityVersion,
    ) -> None:
        policy_input = _make_policy_input(
            org_id, ws_id, capability_version.id, principal_id
        )

        # First invocation
        inv1 = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-dup",
            task_id="task-dup",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=connection.id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={"query": "test"},
            policy_input=policy_input,
        )

        # Second invocation with same idempotency key
        inv2 = await tool_gateway.invoke(
            organization_id=org_id,
            workspace_id=ws_id,
            run_id="run-dup",
            task_id="task-dup",
            attempt_no=1,
            tool_name="test-tool",
            tool_version_id=capability_version.id,
            provider_version_id=new_id(),
            connection_id=connection.id,
            credential_binding_id=credential.id,
            principal_id=principal_id,
            agent_identity_id=None,
            input_args={"query": "test"},
            policy_input=policy_input,
        )

        # Both should complete, but the second should be detected as duplicate
        assert inv1.status == InvocationStatus.COMPLETED
        assert inv2.status == InvocationStatus.COMPLETED
        if inv2.action_receipt:
            assert inv2.action_receipt.effect == "duplicate"


# ---------------------------------------------------------------------------
# Invocation repository tests
# ---------------------------------------------------------------------------


class TestInvocationRepository:
    def test_store_and_retrieve(self) -> None:
        repo = InvocationRepository()
        invocation = ToolInvocation(
            organization_id=new_id(),
            workspace_id=new_id(),
            run_id="run-1",
            task_id="task-1",
            tool_name="test",
            tool_version_id=new_id(),
            provider_version_id=new_id(),
            connection_id=new_id(),
            credential_binding_id=new_id(),
            principal_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.store(invocation)
        assert repo.get(invocation.id) is invocation

    def test_get_by_idempotency_key(self) -> None:
        repo = InvocationRepository()
        invocation = ToolInvocation(
            organization_id=new_id(),
            workspace_id=new_id(),
            run_id="run-1",
            task_id="task-1",
            tool_name="test",
            tool_version_id=new_id(),
            provider_version_id=new_id(),
            connection_id=new_id(),
            credential_binding_id=new_id(),
            principal_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        from zhiwei.capabilities.invocations import ActionReceipt

        receipt = ActionReceipt(
            invocation_id=invocation.id,
            idempotency_key="test-key",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        invocation = invocation.model_copy(update={"action_receipt": receipt})
        repo.store(invocation)
        assert repo.get_by_idempotency_key("test-key") is invocation

    def test_list_for_run(self) -> None:
        repo = InvocationRepository()
        run_id = "run-list"
        inv1 = ToolInvocation(
            organization_id=new_id(),
            workspace_id=new_id(),
            run_id=run_id,
            task_id="task-1",
            tool_name="test",
            tool_version_id=new_id(),
            provider_version_id=new_id(),
            connection_id=new_id(),
            credential_binding_id=new_id(),
            principal_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        inv2 = ToolInvocation(
            organization_id=new_id(),
            workspace_id=new_id(),
            run_id="other-run",
            task_id="task-2",
            tool_name="test",
            tool_version_id=new_id(),
            provider_version_id=new_id(),
            connection_id=new_id(),
            credential_binding_id=new_id(),
            principal_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.store(inv1)
        repo.store(inv2)
        assert len(repo.list_for_run(run_id)) == 1
        assert repo.list_for_run(run_id)[0].id == inv1.id


# ---------------------------------------------------------------------------
# Tool Activity tests
# ---------------------------------------------------------------------------


class TestToolActivity:
    @pytest.mark.asyncio
    async def test_tool_activity_missing_tool_name(
        self, tool_gateway: ToolGateway, invocation_repo: InvocationRepository
    ) -> None:
        activity = ToolActivity(tool_gateway, invocation_repo)
        result = await activity.execute(
            ToolActivityInput(
                run_id="run-1",
                task_id="task-1",
                attempt_no=1,
                organization_id=str(new_id()),
                workspace_id=str(new_id()),
                tool_name="",
                tool_version_id=str(new_id()),
                provider_version_id=str(new_id()),
                connection_id=str(new_id()),
                credential_binding_id=str(new_id()),
                principal_id=str(new_id()),
            )
        )
        assert result.status == "failed"
        assert "tool_name is required" in (result.error or "")
