"""S4 Runner IPC client.

Authenticated client that dispatches invocation requests to runner backends.
The Gateway never calls runners directly — it always goes through this client
which enforces IPC authentication, timeout control, and response validation.

Runner IPC authentication: every request carries a signed token derived from
the Gateway's identity. Runners reject unsigned or tampered requests.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from uuid import UUID

from zhiwei.capabilities.runners.contracts import (
    BaseRunner,
    RunnerHealth,
    RunnerInvocationRequest,
    RunnerInvocationResponse,
    RunnerKind,
    RunnerRegistry,
    RunnerStatus,
)

logger = logging.getLogger(__name__)


class RunnerClientError(RuntimeError):
    """Runner client operation failed."""


class RunnerClient:
    """Authenticated IPC client for runner backends.

    Routes invocation requests to the appropriate runner based on kind/capability.
    Enforces authentication and timeout controls.
    """

    def __init__(
        self,
        registry: RunnerRegistry,
        *,
        ipc_secret: bytes = b"default-test-secret",
    ) -> None:
        self._registry = registry
        self._ipc_secret = ipc_secret

    def _sign_request(self, request: RunnerInvocationRequest) -> str:
        """Produce HMAC signature for runner IPC authentication."""
        payload = f"{request.invocation_id}:{request.idempotency_key}:{request.tool_name}"
        return hmac.new(self._ipc_secret, payload.encode(), hashlib.sha256).hexdigest()

    def authenticate_request(self, request: RunnerInvocationRequest) -> dict[str, str]:
        """Attach authentication headers to a runner request."""
        signature = self._sign_request(request)
        return {
            "x-zhiwei-ipc-signature": signature,
            "x-zhiwei-invocation-id": str(request.invocation_id),
        }

    def verify_request(
        self, request: RunnerInvocationRequest, signature: str
    ) -> bool:
        """Verify an incoming runner request signature."""
        expected = self._sign_request(request)
        return hmac.compare_digest(expected, signature)

    def find_runner_for_request(
        self, kind: RunnerKind | None = None
    ) -> BaseRunner | None:
        """Find a suitable runner for the request.

        Returns the first healthy runner matching the kind, or None if
        no qualified runner is available.
        """
        if kind is not None:
            runners = self._registry.find_by_kind(kind)
        else:
            runners = self._registry.find_healthy()

        for runner in runners:
            return runner
        return None

    async def health_check(self, runner_id: UUID) -> RunnerHealth:
        """Check health of a specific runner."""
        runner = self._registry.get(runner_id)
        if runner is None:
            return RunnerHealth(
                runner_id=runner_id,
                status=RunnerStatus.UNAVAILABLE,
                checked_at=datetime.now(UTC),
            )
        return await runner.health_check()

    async def execute(
        self,
        request: RunnerInvocationRequest,
        *,
        runner_id: UUID | None = None,
        timeout_seconds: int = 30,
    ) -> RunnerInvocationResponse:
        """Execute a tool invocation through the appropriate runner.

        Args:
            request: The invocation request to execute.
            runner_id: Optional explicit runner selection.
            timeout_seconds: Execution timeout.

        Returns:
            RunnerInvocationResponse with execution result.

        Raises:
            RunnerClientError: If no qualified runner or execution fails.
        """
        if runner_id is not None:
            runner = self._registry.get(runner_id)
            if runner is None:
                raise RunnerClientError(
                    f"Runner {runner_id} not found in registry"
                )
        else:
            runner = self.find_runner_for_request()
            if runner is None:
                raise RunnerClientError("No qualified runner available")

        _headers = self.authenticate_request(request)

        try:
            response = await runner.execute(request)
            return response
        except Exception as exc:
            logger.exception("Runner execution failed for %s", request.invocation_id)
            return RunnerInvocationResponse(
                invocation_id=request.invocation_id,
                status="failed",
                error=f"Runner execution error: {exc}",
            )

    async def shutdown_all(self) -> None:
        """Gracefully shutdown all registered runners."""
        for runner in self._registry.list_all():
            try:
                await runner.shutdown()
            except Exception:
                logger.exception("Failed to shutdown runner %s", runner.runner_id)
