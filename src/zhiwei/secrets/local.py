"""S1-T2 RED skeleton：本地 PostgreSQL-backed AES-GCM envelope backend。

契约（冻结）：
- 每个 secret 使用随机 256-bit DEK 与随机 96-bit AES-GCM nonce；plaintext 由 DEK 加密，
  DEK 由当前 master KEK 用 AES-GCM 包装；禁止确定性 nonce / ECB / CBC / 自制 MAC；
- master key 只从显式挂载文件加载：`key_id=<base64(32 bytes)>` 每行一条，最后一行是
  当前活动 key；malformed / 重复 id / 非 32 字节 key 加载期失败（fail closed）；
- 支持 old+active keyring；rotation 优先 rewrap DEK 并用 CAS 增加版本；
- 重启后使用同一 keyring 必须可解密。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zhiwei.secrets.base import SecretBackend


@dataclass(frozen=True)
class KeyringEntry:
    """keyring 单条：id、单调版本与 32 字节 key 材料。"""

    key_id: str
    key_version: int
    key_material: bytes


class Keyring:
    """有序 keyring；active_key_id 指向最新条目（加载时以文件顺序为准）。"""

    def __init__(self, entries: dict[str, KeyringEntry], active_key_id: str) -> None:
        self._entries = entries
        self._active_key_id = active_key_id

    @property
    def entries(self) -> dict[str, KeyringEntry]:
        return dict(self._entries)

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def active(self) -> KeyringEntry:
        return self._entries[self._active_key_id]

    def get(self, key_id: str) -> KeyringEntry | None:
        return self._entries.get(key_id)

    def replace(self, key_id: str) -> Keyring:
        """返回只含指定 key 的 keyring（测试/受控场景用）。"""
        return Keyring(
            {key_id: self._entries[key_id]},
            active_key_id=key_id,
        )

    def __repr__(self) -> str:
        return f"Keyring(active={self._active_key_id!r}, entries={sorted(self._entries)})"


def load_keyring(path: Path) -> Keyring:
    """从显式挂载文件加载 keyring；任何 malformed 内容加载期失败。"""
    raise NotImplementedError("S1-T2 keyring 加载未实现")


@dataclass(frozen=True)
class EnvelopeCiphertext:
    """AES-GCM envelope 的密文结构（GREEN 实现）；repr 不得暴露材料。"""

    envelope_version: int
    key_id: str
    key_version: int
    data_nonce: bytes
    ciphertext: bytes
    wrap_nonce: bytes
    wrapped_dek: bytes


class LocalEnvelopeCipher:
    """纯密码学层：DEK 加密 plaintext，KEK 包装 DEK（AES-GCM，无状态）。"""

    @classmethod
    def encrypt(
        cls,
        *,
        plaintext: bytes,
        aad: bytes,
        keyring: Keyring,
    ) -> EnvelopeCiphertext:
        raise NotImplementedError("S1-T2 envelope cipher 未实现")

    @classmethod
    def decrypt(
        cls,
        *,
        envelope: EnvelopeCiphertext,
        aad: bytes,
        keyring: Keyring,
    ) -> bytes:
        raise NotImplementedError("S1-T2 envelope cipher 未实现")


class LocalSecretBackend(SecretBackend):
    """PostgreSQL-backed envelope store（zhiwei_identity 引擎），持 keyring。"""

    def __init__(self, session_factory: Any, keyring: Keyring) -> None:
        self._session_factory = session_factory
        self._keyring = keyring

    @property
    def keyring(self) -> Keyring:
        return self._keyring

    async def put(
        self,
        ref: Any,
        plaintext: bytes,
        aad: bytes,
        purpose: str,
        expected_version: int | None = None,
    ) -> Any:
        raise NotImplementedError("S1-T2 secret backend put 未实现")

    async def get(self, ref: Any, aad: bytes) -> bytes:
        raise NotImplementedError("S1-T2 secret backend get 未实现")

    async def revoke(self, ref: Any) -> None:
        raise NotImplementedError("S1-T2 secret backend revoke 未实现")

    async def rewrap(self, ref: Any, aad: bytes, expected_version: int) -> Any:
        raise NotImplementedError("S1-T2 secret backend rewrap 未实现")

    def rotate(self, *, key_id: str | None = None, key_material: bytes | None = None) -> str:
        raise NotImplementedError("S1-T2 secret backend rotate 未实现")
