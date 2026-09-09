"""S4 Connection domain types.

subject modes:
- user_delegated: agent 代理某 principal 使用其个人凭据
- workspace_service: workspace 级别服务身份
- service_account: 独立服务账号（非用户）

provider 与 credential 分离：Connection 只持 provider 版本引用，
实际凭据存储在 CredentialBinding（通过 SecretRef 指向 SecretBackend）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.time import ensure_utc


class SubjectMode(StrEnum):
    USER_DELEGATED = "user_delegated"
    WORKSPACE_SERVICE = "workspace_service"
    SERVICE_ACCOUNT = "service_account"


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class Connection(_FrozenModel):
    """Workspace scope Connection：绑定 provider 版本与 subject mode。"""

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    provider_version_id: UUID
    subject_mode: SubjectMode
    status: ConnectionStatus = ConnectionStatus.ACTIVE
    principal_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(ge=1, default=1)
    schema_version: int = 1
    created_at: datetime
    updated_at: datetime

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    def compute_fingerprint(self) -> str:
        return digest_bytes(
            canonical_json(
                {
                    "organization_id": str(self.organization_id),
                    "workspace_id": str(self.workspace_id),
                    "provider_version_id": str(self.provider_version_id),
                    "subject_mode": self.subject_mode.value,
                    "principal_id": str(self.principal_id) if self.principal_id else None,
                }
            )
        )
