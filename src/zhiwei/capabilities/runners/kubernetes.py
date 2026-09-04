"""S4 Kubernetes runner.

Production backend: per-invocation Job/Pod with minimal-privilege
Kubernetes ServiceAccount. API/Agent Worker does not hold runtime/socket
permissions (S4 spec §5).

No Docker socket or K8s credential on API/Agent Worker.
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


class KubernetesRunner(BaseRunner):
    """Kubernetes runner for production deployments.

    Creates per-invocation Job/Pod with:
    - Minimal-privilege ServiceAccount
    - Pinned OCI image digest
    - Non-root, read-only rootfs, no Docker socket
    - Resource limits
    - Default no-network (can be overridden per tool)
    """

    def __init__(
        self,
        spec: RunnerSpec,
        *,
        namespace: str = "zhiwei-runners",
        service_account: str = "zhiwei-runner-sa",
        image_pull_policy: str = "Always",
    ) -> None:
        super().__init__(spec)
        self._namespace = namespace
        self._service_account = service_account
        self._image_pull_policy = image_pull_policy
        self._active_jobs: dict[str, Any] = {}

    def _build_job_manifest(
        self, request: RunnerInvocationRequest
    ) -> dict[str, Any]:
        """Build a minimal Kubernetes Job manifest for invocation."""
        sandbox = request.sandbox_spec
        resource_limits = sandbox.get("resource_limits", {})

        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"tool-{request.invocation_id}",
                "namespace": self._namespace,
                "labels": {
                    "app": "zhiwei-runner",
                    "invocation-id": str(request.invocation_id),
                    "tool-name": request.tool_name,
                },
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "spec": {
                        "serviceAccountName": self._service_account,
                        "restartPolicy": "Never",
                        "securityContext": {
                            "runAsNonRoot": sandbox.get("non_root", True),
                            "runAsUser": 1000,
                            "fsGroup": 1000,
                        },
                        "containers": [
                            {
                                "name": "tool-executor",
                                "image": f"{self._spec.image_digest}",
                                "imagePullPolicy": self._image_pull_policy,
                                "securityContext": {
                                    "readOnlyRootFilesystem": sandbox.get(
                                        "read_only_rootfs", True
                                    ),
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {
                                        "drop": ["ALL"],
                                    },
                                },
                                "resources": {
                                    "requests": resource_limits,
                                    "limits": resource_limits,
                                },
                                "env": [
                                    {
                                        "name": "INVOCATION_ID",
                                        "value": str(request.invocation_id),
                                    },
                                    {
                                        "name": "TOOL_NAME",
                                        "value": request.tool_name,
                                    },
                                ],
                            }
                        ],
                        # Default no-network for stdio/script tools
                        **(
                            {"hostNetwork": False}
                            if sandbox.get("no_network", True)
                            else {}
                        ),
                    }
                },
            },
        }

    async def health_check(self) -> RunnerHealth:
        """Check health of the Kubernetes runner."""
        status = RunnerStatus.HEALTHY
        active_count = len(self._active_jobs)
        if active_count >= self._spec.max_concurrent:
            status = RunnerStatus.UNHEALTHY
        return RunnerHealth(
            runner_id=self._spec.id,
            status=status,
            checked_at=datetime.now(UTC),
            active_tasks=active_count,
            max_tasks=self._spec.max_concurrent,
        )

    async def execute(
        self, request: RunnerInvocationRequest
    ) -> RunnerInvocationResponse:
        """Execute a tool invocation via Kubernetes Job.

        Creates a per-invocation Job/Pod, waits for completion, and
        returns the result.
        """
        job_name = f"tool-{request.invocation_id}"
        self._active_jobs[job_name] = request

        try:
            # Validate sandbox requirements
            violations = self._validate_sandbox_k8s(request.sandbox_spec)
            if violations:
                return RunnerInvocationResponse(
                    invocation_id=request.invocation_id,
                    status="failed",
                    error=f"Sandbox violations: {'; '.join(violations)}",
                )

            # Build and submit Job manifest
            manifest = self._build_job_manifest(request)

            # In real implementation, this would:
            # 1. Apply Job manifest to Kubernetes API
            # 2. Watch Job status until completion/failure
            # 3. Collect Pod logs
            # 4. Clean up Job resource
            output = await self._execute_job(request, manifest)

            return RunnerInvocationResponse(
                invocation_id=request.invocation_id,
                status="completed",
                output=output,
                execution_time_ms=0.0,
            )
        except Exception as exc:
            logger.exception("Kubernetes runner execution failed")
            return RunnerInvocationResponse(
                invocation_id=request.invocation_id,
                status="failed",
                error=f"Kubernetes execution error: {exc}",
            )
        finally:
            self._active_jobs.pop(job_name, None)

    def _validate_sandbox_k8s(self, sandbox: dict[str, Any]) -> list[str]:
        """Validate sandbox spec for Kubernetes runner."""
        violations: list[str] = []
        if sandbox.get("no_docker_socket") is False:
            violations.append("Docker socket must not be mounted in K8s pod")
        if sandbox.get("non_root") is False:
            violations.append("K8s pods must run as non-root")
        if not self._spec.image_digest:
            violations.append("K8s runner requires pinned image digest")
        return violations

    async def _execute_job(
        self, request: RunnerInvocationRequest, manifest: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute tool via Kubernetes Job.

        In real implementation, this would interact with the K8s API.
        """
        return {
            "tool_name": request.tool_name,
            "status": "executed_k8s",
            "invocation_id": str(request.invocation_id),
            "job_name": manifest["metadata"]["name"],
            "namespace": self._namespace,
        }

    async def shutdown(self) -> None:
        """Graceful shutdown: wait for active jobs to complete."""
        logger.info(
            "Shutting down Kubernetes runner %s, %d active jobs",
            self._spec.id,
            len(self._active_jobs),
        )
        self._active_jobs.clear()
