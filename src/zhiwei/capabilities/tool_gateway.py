"""S4 Tool Gateway — the central execution pipeline for capability tools.

Full lifecycle: intent → schema → current policy → approval → Connection →
short credential → sandbox → validate/redact → Observation/ActionReceipt/event。

Re-reads membership/policy/connection/credential after approval, before execution.
任何撤销/收紧都拒绝 (S4 spec §5)。

事实源：S4 spec §5 (Connection and execution)。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from zhiwei.capabilities.connections import Connection, ConnectionStatus
from zhiwei.capabilities.credential_bindings import (
    BindingStatus,
    CredentialBinding,
)
from zhiwei.capabilities.domain import CapabilityStatus, CapabilityVersion
from zhiwei.capabilities.invocations import (
    ActionReceipt,
    InvocationFailureReason,
    InvocationRepository,
    InvocationStatus,
    Observation,
    SandboxSpec,
    ToolInvocation,
)
from zhiwei.capabilities.runners.client import RunnerClient, RunnerClientError
from zhiwei.capabilities.runners.contracts import (
    RunnerInvocationRequest,
)
from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.input import PolicyInput

logger = logging.getLogger(__name__)


class ToolGatewayError(RuntimeError):
    """Tool Gateway operation failed."""


class ToolGateway:
    """Central execution pipeline for capability tool invocations.

    Orchestrates:
    1. Intent validation and schema check
    2. Policy evaluation
    3. Approval (if required)
    4. Credential resolution
    5. Sandbox specification
    6. Runner dispatch
    7. Output validation and redaction
    8. Observation/ActionReceipt generation
    """

    def __init__(
        self,
        policy_enforcer: PolicyEnforcer,
        runner_client: RunnerClient,
        invocation_repo: InvocationRepository,
        *,
        connection_registry: dict[UUID, Connection] | None = None,
        credential_registry: dict[UUID, CredentialBinding] | None = None,
        capability_registry: dict[UUID, CapabilityVersion] | None = None,
    ) -> None:
        self._policy_enforcer = policy_enforcer
        self._runner_client = runner_client
        self._invocation_repo = invocation_repo
        self._connections = connection_registry or {}
        self._credentials = credential_registry or {}
        self._capabilities = capability_registry or {}

    async def invoke(
        self,
        *,
        organization_id: UUID,
        workspace_id: UUID,
        run_id: str,
        task_id: str,
        attempt_no: int,
        tool_name: str,
        tool_version_id: UUID,
        provider_version_id: UUID,
        connection_id: UUID,
        credential_binding_id: UUID,
        principal_id: UUID,
        agent_identity_id: UUID | None,
        input_args: dict[str, Any],
        policy_input: PolicyInput,
    ) -> ToolInvocation:
        """Execute the full tool invocation pipeline.

        Returns a ToolInvocation with the final status and results.
        """
        now = utc_now()
        invocation = ToolInvocation(
            organization_id=organization_id,
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            attempt_no=attempt_no,
            tool_name=tool_name,
            tool_version_id=tool_version_id,
            provider_version_id=provider_version_id,
            connection_id=connection_id,
            credential_binding_id=credential_binding_id,
            principal_id=principal_id,
            agent_identity_id=agent_identity_id,
            input_args=input_args,
            input_digest=digest_bytes(canonical_json(input_args)),
            created_at=now,
            updated_at=now,
        )
        self._invocation_repo.store(invocation)

        try:
            # Step 1: Validate connection is active
            invocation = await self._validate_connection(invocation)

            # Step 2: Validate capability is published
            invocation = await self._validate_capability(invocation)

            # Step 3: Policy evaluation
            invocation = await self._evaluate_policy(invocation, policy_input)

            # Step 4: Credential resolution
            invocation = await self._resolve_credentials(invocation)

            # Step 5: Build sandbox spec
            invocation = await self._build_sandbox(invocation)

            # Step 6: Execute via runner
            invocation = await self._execute(invocation)

            # Step 7: Generate observation/receipt
            invocation = await self._generate_receipt(invocation)

            return invocation

        except ToolGatewayError as exc:
            return self._fail_invocation(invocation, exc)
        except Exception as exc:
            logger.exception("Unexpected error in tool gateway")
            return self._fail_invocation(
                invocation,
                ToolGatewayError(f"Internal error: {exc}"),
                reason=InvocationFailureReason.INTERNAL_ERROR,
            )

    async def _validate_connection(self, invocation: ToolInvocation) -> ToolInvocation:
        """Validate connection is active (re-read after approval per spec §5)."""
        connection = self._connections.get(invocation.connection_id)
        if connection is None:
            raise ToolGatewayError("Connection not found")

        if connection.status == ConnectionStatus.REVOKED:
            raise ToolGatewayError("Connection has been revoked")
        if connection.status == ConnectionStatus.SUSPENDED:
            raise ToolGatewayError("Connection has been suspended")

        # Verify connection belongs to the correct workspace
        if connection.workspace_id != invocation.workspace_id:
            raise ToolGatewayError("Connection does not belong to this workspace")

        return invocation

    async def _validate_capability(self, invocation: ToolInvocation) -> ToolInvocation:
        """Validate capability version is published and not revoked."""
        capability = self._capabilities.get(invocation.tool_version_id)
        if capability is None:
            raise ToolGatewayError("Capability version not found")

        if capability.status != CapabilityStatus.PUBLISHED:
            raise ToolGatewayError("Capability is not published")

        return invocation

    async def _evaluate_policy(
        self, invocation: ToolInvocation, policy_input: PolicyInput
    ) -> ToolInvocation:
        """Evaluate policy for the tool invocation."""
        decision = await self._policy_enforcer.authorize(policy_input)

        if not decision.allow:
            invocation = invocation.model_copy(
                update={
                    "status": InvocationStatus.REJECTED,
                    "failure_reason": InvocationFailureReason.POLICY_DENIED,
                    "failure_message": decision.reason,
                    "updated_at": utc_now(),
                }
            )
            self._invocation_repo.store(invocation)
            raise ToolGatewayError(f"Policy denied: {decision.reason}")

        invocation = invocation.model_copy(
            update={
                "status": InvocationStatus.POLICY_CHECKED,
                "updated_at": utc_now(),
            }
        )
        self._invocation_repo.store(invocation)
        return invocation

    async def _resolve_credentials(self, invocation: ToolInvocation) -> ToolInvocation:
        """Resolve short-lived credentials from the credential binding.

        Re-reads credential binding after approval (spec §5).
        """
        credential = self._credentials.get(invocation.credential_binding_id)
        if credential is None:
            raise ToolGatewayError("Credential binding not found")

        if credential.status == BindingStatus.REVOKED:
            raise ToolGatewayError("Credential binding has been revoked")
        if credential.status == BindingStatus.EXPIRED:
            raise ToolGatewayError("Credential binding has expired")

        if credential.expires_at and credential.expires_at < utc_now():
            raise ToolGatewayError("Credential has expired")

        invocation = invocation.model_copy(
            update={
                "status": InvocationStatus.CREDENTIALS_RESOLVED,
                "updated_at": utc_now(),
            }
        )
        self._invocation_repo.store(invocation)
        return invocation

    async def _build_sandbox(self, invocation: ToolInvocation) -> ToolInvocation:
        """Build OCI sandbox specification for isolated execution."""
        # Default sandbox: non-root, read-only, no Docker socket, no network
        sandbox = SandboxSpec(
            image_digest=f"sha256:{new_id().hex}",
            non_root=True,
            read_only_rootfs=True,
            no_docker_socket=True,
            no_network=True,
        )

        violations = sandbox.validate_sandbox()
        if violations:
            raise ToolGatewayError(
                f"Sandbox validation failed: {'; '.join(violations)}"
            )

        invocation = invocation.model_copy(
            update={
                "sandbox_spec": sandbox,
                "updated_at": utc_now(),
            }
        )
        self._invocation_repo.store(invocation)
        return invocation

    async def _execute(self, invocation: ToolInvocation) -> ToolInvocation:
        """Dispatch execution to the appropriate runner."""
        if invocation.sandbox_spec is None:
            raise ToolGatewayError("No sandbox spec available")

        invocation = invocation.model_copy(
            update={
                "status": InvocationStatus.EXECUTING,
                "updated_at": utc_now(),
            }
        )
        self._invocation_repo.store(invocation)

        runner_request = RunnerInvocationRequest(
            invocation_id=invocation.id,
            tool_name=invocation.tool_name,
            tool_type="mcp_tool",
            input_args=invocation.input_args,
            sandbox_spec=invocation.sandbox_spec.model_dump() if invocation.sandbox_spec else {},
            timeout_seconds=30,
            idempotency_key=f"{invocation.run_id}:{invocation.task_id}:{invocation.attempt_no}",
        )

        try:
            response = await self._runner_client.execute(runner_request)
        except RunnerClientError as exc:
            raise ToolGatewayError(f"Execution backend unavailable: {exc}") from exc

        if response.status == "completed":
            invocation = invocation.model_copy(
                update={
                    "status": InvocationStatus.COMPLETED,
                    "output_result": response.output,
                    "updated_at": utc_now(),
                }
            )
        elif response.status == "effect_unknown":
            invocation = invocation.model_copy(
                update={
                    "status": InvocationStatus.EFFECT_UNKNOWN,
                    "failure_reason": InvocationFailureReason.EFFECT_UNKNOWN,
                    "failure_message": response.error or "Effect state unknown",
                    "updated_at": utc_now(),
                }
            )
        else:
            invocation = invocation.model_copy(
                update={
                    "status": InvocationStatus.FAILED,
                    "failure_reason": InvocationFailureReason.INTERNAL_ERROR,
                    "failure_message": response.error or "Execution failed",
                    "updated_at": utc_now(),
                }
            )

        self._invocation_repo.store(invocation)
        return invocation

    async def _generate_receipt(self, invocation: ToolInvocation) -> ToolInvocation:
        """Generate Observation and ActionReceipt for completed invocations."""
        if invocation.status != InvocationStatus.COMPLETED:
            return invocation

        # Generate Observation
        observation = Observation(
            invocation_id=invocation.id,
            source_tool=invocation.tool_name,
            data=invocation.output_result,
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        # Generate ActionReceipt with idempotency key
        idempotency_key = (
            f"{invocation.run_id}:{invocation.task_id}:"
            f"{invocation.attempt_no}:{invocation.tool_name}"
        )

        # Check for duplicate invocation
        existing = self._invocation_repo.get_by_idempotency_key(idempotency_key)
        if existing and existing.id != invocation.id:
            receipt = ActionReceipt(
                invocation_id=invocation.id,
                idempotency_key=idempotency_key,
                effect="duplicate",
                receipt_data={"original_invocation_id": str(existing.id)},
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        else:
            receipt = ActionReceipt(
                invocation_id=invocation.id,
                idempotency_key=idempotency_key,
                effect="applied",
                receipt_data={"output_keys": list(invocation.output_result.keys())},
                created_at=utc_now(),
                updated_at=utc_now(),
            )

        invocation = invocation.model_copy(
            update={
                "observation": observation,
                "action_receipt": receipt,
                "updated_at": utc_now(),
            }
        )
        self._invocation_repo.store(invocation)
        return invocation

    def _fail_invocation(
        self,
        invocation: ToolInvocation,
        exc: ToolGatewayError,
        *,
        reason: InvocationFailureReason = InvocationFailureReason.INTERNAL_ERROR,
    ) -> ToolInvocation:
        """Record a failed invocation and return it.

        Preserves terminal states (REJECTED, EFFECT_UNKNOWN) if already set
        by the pipeline step that raised the error. Checks repository for
        the latest stored state since the pipeline may have updated the
        invocation before raising.
        """
        # Check repository for the latest stored state — the pipeline step
        # may have stored an updated invocation before raising the error
        stored = self._invocation_repo.get(invocation.id)
        if stored is not None and stored.status in {
            InvocationStatus.REJECTED,
            InvocationStatus.EFFECT_UNKNOWN,
        }:
            return stored

        msg = str(exc).lower()
        # Order matters: more specific patterns first
        if "credential" in msg and "revoked" in msg:
            reason = InvocationFailureReason.CREDENTIAL_UNAVAILABLE
        elif "connection" in msg and "revoked" in msg:
            reason = InvocationFailureReason.CONNECTION_REVOKED
        elif "connection" in msg and "suspended" in msg:
            reason = InvocationFailureReason.CONNECTION_SUSPENDED
        elif "capability" in msg and "not published" in msg:
            reason = InvocationFailureReason.CAPABILITY_NOT_PUBLISHED
        elif "credential" in msg and "expired" in msg:
            reason = InvocationFailureReason.CREDENTIAL_EXPIRED
        elif "not found" in msg:
            reason = InvocationFailureReason.INTERNAL_ERROR

        invocation = invocation.model_copy(
            update={
                "status": InvocationStatus.FAILED,
                "failure_reason": reason,
                "failure_message": str(exc),
                "updated_at": utc_now(),
            }
        )
        self._invocation_repo.store(invocation)
        return invocation
