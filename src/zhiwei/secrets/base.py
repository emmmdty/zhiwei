"""通用 SecretBackend port（S1-T2；S4 复用保存 Connection credentials）。

业务层只持 opaque SecretRef，不接触数据库 ciphertext 结构。所有篡改/密钥缺失/AAD
不匹配统一抛 SecretIntegrityError，不泄露任何明文；revoke 后拒绝解密。

port 是结构契约（Protocol）：LocalSecretBackend 与未来 Vault/KMS adapter 都以
方法面匹配，业务层（SessionService）不依赖任何 concrete-only 签名（验收阻断 3）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class SecretIntegrityError(Exception):
    """篡改检测失败的统一错误；不得泄露任何明文。"""


class SecretRevokedError(Exception):
    """目标 secret 已 revoke（或不存在），拒绝解密。"""


class SecretVersionConflictError(Exception):
    """expected_version CAS 失败：行已被并发改写。"""


@dataclass(frozen=True)
class SecretRef:
    """不透明 secret handle；repr/str 就是句柄本身，不含任何材料。"""

    value: str

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return self.value


class SecretEnvelopeMeta(BaseModel):
    """envelope 元数据（不含明文/密文/密钥材料，repr 安全）。"""

    model_config = ConfigDict(frozen=True)

    ref: str
    purpose: str
    version: int
    envelope_version: int
    key_id: str
    key_version: int
    created_at: datetime
    revoked_at: datetime | None = None


class SecretBackend(Protocol):
    """S4 可复用的 secret port（结构契约，业务层依赖的唯一 secret 接口）。

    - put(ref, plaintext, aad, purpose, expected_version=None)：
      expected_version 为 None 时创建或替换（版本递增）；给定值时严格 CAS，
      版本不匹配抛 SecretVersionConflictError；
    - get(ref, aad)：revoked/不存在抛 SecretRevokedError；篡改抛 SecretIntegrityError；
    - revoke(ref)：置 revoked_at，之后拒绝解密；
    - rewrap(ref, aad, expected_version)：仅重包 DEK（不动 ciphertext），CAS 增加版本；
    - rotate()：向 keyring 追加新活动 key（内存态；落盘由 operator 管理）。
    """

    async def put(
        self,
        ref: SecretRef,
        plaintext: bytes,
        aad: bytes,
        purpose: str,
        expected_version: int | None = None,
    ) -> SecretEnvelopeMeta:
        raise NotImplementedError

    async def get(self, ref: SecretRef, aad: bytes) -> bytes:
        raise NotImplementedError

    async def revoke(self, ref: SecretRef) -> None:
        raise NotImplementedError

    async def rewrap(self, ref: SecretRef, aad: bytes, expected_version: int) -> SecretEnvelopeMeta:
        raise NotImplementedError

    def rotate(self, *, key_id: str | None = None, key_material: bytes | None = None) -> str:
        raise NotImplementedError
