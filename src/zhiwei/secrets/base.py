"""S1-T2 RED skeleton：通用 SecretBackend port。

契约（冻结）：
- put/get/revoke/rewrap/rotate + expected_version CAS；S4 复用同一 port 保存
  Connection credentials（per-org AAD）；
- tampered ciphertext / wrapped DEK / nonce / AAD 一律抛统一 SecretIntegrityError；
- revoke 后拒绝解密；rewrap 用 CAS 增加版本，旧 version CAS 失败；
- SecretRef 是 opaque handle，repr 不得暴露任何材料。
"""

from __future__ import annotations

from dataclasses import dataclass


class SecretIntegrityError(Exception):
    """篡改检测失败的统一错误；不得泄露任何明文。"""


class SecretRevokedError(Exception):
    """目标 secret 已 revoke，拒绝解密。"""


class SecretVersionConflictError(Exception):
    """expected_version CAS 失败：行已被并发改写。"""


@dataclass(frozen=True)
class SecretRef:
    """不透明 secret handle；repr/str 就是句柄本身。"""

    value: str

    def __str__(self) -> str:
        return self.value


class SecretBackend:
    """S4 可复用的 secret port；GREEN 提供本地 AES-GCM envelope 实现。"""

    async def put(
        self,
        ref: SecretRef,
        plaintext: bytes,
        aad: bytes,
        purpose: str,
        expected_version: int | None = None,
    ) -> object:
        raise NotImplementedError("S1-T2 secret backend put 未实现")

    async def get(self, ref: SecretRef, aad: bytes) -> bytes:
        raise NotImplementedError("S1-T2 secret backend get 未实现")

    async def revoke(self, ref: SecretRef) -> None:
        raise NotImplementedError("S1-T2 secret backend revoke 未实现")

    async def rewrap(self, ref: SecretRef, aad: bytes, expected_version: int) -> object:
        raise NotImplementedError("S1-T2 secret backend rewrap 未实现")

    def rotate(self, *, key_id: str | None = None, key_material: bytes | None = None) -> str:
        raise NotImplementedError("S1-T2 secret backend rotate 未实现")
