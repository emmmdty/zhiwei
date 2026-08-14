"""SecretBackend port 与本地 AES-GCM envelope 实现（S1-T2；S4 复用）。"""

from __future__ import annotations

from zhiwei.secrets.base import (
    SecretBackend,
    SecretEnvelopeMeta,
    SecretIntegrityError,
    SecretRef,
    SecretRevokedError,
    SecretVersionConflictError,
)
from zhiwei.secrets.local import (
    EnvelopeCiphertext,
    Keyring,
    KeyringEntry,
    LocalEnvelopeCipher,
    LocalSecretBackend,
    load_keyring,
)

__all__ = [
    "EnvelopeCiphertext",
    "Keyring",
    "KeyringEntry",
    "LocalEnvelopeCipher",
    "LocalSecretBackend",
    "SecretBackend",
    "SecretEnvelopeMeta",
    "SecretIntegrityError",
    "SecretRef",
    "SecretRevokedError",
    "SecretVersionConflictError",
    "load_keyring",
]
