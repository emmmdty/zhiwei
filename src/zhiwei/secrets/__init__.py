"""S1-T2 RED skeleton：SecretBackend port（S4 复用，非 OIDC 专用）。

业务层只持 opaque SecretRef，不接触数据库 ciphertext 结构；GREEN 提供
PostgreSQL-backed AES-GCM envelope 实现。
"""

from __future__ import annotations

from dataclasses import dataclass

from zhiwei.secrets.base import (
    SecretBackend,
    SecretIntegrityError,
    SecretRef,
    SecretRevokedError,
    SecretVersionConflictError,
)

__all__ = [
    "SecretBackend",
    "SecretIntegrityError",
    "SecretRef",
    "SecretRevokedError",
    "SecretVersionConflictError",
]


@dataclass(frozen=True)
class _BackendMarker:
    """占位，避免 RED 阶段出现空模块。"""
