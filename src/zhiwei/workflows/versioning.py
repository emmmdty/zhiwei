"""S2 runtime: Temporal workflow for version publishing。

事实源：design doc §4.3、S2-T3 plan。

Workflow for publishing agent versions. Orchestration only — validates
the version, publishes it, and creates the run configuration.
No Temporal SDK dependency.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from zhiwei.agents.task_graph import TaskGraph


class VersionPublishConfig(BaseModel):
    """Configuration for publishing an agent version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version_id: UUID
    agent_id: UUID
    graph: TaskGraph


class VersionPublishResult(BaseModel):
    """Result of a version publish workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version_id: UUID
    status: str
    published: bool = False


class VersionPublishClient(Protocol):
    """Port for the Temporal workflow client (version publishing)."""

    def start_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        input: VersionPublishConfig,
    ) -> None: ...

    def execute_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        input: VersionPublishConfig,
    ) -> VersionPublishResult: ...


class VersionPublishActivities(Protocol):
    """Protocol for version publish activities."""

    def validate_version(self, config: VersionPublishConfig) -> bool: ...

    def publish_version(self, config: VersionPublishConfig) -> VersionPublishResult: ...


class VersionPublishWorkflow:
    """Workflow for publishing agent versions.

    Validates the version, publishes it, and returns the result.
    Orchestration only — activities handle side effects.
    """

    WORKFLOW_TYPE = "VersionPublish"

    def __init__(
        self,
        client: VersionPublishClient,
        activities: VersionPublishActivities,
    ) -> None:
        self._client = client
        self._activities = activities

    def start(self, config: VersionPublishConfig) -> None:
        """Start the workflow via the client."""
        workflow_id = f"version-{config.version_id}"
        self._client.start_workflow(
            workflow_type=self.WORKFLOW_TYPE,
            workflow_id=workflow_id,
            input=config,
        )

    def run(self, config: VersionPublishConfig) -> VersionPublishResult:
        """Execute the version publish workflow."""
        is_valid = self._activities.validate_version(config)
        if not is_valid:
            return VersionPublishResult(
                version_id=config.version_id,
                status="rejected",
                published=False,
            )
        return self._activities.publish_version(config)
