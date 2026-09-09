"""S4-T3 Contract: Connection and CredentialBinding domain models。

验证：
- frozen model 不可变
- 必填字段缺失 → ValidationError
- 枚举值正确接受/拒绝
- SubjectMode / CredentialType 枚举完整
- version 正整数约束
- Connection fingerprint 确定性
- SecretRef 可作为 CredentialBinding 的 secret_ref
- metadata 默认空 dict
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from zhiwei.capabilities.connections import (
    Connection,
    ConnectionStatus,
    SubjectMode,
)
from zhiwei.capabilities.credential_bindings import (
    BindingStatus,
    CredentialBinding,
    CredentialType,
)
from zhiwei.secrets.base import SecretRef


def _conn(**overrides: object) -> Connection:
    defaults = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "workspace_id": uuid4(),
        "provider_version_id": uuid4(),
        "subject_mode": SubjectMode.USER_DELEGATED,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Connection(**defaults)  # type: ignore[arg-type]


def _binding(**overrides: object) -> CredentialBinding:
    defaults = {
        "id": uuid4(),
        "connection_id": uuid4(),
        "organization_id": uuid4(),
        "workspace_id": uuid4(),
        "credential_type": CredentialType.API_KEY,
        "secret_ref": SecretRef(value="cred:test"),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return CredentialBinding(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- frozen


def test_connection_is_frozen() -> None:
    conn = _conn()
    with pytest.raises(ValidationError):
        conn.status = ConnectionStatus.REVOKED  # type: ignore[misc]


def test_credential_binding_is_frozen() -> None:
    binding = _binding()
    with pytest.raises(ValidationError):
        binding.status = BindingStatus.REVOKED  # type: ignore[misc]


# --------------------------------------------------------------------------- required fields


def test_connection_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError):
        Connection()  # type: ignore[call-arg]


def test_credential_binding_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError):
        CredentialBinding()  # type: ignore[call-arg]


# --------------------------------------------------------------------------- enum values


def test_subject_modes() -> None:
    conn_user = _conn(subject_mode=SubjectMode.USER_DELEGATED)
    assert conn_user.subject_mode == SubjectMode.USER_DELEGATED

    conn_svc = _conn(subject_mode=SubjectMode.WORKSPACE_SERVICE)
    assert conn_svc.subject_mode == SubjectMode.WORKSPACE_SERVICE

    conn_sa = _conn(subject_mode=SubjectMode.SERVICE_ACCOUNT)
    assert conn_sa.subject_mode == SubjectMode.SERVICE_ACCOUNT


def test_connection_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _conn(status="deleted")  # type: ignore[arg-type]


def test_credential_types() -> None:
    for ct in CredentialType:
        b = _binding(credential_type=ct)
        assert b.credential_type is ct


def test_binding_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _binding(status="unknown")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- version


def test_connection_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _conn(version=0)


def test_credential_binding_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _binding(version=0)


# --------------------------------------------------------------------------- fingerprint


def test_connection_fingerprint_is_deterministic() -> None:
    args = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "workspace_id": uuid4(),
        "provider_version_id": uuid4(),
        "subject_mode": SubjectMode.USER_DELEGATED,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    first = Connection(**args).compute_fingerprint()
    second = Connection(**args).compute_fingerprint()
    assert first == second


def test_connection_fingerprint_changes_with_workspace() -> None:
    args = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "workspace_id": uuid4(),
        "provider_version_id": uuid4(),
        "subject_mode": SubjectMode.USER_DELEGATED,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    a = Connection(**args).compute_fingerprint()
    args["workspace_id"] = uuid4()
    b = Connection(**args).compute_fingerprint()
    assert a != b


def test_connection_fingerprint_changes_with_provider_version() -> None:
    args = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "workspace_id": uuid4(),
        "provider_version_id": uuid4(),
        "subject_mode": SubjectMode.USER_DELEGATED,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    a = Connection(**args).compute_fingerprint()
    args["provider_version_id"] = uuid4()
    b = Connection(**args).compute_fingerprint()
    assert a != b


# --------------------------------------------------------------------------- SecretRef


def test_credential_binding_accepts_secret_ref() -> None:
    ref = SecretRef(value="conn:abc123:cred:xyz789")
    binding = _binding(secret_ref=ref)
    assert binding.secret_ref == ref
    assert str(binding.secret_ref) == "conn:abc123:cred:xyz789"


def test_credential_binding_secret_ref_is_required() -> None:
    with pytest.raises(ValidationError):
        CredentialBinding(
            id=uuid4(),
            connection_id=uuid4(),
            organization_id=uuid4(),
            workspace_id=uuid4(),
            credential_type=CredentialType.API_KEY,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )  # type: ignore[call-arg]


# --------------------------------------------------------------------------- metadata defaults


def test_connection_metadata_defaults_to_empty() -> None:
    conn = _conn()
    assert conn.metadata == {}


def test_credential_binding_metadata_defaults_to_empty() -> None:
    b = _binding()
    assert b.metadata == {}


def test_connection_metadata_stores_arbitrary_dict() -> None:
    conn = _conn(metadata={"region": "us-east-1", "env": "prod"})
    assert conn.metadata["region"] == "us-east-1"
    assert conn.metadata["env"] == "prod"


# --------------------------------------------------------------------------- scope invariants


def test_binding_scopes_match_connection() -> None:
    org_id, ws_id = uuid4(), uuid4()
    conn = _conn(organization_id=org_id, workspace_id=ws_id)
    b = _binding(connection_id=conn.id, organization_id=org_id, workspace_id=ws_id)
    assert b.organization_id == conn.organization_id
    assert b.workspace_id == conn.workspace_id
    assert b.connection_id == conn.id
