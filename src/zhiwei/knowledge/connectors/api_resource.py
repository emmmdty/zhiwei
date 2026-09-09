"""API/MCP resource connector: observations enter Source Ledger before becoming Evidence.

observation 必须进入 Source Ledger 才可作为 Evidence。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.capabilities.inspection.network import check_url_safety
from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.knowledge.contracts import Locator, SourceObject, SourceVersion
from zhiwei.knowledge.ledger import SourceLedger


class ReproducibilityLevel(StrEnum):
    """ADR-003 reproducibility levels for API/MCP resource connectors."""

    REPLAYABLE = "replayable"
    COPY_FROZEN = "copy_frozen"
    REFERENCE_ONLY = "reference_only"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Observation(_FrozenModel):
    """An observation from an API/MCP resource.

    Must enter the Source Ledger before becoming Evidence.
    """

    resource_uri: str = Field(min_length=1)
    method: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utc_now)


class ObservationResult(_FrozenModel):
    """Result of recording an observation in the Source Ledger."""

    observation: Observation
    source_version: SourceVersion
    content_digest: str


class ApiResourceConnector:
    """API/MCP resource connector for the Source Ledger.

    Requires reproducibility_level declaration at connection time.
    Observations must enter the Source Ledger before becoming Evidence.
    """

    def __init__(
        self,
        endpoint: str,
        organization_id: UUID,
        workspace_id: UUID,
        reproducibility_level: ReproducibilityLevel = ReproducibilityLevel.REFERENCE_ONLY,
        *,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("endpoint must not be blank")
        ssrf_report = check_url_safety(endpoint)
        if not ssrf_report.passed:
            blocking = [f.message for f in ssrf_report.findings if f.is_blocking()]
            raise ValueError(
                f"URL safety check failed for endpoint: {'; '.join(blocking)}"
            )
        self._endpoint = endpoint
        self._organization_id = organization_id
        self._workspace_id = workspace_id
        self._reproducibility_level = reproducibility_level
        self._auth_headers = auth_headers or {}
        self._connected = False
        self._ledger = SourceLedger()

    @property
    def reproducibility_level(self) -> ReproducibilityLevel:
        return self._reproducibility_level

    def connect(self) -> None:
        """Establish connection to the API/MCP resource."""
        self._connected = True

    def disconnect(self) -> None:
        """Close connection to the API/MCP resource."""
        self._connected = False

    def _assert_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Connector is not connected")

    def record_observation(
        self,
        observation: Observation,
    ) -> ObservationResult:
        """Record an observation in the Source Ledger.

        The observation must be recorded in the ledger before it can
        be used as Evidence (S5 spec §4).
        """
        self._assert_connected()

        content = canonical_json(
            {
                "resource_uri": observation.resource_uri,
                "method": observation.method,
                "params": observation.params,
                "observed_at": observation.observed_at.isoformat(),
            }
        )
        content_digest = digest_bytes(content)

        locator = Locator(
            connector="api_resource",
            uri=f"{self._endpoint}{observation.resource_uri}",
            version_hint=content_digest,
        )

        source_object = SourceObject(
            id=new_id(),
            organization_id=self._organization_id,
            workspace_id=self._workspace_id,
            source_type="api_resource",
            metadata={
                "resource_uri": observation.resource_uri,
                "method": observation.method,
                "reproducibility_level": self._reproducibility_level.value,
            },
        )
        self._ledger.register_object(source_object)

        now = utc_now()
        source_version = self._ledger.create_version(
            source_object.id,
            locator=locator,
            content_digest=content_digest,
            observed_at=now,
            valid_at=now,
            metadata={
                "reproducibility_level": self._reproducibility_level.value,
                "method": observation.method,
            },
        )

        return ObservationResult(
            observation=observation,
            source_version=source_version,
            content_digest=content_digest,
        )

    def get_version(self, version_id: UUID) -> SourceVersion:
        """Retrieve a SourceVersion by id."""
        self._assert_connected()
        return self._ledger.get_version(version_id)

    def list_versions(self, source_object_id: UUID) -> list[SourceVersion]:
        """List all versions of a SourceObject."""
        self._assert_connected()
        return self._ledger.list_versions(source_object_id)
