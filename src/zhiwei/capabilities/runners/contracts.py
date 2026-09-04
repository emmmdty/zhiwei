"""S4 Runner contract types.

Defines the typed interface between the Tool Gateway and runner backends.
All runners must satisfy the RunnerProtocol; the Gateway never calls runners
directly — it goes through RunnerClient which enforces IPC authentication.

事实源：S4 spec §5 (Connection and execution)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc


class RunnerStatus(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNAVAILABLE = "unavailable"
    MAINTENANCE = "maintenance"


class RunnerKind(StrEnum):
    PREBUILT = "prebuilt"
    REMOTE_HTTP = "remote_http"
    KUBERNETES = "kubernetes"


class RunnerCapability(StrEnum):
    """Capabilities a runner can provide."""

    STDIO = "stdio"
    SCRIPT = "script"
    REMOTE_HTTP = "remote_http"
    CONTAINER = "container"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class RunnerSpec(_FrozenModel):
    """Immutable specification for a runner backend."""

    id: UUID = Field(default_factory=new_id)
    name: str = Field(min_length=1)
    kind: RunnerKind
    capabilities: tuple[RunnerCapability, ...] = ()
    image_digest: str | None = None
    network_zone: str = "default"
    max_concurrent: int = 1
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    endpoint_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: RunnerStatus = RunnerStatus.HEALTHY
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    def compute_content_digest(self) -> str:
        """Compute SHA-256 digest of runner spec content."""
        return digest_bytes(
            canonical_json(
                {
                    "name": self.name,
                    "kind": self.kind.value,
                    "image_digest": self.image_digest,
                    "network_zone": self.network_zone,
                }
            )
        )


class RunnerHealth(_FrozenModel):
    """Health check result for a runner backend."""

    runner_id: UUID
    status: RunnerStatus
    checked_at: datetime
    active_tasks: int = 0
    max_tasks: int = 1
    error_message: str | None = None
    uptime_seconds: float = 0.0


class RunnerInvocationRequest(_FrozenModel):
    """Request sent to a runner for tool execution."""

    invocation_id: UUID
    tool_name: str = Field(min_length=1)
    tool_type: str = Field(min_length=1)
    input_args: dict[str, Any] = Field(default_factory=dict)
    sandbox_spec: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 30
    credential_ref: str | None = None
    idempotency_key: str = Field(min_length=1)


class RunnerInvocationResponse(_FrozenModel):
    """Response from a runner after tool execution."""

    invocation_id: UUID
    status: str  # completed | failed | effect_unknown
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    execution_time_ms: float = 0.0
    resource_usage: dict[str, Any] = Field(default_factory=dict)


class RunnerProtocol(Protocol):
    """Structural contract for all runner backends.

    The Gateway calls runners through this interface. Each runner kind
    (prebuilt, remote_http, kubernetes) implements this protocol.
    """

    @property
    def runner_id(self) -> UUID: ...

    @property
    def runner_kind(self) -> RunnerKind: ...

    async def health_check(self) -> RunnerHealth: ...

    async def execute(
        self, request: RunnerInvocationRequest
    ) -> RunnerInvocationResponse: ...

    async def shutdown(self) -> None: ...


class BaseRunner(ABC):
    """Abstract base class for runner implementations.

    Provides common fields and lifecycle management. Concrete runners
    (prebuilt, remote_http, kubernetes) subclass this.
    """

    def __init__(self, spec: RunnerSpec) -> None:
        self._spec = spec

    @property
    def runner_id(self) -> UUID:
        return self._spec.id

    @property
    def runner_kind(self) -> RunnerKind:
        return self._spec.kind

    @property
    def spec(self) -> RunnerSpec:
        return self._spec

    @abstractmethod
    async def health_check(self) -> RunnerHealth: ...

    @abstractmethod
    async def execute(
        self, request: RunnerInvocationRequest
    ) -> RunnerInvocationResponse: ...

    async def shutdown(self) -> None:  # noqa: B027
        """Default no-op shutdown; override for resource cleanup."""


class RunnerRegistry:
    """Registry of available runner backends.

    Maps runner IDs to their implementations and provides lookup by kind
    and capability.
    """

    def __init__(self) -> None:
        self._runners: dict[UUID, BaseRunner] = {}

    def register(self, runner: BaseRunner) -> None:
        self._runners[runner.runner_id] = runner

    def unregister(self, runner_id: UUID) -> None:
        self._runners.pop(runner_id, None)

    def get(self, runner_id: UUID) -> BaseRunner | None:
        return self._runners.get(runner_id)

    def find_by_kind(self, kind: RunnerKind) -> list[BaseRunner]:
        return [r for r in self._runners.values() if r.runner_kind == kind]

    def find_healthy(self) -> list[BaseRunner]:
        return [
            r for r in self._runners.values()
            if r.spec.status == RunnerStatus.HEALTHY
        ]

    def list_all(self) -> list[BaseRunner]:
        return list(self._runners.values())
