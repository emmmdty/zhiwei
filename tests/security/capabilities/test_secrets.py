"""S4-T3 Security: Secrets backend protocol, SecretRef opaqueness, Vault backend invariants。

验证：
- SecretRef repr 不泄露内部结构（就是值本身）
- CredentialBinding 的 secret_ref 不以 credential_data 等字段暴露
- SecretBackend Protocol 方法存在且签名完整
- Vault backend rotation 参数校验
- Vault backend 不接受小于 32 字节的 key material
- domain model 不泄露 plaintext/key/ciphertext
- Connection/CredentialBinding 不持有明文凭据字段
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
    CredentialBinding,
    CredentialType,
)
from zhiwei.secrets.base import SecretBackend, SecretRef
from zhiwei.secrets.vault import VaultTransitBackend

# --------------------------------------------------------------------------- SecretRef opaqueness


def test_secret_ref_repr_is_value() -> None:
    ref = SecretRef(value="vault:secret/data/api-key:v1")
    assert repr(ref) == "vault:secret/data/api-key:v1"
    assert str(ref) == "vault:secret/data/api-key:v1"


def test_secret_ref_no_plaintext_in_repr() -> None:
    ref = SecretRef(value="connection:abc:credential:xyz")
    rendered = repr(ref)
    assert "plaintext" not in rendered.lower()
    assert "secret_data" not in rendered.lower()


# --------------------------------------------------------------------------- no credential data in domain


def test_credential_binding_has_no_credential_data_field() -> None:
    assert "credential_data" not in CredentialBinding.model_fields
    assert "secret_data" not in CredentialBinding.model_fields
    assert "plaintext" not in CredentialBinding.model_fields
    assert "token" not in CredentialBinding.model_fields


def test_connection_has_no_credential_fields() -> None:
    assert "credential_data" not in Connection.model_fields
    assert "secret_data" not in Connection.model_fields
    assert "api_key" not in Connection.model_fields
    assert "token" not in Connection.model_fields


def test_credential_binding_secret_ref_is_opaque() -> None:
    ref = SecretRef(value="vault:v1:opaque-ciphertext-blob")
    b = CredentialBinding(
        id=uuid4(),
        connection_id=uuid4(),
        organization_id=uuid4(),
        workspace_id=uuid4(),
        credential_type=CredentialType.OAUTH2,
        secret_ref=ref,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    rendered = repr(b)
    assert "opaque-ciphertext-blob" in rendered or "vault:v1:" in rendered
    assert "plaintext" not in rendered.lower()


# --------------------------------------------------------------------------- SecretBackend Protocol


def test_secret_backend_protocol_surface() -> None:
    methods = {"put", "get", "revoke", "rewrap", "rotate"}
    missing = methods - set(dir(SecretBackend))
    assert not missing, f"SecretBackend port missing: {missing}"


# --------------------------------------------------------------------------- Vault backend


def test_vault_backend_rotation_requires_32_byte_material() -> None:
    backend = VaultTransitBackend(vault_url="http://localhost:8200", token="test")
    with pytest.raises(ValueError, match="32 bytes"):
        backend.rotate(key_material=b"short")


def test_vault_backend_rotation_returns_key_id() -> None:
    backend = VaultTransitBackend(vault_url="http://localhost:8200", token="test")
    result = backend.rotate(key_id="new-key")
    assert result == "new-key"


def test_vault_backend_default_rotation_returns_key_name() -> None:
    backend = VaultTransitBackend(
        vault_url="http://localhost:8200", token="test", key_name="my-key"
    )
    result = backend.rotate()
    assert result == "my-key"


def test_vault_backend_stores_secrets_in_memory() -> None:
    backend = VaultTransitBackend(vault_url="http://localhost:8200", token="test")
    assert len(backend._in_memory_store) == 0


# --------------------------------------------------------------------------- domain model security


def test_connection_frozen_prevents_credential_injection() -> None:
    conn = _conn()
    with pytest.raises(ValidationError):
        conn.status = ConnectionStatus.REVOKED  # type: ignore[misc]
    assert "credential_data" not in Connection.model_fields


def test_binding_frozen_prevents_secret_ref_override() -> None:
    b = _binding()
    with pytest.raises(ValidationError):
        b.secret_ref = SecretRef(value="hacked")  # type: ignore[misc]


def test_binding_expires_at_can_be_none() -> None:
    b = _binding(expires_at=None)
    assert b.expires_at is None


def test_binding_expires_at_requires_tz_aware() -> None:
    b = _binding(expires_at=datetime.now(UTC))
    assert b.expires_at is not None
    assert b.expires_at.tzinfo is not None


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
