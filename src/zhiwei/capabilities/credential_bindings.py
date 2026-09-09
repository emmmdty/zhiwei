"""S4 CredentialBinding domain types。

CredentialBinding 将 Connection 与凭据（通过 SecretRef 指向 SecretBackend）
绑定。凭据的实际加密/解密由 SecretBackend port 处理，Binding 只持 opaque ref。

S4 spec §5：provider 与 credential 分离；approval 后重新读取
membership/policy/connection/credential before execution。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.time import ensure_utc
from zhiwei.secrets.base import SecretRef


class BindingStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class CredentialType(StrEnum):
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    BASIC_AUTH = "basic_auth"
    TOKEN = "token"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class CredentialBinding(_FrozenModel):
    """绑定 Connection 与 SecretRef；凭据实际材料不进入 domain model。"""

    id: UUID
    connection_id: UUID
    organization_id: UUID
    workspace_id: UUID
    credential_type: CredentialType
    secret_ref: SecretRef
    status: BindingStatus = BindingStatus.ACTIVE
    purpose: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None
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
