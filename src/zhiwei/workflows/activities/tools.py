"""S4 Tool Activity for Temporal: tool execution through Activity boundary.

Per S4 spec §5/§7:
- Tool invocations go through Tool Gateway Activity
- Intent/result/receipt submitted through canonical events
- No live runners called during testing

事实源：S4 spec §5、§7。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from zhiwei.capabilities.invocations import (
    InvocationRepository,
    InvocationStatus,
    ToolInvocation,
)
from zhiwei.capabilities.tool_gateway import ToolGateway
from zhiwei.contracts.identifiers import new_id

logger = logging.getLogger(__name__)


@dataclass
class ToolActivityInput:
    """Input for a tool activity execution.

    Carries all necessary context for the Tool Gateway to execute
    a tool invocation through the full pipeline.
    """

    run_id: str
    task_id: str
    attempt_no: int
    organization_id: str
    workspace_id: str
    tool_name: str
    tool_version_id: str
    provider_version_id: str
    connection_id: str
    credential_binding_id: str
    principal_id: str
    agent_identity_id: str | None = None
    input_args: dict[str, Any] = field(default_factory=dict)
    policy_input: dict[str, Any] = field(default_factory=dict)
    actor_ref: str = "agent-runtime:worker"


@dataclass
class ToolActivityOutput:
    """Output from a tool activity execution.

    Carries the invocation result, receipt, and status for the workflow
    to interpret and record as canonical events.
    """

    invocation_id: str
    task_id: str
    status: str  # completed | failed | effect_unknown
    output_values: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    receipt_effect: str | None = None  # applied | duplicate | effect_unknown
    receipt_data: dict[str, Any] = field(default_factory=dict)


class ToolActivity:
    """Temporal activity boundary for tool execution.

    Orchestrates:
    1. Tool Gateway pipeline execution
    2. Invocation result capture
    3. Receipt generation
    4. Canonical event context
    """

    def __init__(
        self,
        gateway: ToolGateway,
        invocation_repo: InvocationRepository,
    ) -> None:
        self._gateway = gateway
        self._invocation_repo = invocation_repo

    async def execute(self, input: ToolActivityInput) -> ToolActivityOutput:
        """Execute a tool activity through the Tool Gateway.

        Args:
            input: Tool activity input with all invocation context.

        Returns:
            ToolActivityOutput with invocation result and receipt.
        """
        try:
            from zhiwei.policy.input import PolicyInput

            # Validate required fields before proceeding
            if not input.tool_name:
                return ToolActivityOutput(
                    invocation_id=str(new_id()),
                    task_id=input.task_id,
                    status="failed",
                    error="tool_name is required",
                )

            if not input.tool_version_id:
                return ToolActivityOutput(
                    invocation_id=str(new_id()),
                    task_id=input.task_id,
                    status="failed",
                    error="tool_version_id is required",
                )

            if not input.connection_id:
                return ToolActivityOutput(
                    invocation_id=str(new_id()),
                    task_id=input.task_id,
                    status="failed",
                    error="connection_id is required",
                )

            if not input.credential_binding_id:
                return ToolActivityOutput(
                    invocation_id=str(new_id()),
                    task_id=input.task_id,
                    status="failed",
                    error="credential_binding_id is required",
                )

            if not input.principal_id:
                return ToolActivityOutput(
                    invocation_id=str(new_id()),
                    task_id=input.task_id,
                    status="failed",
                    error="principal_id is required",
                )

            # Build policy input from the activity input
            if input.policy_input:
                policy_input = PolicyInput.model_validate(input.policy_input)
            else:
                # Build a minimal policy input for testing
                from datetime import UTC, datetime

                from zhiwei.identity.domain import PrincipalKind
                from zhiwei.policy.input import (
                    Actor,
                    RequestContext,
                    ResourceRef,
                )
                from zhiwei.policy.roles import (
                    Action,
                    Purpose,
                    ResourceType,
                )

                policy_input = PolicyInput(
                    actor=Actor(
                        principal_id=UUID(input.principal_id),
                        kind=PrincipalKind.USER,
                    ),
                    organization_id=UUID(input.organization_id),
                    workspace_id=UUID(input.workspace_id),
                    resource=ResourceRef(
                        type=ResourceType.CAPABILITY_VERSION,
                        id=UUID(input.tool_version_id),
                        version="1",
                    ),
                    action=Action.IMPORT_CHECK_TEST,
                    purpose=Purpose.GENERAL,
                    context=RequestContext(
                        now=datetime.now(UTC),
                    ),
                )

            invocation = await self._gateway.invoke(
                organization_id=UUID(input.organization_id),
                workspace_id=UUID(input.workspace_id),
                run_id=input.run_id,
                task_id=input.task_id,
                attempt_no=input.attempt_no,
                tool_name=input.tool_name,
                tool_version_id=UUID(input.tool_version_id),
                provider_version_id=UUID(input.provider_version_id),
                connection_id=UUID(input.connection_id),
                credential_binding_id=UUID(input.credential_binding_id),
                principal_id=UUID(input.principal_id),
                agent_identity_id=(
                    UUID(input.agent_identity_id) if input.agent_identity_id else None
                ),
                input_args=input.input_args,
                policy_input=policy_input,
            )

            return self._build_output(invocation)

        except Exception as exc:
            logger.exception("Tool activity execution failed")
            return ToolActivityOutput(
                invocation_id=str(new_id()),
                task_id=input.task_id,
                status="failed",
                error=str(exc),
            )

    def _build_output(self, invocation: ToolInvocation) -> ToolActivityOutput:
        """Build activity output from invocation result."""
        status_map = {
            InvocationStatus.COMPLETED: "completed",
            InvocationStatus.FAILED: "failed",
            InvocationStatus.EFFECT_UNKNOWN: "effect_unknown",
            InvocationStatus.REJECTED: "failed",
        }

        status = status_map.get(invocation.status, "failed")
        error = invocation.failure_message if invocation.failure_message else None

        receipt_effect = None
        receipt_data: dict[str, Any] = {}
        if invocation.action_receipt:
            receipt_effect = invocation.action_receipt.effect
            receipt_data = invocation.action_receipt.receipt_data

        return ToolActivityOutput(
            invocation_id=str(invocation.id),
            task_id=invocation.task_id,
            status=status,
            output_values=invocation.output_result,
            error=error,
            receipt_effect=receipt_effect,
            receipt_data=receipt_data,
        )
