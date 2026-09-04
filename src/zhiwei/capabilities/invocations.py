"""S4 Invocation domain types.

Tool invocation lifecycle: intent → policy → approval → credential → sandbox →
validate/redact → Observation/ActionReceipt。

事实源：S4 spec §5 (Connection and execution)。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc


class InvocationStatus(StrEnum):
    """Tool invocation lifecycle states."""

    PENDING = "pending"
    POLICY_CHECKED = "policy_checked"
    APPROVED = "approved"
    CREDENTIALS_RESOLVED = "credentials_resolved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    EFFECT_UNKNOWN = "effect_unknown"


class InvocationFailureReason(StrEnum):
    """Typed failure reasons for tool invocations."""

    POLICY_DENIED = "policy_denied"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_EXPIRED = "approval_expired"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    CREDENTIAL_EXPIRED = "credential_expired"
    CONNECTION_REVOKED = "connection_revoked"
    CONNECTION_SUSPENDED = "connection_suspended"
    CAPABILITY_NOT_PUBLISHED = "capability_not_published"
    CAPABILITY_REVOKED = "capability_revoked"
    EXECUTION_BACKEND_UNAVAILABLE = "execution_backend_unavailable"
    RUNNER_UNAVAILABLE = "runner_unavailable"
    RUNNER_TIMEOUT = "runner_timeout"
    SANDBOX_VIOLATION = "sandbox_violation"
    INPUT_VALIDATION_FAILED = "input_validation_failed"
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    REDACTION_REQUIRED = "redaction_required"
    EFFECT_UNKNOWN = "effect_unknown"
    INTERNAL_ERROR = "internal_error"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class SandboxSpec(_FrozenModel):
    """OCI sandbox specification for isolated execution.

    S4 spec §5: OCI digest pinned, non-root/read-only/no Docker socket/
    resource cap/default no-network.
    """

    image_digest: str = Field(min_length=1, description="OCI image digest (pinned)")
    non_root: bool = True
    read_only_rootfs: bool = True
    no_docker_socket: bool = True
    no_network: bool = True
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    security_context: dict[str, Any] = Field(default_factory=dict)

    def validate_sandbox(self) -> list[str]:
        """Validate sandbox spec meets security requirements.

        Returns list of violation messages; empty = compliant.
        """
        violations: list[str] = []
        if not self.non_root:
            violations.append("container must run as non-root")
        if not self.read_only_rootfs:
            violations.append("rootfs must be read-only")
        if not self.no_docker_socket:
            violations.append("Docker socket must not be mounted")
        if not self.image_digest:
            violations.append("image digest must be pinned")
        if "docker" in self.capabilities:
            violations.append("Docker capabilities are not allowed")
        return violations


class Observation(_FrozenModel):
    """Observation from a tool invocation — structured side-channel data."""

    id: UUID = Field(default_factory=new_id)
    invocation_id: UUID
    source_tool: str
    observation_type: str = "tool_output"
    data: dict[str, Any] = Field(default_factory=dict)
    redacted_fields: tuple[str, ...] = ()
    classification: str = "PUBLIC"
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value


class ActionReceipt(_FrozenModel):
    """ActionReceipt for write/exec invocations — idempotency and effect tracking.

    S2 duplicate/effect_unknown semantics apply here.
    """

    id: UUID = Field(default_factory=new_id)
    invocation_id: UUID
    idempotency_key: str = Field(min_length=1)
    effect: str = "applied"  # applied | duplicate | effect_unknown
    receipt_data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value


class ToolInvocation(_FrozenModel):
    """A single tool invocation request through the Tool Gateway.

    Tracks the full lifecycle from intent through execution to receipt.
    """

    id: UUID = Field(default_factory=new_id)
    organization_id: UUID
    workspace_id: UUID
    run_id: str
    task_id: str
    attempt_no: int = 1
    tool_name: str = Field(min_length=1)
    tool_version_id: UUID
    provider_version_id: UUID
    connection_id: UUID
    credential_binding_id: UUID
    principal_id: UUID
    agent_identity_id: UUID | None = None
    status: InvocationStatus = InvocationStatus.PENDING
    input_args: dict[str, Any] = Field(default_factory=dict)
    input_digest: str = ""
    output_result: dict[str, Any] = Field(default_factory=dict)
    observation: Observation | None = None
    action_receipt: ActionReceipt | None = None
    failure_reason: InvocationFailureReason | None = None
    failure_message: str = ""
    sandbox_spec: SandboxSpec | None = None
    runner_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    def compute_input_digest(self) -> str:
        """Compute SHA-256 digest of canonical JSON of invocation input."""
        return digest_bytes(
            canonical_json(
                {
                    "tool_name": self.tool_name,
                    "input_args": self.input_args,
                }
            )
        )


class InvocationRepository:
    """In-memory repository for tool invocations (test-friendly)."""

    def __init__(self) -> None:
        self._invocations: dict[UUID, ToolInvocation] = {}
        self._by_idempotency: dict[str, UUID] = {}

    def store(self, invocation: ToolInvocation) -> None:
        self._invocations[invocation.id] = invocation
        if invocation.action_receipt:
            key = invocation.action_receipt.idempotency_key
            self._by_idempotency[key] = invocation.id

    def get(self, invocation_id: UUID) -> ToolInvocation | None:
        return self._invocations.get(invocation_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> ToolInvocation | None:
        inv_id = self._by_idempotency.get(idempotency_key)
        if inv_id is None:
            return None
        return self._invocations.get(inv_id)

    def list_for_run(self, run_id: str) -> list[ToolInvocation]:
        return [i for i in self._invocations.values() if i.run_id == run_id]

    def list_for_connection(self, connection_id: UUID) -> list[ToolInvocation]:
        return [i for i in self._invocations.values() if i.connection_id == connection_id]
