"""本地 PostgreSQL-backed AES-GCM envelope secret backend。

密码学契约（冻结）：
- 每个 secret 使用随机 256-bit DEK 与随机 96-bit AES-GCM nonce；plaintext 由 DEK 加密，
  DEK 由当前 master KEK 用 AES-GCM 包装（wrap 附带 purpose+ref 绑定的 wrap AAD，
  防止跨行交换 wrapped_dek）；禁止确定性 nonce / ECB / CBC / 自制 MAC；
- master key 只从显式挂载文件加载：`key_id=<base64(32 bytes)>` 每行一条，最后一行是
  活动 key；malformed / 重复 id / 非 32 字节 key 加载期失败（fail closed）；
- 支持 old+active keyring；rotation 优先 rewrap DEK 并用 CAS 增加版本；旧 key 移除前
  完成 rewrap；重启后使用同一 keyring 必须可解密；
- ciphertext / wrapped DEK / nonce / AAD 任一篡改统一抛 SecretIntegrityError。

安全说明（为什么 keyring 文件用文字格式）：Docker secret 是只读挂载，operator 通过
追加新行完成 rotation；backend 的 rotate() 只更新进程内 keyring，重启前 operator 必须
把新 key 写入文件，否则重启后仍用旧 key 解密（fail closed 不丢数据）。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.contracts.canonical import canonical_json
from zhiwei.secrets.base import (
    SecretBackend,
    SecretEnvelopeMeta,
    SecretIntegrityError,
    SecretRef,
    SecretRevokedError,
    SecretVersionConflictError,
)

_ENVELOPE_VERSION = 1
_DEK_SIZE = 32
_NONCE_SIZE = 12


class KeyringEntry:
    """keyring 单条：id、单调版本与 32 字节 key 材料。"""

    __slots__ = ("key_id", "key_material", "key_version")

    def __init__(self, key_id: str, key_version: int, key_material: bytes) -> None:
        self.key_id = key_id
        self.key_version = key_version
        self.key_material = key_material


class Keyring:
    """有序 keyring；active 指向最新条目（文件顺序 = 版本顺序）。"""

    def __init__(self, entries: dict[str, KeyringEntry], active_key_id: str) -> None:
        self._entries = dict(entries)
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
        """返回只保留指定 key 的 keyring；key 不存在时得到空 keyring（fail closed）。

        空 keyring 的 active_key_id 指向幽灵条目：任何 get() 返回 None，
        decrypt 必然抛 SecretIntegrityError。
        """
        return Keyring(
            {
                entry.key_id: entry
                for entry in self._entries.values()
                if entry.key_id == key_id
            },
            active_key_id=key_id,
        )

    def with_added(self, entry: KeyringEntry) -> Keyring:
        """返回追加了新活动 key 的副本；重复 id 拒绝（fail closed）。"""
        if entry.key_id in self._entries:
            raise ValueError(f"duplicate key id: {entry.key_id!r}")
        entries = dict(self._entries)
        entries[entry.key_id] = entry
        return Keyring(entries, active_key_id=entry.key_id)

    def __repr__(self) -> str:
        return f"Keyring(active={self._active_key_id!r}, entries={sorted(self._entries)})"


def load_keyring(path: Path) -> Keyring:
    """从显式挂载文件加载 keyring；任何 malformed 内容加载期失败。"""
    raw = Path(path).read_bytes()
    lines = raw.decode("utf-8").splitlines()
    if not lines:
        raise ValueError("keyring file is empty")
    entries: dict[str, KeyringEntry] = {}
    for version, line in enumerate(lines, start=1):
        if "=" not in line:
            raise ValueError(f"malformed keyring line (missing '='): {line!r}")
        key_id, encoded = line.split("=", 1)
        if not key_id:
            raise ValueError("keyring line has an empty key id")
        if not encoded:
            raise ValueError(f"keyring line {key_id!r} has empty key material")
        try:
            material = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError(f"keyring line {key_id!r} is not valid base64") from exc
        if len(material) != _DEK_SIZE:
            raise ValueError(f"keyring line {key_id!r} must decode to 32 bytes")
        if key_id in entries:
            raise ValueError(f"duplicate key id in keyring: {key_id!r}")
        entries[key_id] = KeyringEntry(key_id=key_id, key_version=version, key_material=material)
    if not entries:
        raise ValueError("keyring file contains no keys")
    active = next(reversed(entries))
    return Keyring(entries, active_key_id=active)


class EnvelopeCiphertext:
    """AES-GCM envelope 密文结构；repr 不暴露材料。"""

    __slots__ = (
        "ciphertext",
        "data_nonce",
        "envelope_version",
        "key_id",
        "key_version",
        "wrap_nonce",
        "wrapped_dek",
    )

    def __init__(
        self,
        *,
        envelope_version: int,
        key_id: str,
        key_version: int,
        data_nonce: bytes,
        ciphertext: bytes,
        wrap_nonce: bytes,
        wrapped_dek: bytes,
    ) -> None:
        self.envelope_version = envelope_version
        self.key_id = key_id
        self.key_version = key_version
        self.data_nonce = data_nonce
        self.ciphertext = ciphertext
        self.wrap_nonce = wrap_nonce
        self.wrapped_dek = wrapped_dek

    def __repr__(self) -> str:
        return (
            f"EnvelopeCiphertext(envelope_version={self.envelope_version}, "
            f"key_id={self.key_id!r}, key_version={self.key_version})"
        )


class LocalEnvelopeCipher:
    """纯密码学层：DEK 加密 plaintext，KEK 包装 DEK（AES-GCM，无状态）。"""

    @classmethod
    def encrypt(
        cls,
        *,
        plaintext: bytes,
        aad: bytes,
        keyring: Keyring,
        wrap_aad: bytes = b"",
    ) -> EnvelopeCiphertext:
        dek = os.urandom(_DEK_SIZE)
        data_nonce = os.urandom(_NONCE_SIZE)
        ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext, aad)
        active = keyring.active()
        wrap_nonce = os.urandom(_NONCE_SIZE)
        wrapped_dek = AESGCM(active.key_material).encrypt(wrap_nonce, dek, wrap_aad)
        return EnvelopeCiphertext(
            envelope_version=_ENVELOPE_VERSION,
            key_id=active.key_id,
            key_version=active.key_version,
            data_nonce=data_nonce,
            ciphertext=ciphertext,
            wrap_nonce=wrap_nonce,
            wrapped_dek=wrapped_dek,
        )

    @classmethod
    def decrypt(
        cls,
        *,
        envelope: EnvelopeCiphertext,
        aad: bytes,
        keyring: Keyring,
        wrap_aad: bytes = b"",
    ) -> bytes:
        if envelope.envelope_version != _ENVELOPE_VERSION:
            raise SecretIntegrityError("unsupported envelope version")
        entry = keyring.get(envelope.key_id)
        if entry is None:
            raise SecretIntegrityError("envelope key is not present in keyring")
        try:
            dek = AESGCM(entry.key_material).decrypt(
                envelope.wrap_nonce, envelope.wrapped_dek, wrap_aad
            )
        except InvalidTag as exc:
            raise SecretIntegrityError("wrapped dek integrity check failed") from exc
        try:
            return AESGCM(dek).decrypt(envelope.data_nonce, envelope.ciphertext, aad)
        except InvalidTag as exc:
            raise SecretIntegrityError("ciphertext integrity check failed") from exc


class LocalSecretBackend(SecretBackend):
    """PostgreSQL-backed envelope store（zhiwei_identity 引擎），持 keyring。

    业务层只通过 SecretBackend port 使用本类；需要与外部数据库事务原子提交的
    写入（refresh 的 envelope 改写 + auth_sessions 完成）走 put_in_session，
    由 persistence/UoW adapter（LocalSessionRefreshUnitOfWork）组合——port 本身
    不暴露任何数据库/session 参数（验收阻断 3，S4 Vault/KMS adapter 可替换）。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], keyring: Keyring) -> None:
        self._session_factory = session_factory
        self._keyring = keyring

    @property
    def keyring(self) -> Keyring:
        return self._keyring

    async def put(
        self,
        ref: SecretRef,
        plaintext: bytes,
        aad: bytes,
        purpose: str,
        expected_version: int | None = None,
    ) -> SecretEnvelopeMeta:
        """创建或替换 envelope（自有事务）；port 契约不含 external_session 等参数。"""
        params = self._encrypt_params(ref, plaintext, aad, purpose)
        async with self._session_factory() as session, session.begin():
            return await self._put_in_session(session, params, expected_version)

    async def put_in_session(
        self,
        session: AsyncSession,
        ref: SecretRef,
        plaintext: bytes,
        aad: bytes,
        purpose: str,
        expected_version: int | None = None,
    ) -> SecretEnvelopeMeta:
        """在调用方事务内写 envelope（与 session 完成同原子边界，UoW adapter 专用）。

        类型化 SQLAlchemy session 只出现在本 local 实现与 persistence adapter 边界；
        SecretBackend port 不包含该参数（验收阻断 3）。
        """
        params = self._encrypt_params(ref, plaintext, aad, purpose)
        return await self._put_in_session(session, params, expected_version)

    def _encrypt_params(
        self, ref: SecretRef, plaintext: bytes, aad: bytes, purpose: str
    ) -> dict[str, Any]:
        """纯加密：用活动 KEK 包 DEK 并加密 plaintext，产出 INSERT 参数。"""
        envelope = LocalEnvelopeCipher.encrypt(
            plaintext=plaintext,
            aad=aad,
            keyring=self._keyring,
            wrap_aad=_wrap_aad(purpose, ref),
        )
        return {
            "ref": ref.value,
            "purpose": purpose,
            "ev": envelope.envelope_version,
            "kid": envelope.key_id,
            "kver": envelope.key_version,
            "dn": envelope.data_nonce,
            "wd": envelope.wrapped_dek,
            "wn": envelope.wrap_nonce,
            "ct": envelope.ciphertext,
        }

    @staticmethod
    async def _put_in_session(
        session: Any, params: dict[str, Any], expected_version: int | None
    ) -> SecretEnvelopeMeta:
        if expected_version is None:
            # 创建或替换（版本递增）：单语句 ON CONFLICT，revoked 行拒绝替换。
            # 不用「INSERT 失败后 rollback 再 UPDATE」——那会在 begin() 上下文里
            # 关闭事务，SQLAlchemy 抛 InvalidRequestError。
            result = await session.execute(
                text(
                    "INSERT INTO secret_envelopes "
                    "(ref, purpose, version, envelope_version, key_id, key_version, "
                    " data_nonce, wrapped_dek, wrap_nonce, ciphertext, schema_version) "
                    "VALUES (:ref, :purpose, 1, :ev, :kid, :kver, :dn, :wd, :wn, :ct, 1) "
                    "ON CONFLICT (ref) DO UPDATE SET "
                    " purpose = EXCLUDED.purpose, "
                    " envelope_version = EXCLUDED.envelope_version, "
                    " key_id = EXCLUDED.key_id, key_version = EXCLUDED.key_version, "
                    " data_nonce = EXCLUDED.data_nonce, wrapped_dek = EXCLUDED.wrapped_dek, "
                    " wrap_nonce = EXCLUDED.wrap_nonce, ciphertext = EXCLUDED.ciphertext, "
                    " version = secret_envelopes.version + 1 "
                    "WHERE secret_envelopes.revoked_at IS NULL "
                    "RETURNING ref, purpose, version, envelope_version, key_id, "
                    "key_version, created_at, revoked_at"
                ),
                params,
            )
        else:
            result = await session.execute(
                text(
                    "UPDATE secret_envelopes SET "
                    " purpose = :purpose, envelope_version = :ev, key_id = :kid, "
                    " key_version = :kver, data_nonce = :dn, wrapped_dek = :wd, "
                    " wrap_nonce = :wn, ciphertext = :ct, version = version + 1 "
                    "WHERE ref = :ref AND version = :expected AND revoked_at IS NULL "
                    "RETURNING ref, purpose, version, envelope_version, key_id, "
                    "key_version, created_at, revoked_at"
                ),
                {**params, "expected": expected_version},
            )
        meta = result.mappings().first()
        if meta is None:
            raise SecretVersionConflictError("expected_version CAS failed") from None
        return _meta_from_mapping(meta)

    async def get(self, ref: SecretRef, aad: bytes) -> bytes:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "SELECT ref, purpose, version, envelope_version, key_id, key_version, "
                    "data_nonce, wrapped_dek, wrap_nonce, ciphertext "
                    "FROM secret_envelopes WHERE ref = :ref AND revoked_at IS NULL"
                ),
                {"ref": ref.value},
            )
            row = result.mappings().first()
        if row is None:
            raise SecretRevokedError("secret is revoked or does not exist")
        envelope = EnvelopeCiphertext(
            envelope_version=row["envelope_version"],
            key_id=row["key_id"],
            key_version=row["key_version"],
            data_nonce=row["data_nonce"],
            ciphertext=row["ciphertext"],
            wrap_nonce=row["wrap_nonce"],
            wrapped_dek=row["wrapped_dek"],
        )
        return LocalEnvelopeCipher.decrypt(
            envelope=envelope,
            aad=aad,
            keyring=self._keyring,
            wrap_aad=_wrap_aad(row["purpose"], ref),
        )

    async def revoke(self, ref: SecretRef) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE secret_envelopes SET revoked_at = now() "
                    "WHERE ref = :ref AND revoked_at IS NULL"
                ),
                {"ref": ref.value},
            )

    async def rewrap(self, ref: SecretRef, aad: bytes, expected_version: int) -> SecretEnvelopeMeta:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "SELECT purpose, version, envelope_version, key_id, key_version, "
                    "data_nonce, wrapped_dek, wrap_nonce, ciphertext FROM secret_envelopes "
                    "WHERE ref = :ref AND revoked_at IS NULL"
                ),
                {"ref": ref.value},
            )
            row = result.mappings().first()
            if row is None:
                raise SecretRevokedError("secret is revoked or does not exist")
            envelope = EnvelopeCiphertext(
                envelope_version=row["envelope_version"],
                key_id=row["key_id"],
                key_version=row["key_version"],
                data_nonce=row["data_nonce"],
                ciphertext=row["ciphertext"],
                wrap_nonce=row["wrap_nonce"],
                wrapped_dek=row["wrapped_dek"],
            )
            wrap_aad = _wrap_aad(row["purpose"], ref)
            # 只重包 DEK：先用旧 KEK 解开，再用当前活动 KEK 重包；ciphertext 不动
            entry = self._keyring.get(envelope.key_id)
            if entry is None:
                raise SecretIntegrityError("envelope key is not present in keyring")
            try:
                dek = AESGCM(entry.key_material).decrypt(
                    envelope.wrap_nonce, envelope.wrapped_dek, wrap_aad
                )
            except InvalidTag as exc:
                raise SecretIntegrityError("wrapped dek integrity check failed") from exc
            active = self._keyring.active()
            new_wrap_nonce = os.urandom(_NONCE_SIZE)
            new_wrapped_dek = AESGCM(active.key_material).encrypt(
                new_wrap_nonce, dek, wrap_aad
            )
            updated = await session.execute(
                text(
                    "UPDATE secret_envelopes SET wrapped_dek = :wd, wrap_nonce = :wn, "
                    "key_id = :kid, key_version = :kver, version = version + 1 "
                    "WHERE ref = :ref AND version = :expected AND revoked_at IS NULL RETURNING "
                    "ref, purpose, version, envelope_version, key_id, key_version, "
                    "created_at, revoked_at"
                ),
                {
                    "wd": new_wrapped_dek,
                    "wn": new_wrap_nonce,
                    "kid": active.key_id,
                    "kver": active.key_version,
                    "ref": ref.value,
                    "expected": expected_version,
                },
            )
            meta = updated.mappings().first()
            if meta is None:
                raise SecretVersionConflictError("expected_version CAS failed during rewrap")
            return _meta_from_mapping(meta)

    def rotate(self, *, key_id: str | None = None, key_material: bytes | None = None) -> str:
        """追加新活动 key（内存态）。重启前 operator 须把新 key 写入 keyring 文件。"""
        if key_material is not None and len(key_material) != _DEK_SIZE:
            raise ValueError("key material must be 32 bytes")
        material = key_material or os.urandom(_DEK_SIZE)
        new_version = max(e.key_version for e in self._keyring.entries.values()) + 1
        new_id = key_id or f"k{new_version}"
        entry = KeyringEntry(new_id, new_version, material)
        self._keyring = self._keyring.with_added(entry)
        return new_id


def _wrap_aad(purpose: str, ref: SecretRef) -> bytes:
    """wrap AAD：绑定 purpose + ref，防止跨行交换 wrapped_dek。"""
    return canonical_json({"purpose": purpose, "ref": ref.value})


def _meta_from_mapping(row: Any) -> SecretEnvelopeMeta:
    return SecretEnvelopeMeta(
        ref=row["ref"],
        purpose=row["purpose"],
        version=row["version"],
        envelope_version=row["envelope_version"],
        key_id=row["key_id"],
        key_version=row["key_version"],
        created_at=row["created_at"],
        revoked_at=row.get("revoked_at"),
    )
