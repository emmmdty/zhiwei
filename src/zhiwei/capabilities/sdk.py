"""SDK Provider SPI: stable discovery, invoke, health and auth contract.

Constraints from S4 spec §4:
- Stable discovery/invoke/health/auth contract.
- Still goes through admission/version/binding lifecycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SDKProviderError(RuntimeError):
    """Raised when an SDK provider operation fails."""


class SDKHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class SDKAuthMethod(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    OAUTH2 = "oauth2"
    CUSTOM = "custom"


class SDKCapability(BaseModel):
    """A capability advertised by an SDK provider during discovery."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str = ""
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class SDKDiscoveryResult(BaseModel):
    """Result of provider discovery."""

    model_config = ConfigDict(frozen=True)

    provider_name: str = Field(min_length=1)
    provider_version: str = ""
    capabilities: tuple[SDKCapability, ...] = ()
    auth_methods: tuple[SDKAuthMethod, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class SDKHealthResult(BaseModel):
    """Result of a health check."""

    model_config = ConfigDict(frozen=True)

    status: SDKHealthStatus
    message: str = ""
    latency_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class SDKInvokeResult(BaseModel):
    """Result of invoking an SDK capability."""

    model_config = ConfigDict(frozen=True)

    success: bool
    output: Any = None
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SDKProviderPort(ABC):
    """Abstract port for SDK provider discovery, invoke, health and auth.

    Implementations must be stateless and idempotent where possible.
    """

    @abstractmethod
    def discover(self) -> SDKDiscoveryResult:
        """Discover provider capabilities and auth requirements."""
        ...

    @abstractmethod
    def invoke(
        self,
        capability_name: str,
        input_data: dict[str, Any],
        *,
        auth_context: dict[str, str] | None = None,
    ) -> SDKInvokeResult:
        """Invoke a provider capability with the given input."""
        ...

    @abstractmethod
    def health_check(self) -> SDKHealthResult:
        """Check provider health status."""
        ...

    @abstractmethod
    def get_auth_methods(self) -> tuple[SDKAuthMethod, ...]:
        """Return the authentication methods supported by this provider."""
        ...


class InMemorySDKProvider(SDKProviderPort):
    """Reference in-memory SDK provider for testing."""

    def __init__(
        self,
        name: str = "in-memory-provider",
        version: str = "1.0.0",
        capabilities: list[SDKCapability] | None = None,
        auth_methods: tuple[SDKAuthMethod, ...] = (SDKAuthMethod.NONE,),
    ) -> None:
        self._name = name
        self._version = version
        self._capabilities = tuple(capabilities or [])
        self._auth_methods = auth_methods
        self._invoke_handler: Any = None

    def set_invoke_handler(self, handler: Any) -> None:
        """Set a custom invoke handler for testing."""
        self._invoke_handler = handler

    def discover(self) -> SDKDiscoveryResult:
        return SDKDiscoveryResult(
            provider_name=self._name,
            provider_version=self._version,
            capabilities=self._capabilities,
            auth_methods=self._auth_methods,
        )

    def invoke(
        self,
        capability_name: str,
        input_data: dict[str, Any],
        *,
        auth_context: dict[str, str] | None = None,
    ) -> SDKInvokeResult:
        if self._invoke_handler is not None:
            return self._invoke_handler(capability_name, input_data, auth_context=auth_context)
        return SDKInvokeResult(
            success=True,
            output={"echo": input_data},
        )

    def health_check(self) -> SDKHealthResult:
        return SDKHealthResult(status=SDKHealthStatus.HEALTHY)

    def get_auth_methods(self) -> tuple[SDKAuthMethod, ...]:
        return self._auth_methods
