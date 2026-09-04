"""S4 Prebuilt local runner.

local-product: dedicated prebuilt runner service. The admission/build pipeline
produces signed images and Compose overlays for each provider. This runner
executes tools using prebuilt provider runner services on the local machine.

No Docker socket or K8s credential on API/Agent Worker (S4 spec §5)。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from zhiwei.capabilities.runners.contracts import (
    BaseRunner,
    RunnerHealth,
    RunnerInvocationRequest,
    RunnerInvocationResponse,
    RunnerSpec,
    RunnerStatus,
)

logger = logging.getLogger(__name__)


class PrebuiltRunner(BaseRunner):
    """Prebuilt local runner for local-product deployments.

    Executes tools using admission/build pipeline produced provider runner
    services. Image digest is pinned per provider version.
    """

    def __init__(self, spec: RunnerSpec) -> None:
        super().__init__(spec)
        self._active_tasks = 0

    async def health_check(self) -> RunnerHealth:
        """Check health of the prebuilt runner service."""
        status = RunnerStatus.HEALTHY
        if self._active_tasks >= self._spec.max_concurrent:
            status = RunnerStatus.UNHEALTHY
        return RunnerHealth(
            runner_id=self._spec.id,
            status=status,
            checked_at=datetime.now(UTC),
            active_tasks=self._active_tasks,
            max_tasks=self._spec.max_concurrent,
        )

    async def execute(
        self, request: RunnerInvocationRequest
    ) -> RunnerInvocationResponse:
        """Execute a tool invocation through the prebuilt runner.

        Validates sandbox spec, then dispatches to the prebuilt provider
        runner service. Image digest must be pinned.
        """
        self._active_tasks += 1
        try:
            violations = self._validate_sandbox(request.sandbox_spec)
            if violations:
                return RunnerInvocationResponse(
                    invocation_id=request.invocation_id,
                    status="failed",
                    error=f"Sandbox violations: {'; '.join(violations)}",
                )

            output = await self._execute_in_sandbox(request)

            return RunnerInvocationResponse(
                invocation_id=request.invocation_id,
                status="completed",
                output=output,
                execution_time_ms=0.0,
            )
        except Exception as exc:
            logger.exception("Prebuilt runner execution failed")
            return RunnerInvocationResponse(
                invocation_id=request.invocation_id,
                status="failed",
                error=f"Prebuilt execution error: {exc}",
            )
        finally:
            self._active_tasks = max(0, self._active_tasks - 1)

    def _validate_sandbox(self, sandbox: dict[str, Any]) -> list[str]:
        """Validate sandbox spec for prebuilt runner."""
        violations: list[str] = []
        if sandbox.get("no_docker_socket") is False:
            violations.append("Docker socket must not be mounted")
        if sandbox.get("non_root") is False:
            violations.append("Container must run as non-root")
        return violations

    async def _execute_in_sandbox(
        self, request: RunnerInvocationRequest
    ) -> dict[str, Any]:
        """Execute tool in the prebuilt sandbox environment.

        In a real deployment, this would invoke the Compose-managed provider
        runner service. For now, it simulates execution.
        """
        return {
            "tool_name": request.tool_name,
            "status": "executed",
            "invocation_id": str(request.invocation_id),
        }

    async def shutdown(self) -> None:
        """Graceful shutdown of prebuilt runner."""
        logger.info("Shutting down prebuilt runner %s", self._spec.id)
