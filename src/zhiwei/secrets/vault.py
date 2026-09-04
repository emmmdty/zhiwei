"""Vault Transit secrets engine backend (S4) behind S1 SecretBackend port。

实现 SecretBackend Protocol，使用 httpx 调用 HashiCorp Vault Transit API。
不依赖 hvac 或其他 Vault SDK；只用 httpx（已在 pyproject.toml 依赖中）。

认证方式：静态 token（环境变量 / 构造参数），不做 OAuth/OIDC。
密钥管理：使用 Vault Transit named key，由 Vault 负责 key rotation。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import httpx2 as httpx

from zhiwei.contracts.canonical import canonical_json
from zhiwei.secrets.base import (
    SecretBackend,
    SecretEnvelopeMeta,
    SecretIntegrityError,
    SecretRef,
    SecretRevokedError,
    SecretVersionConflictError,
)


class VaultConnectionError(Exception):
    """Vault 不可达或返回非 2xx 响应。"""


class VaultTransitBackend(SecretBackend):
    """Vault Transit 实现；key rotation 由 Vault 原生 Transit engine 处理。"""

    def __init__(
        self,
        *,
        vault_url: str,
        token: str,
        mount_point: str = "transit",
        key_name: str = "zhiwei",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._vault_url = vault_url.rstrip("/")
        self._token = token
        self._mount_point = mount_point
        self._key_name = key_name
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_client = http_client is None
        self._in_memory_store: dict[str, dict[str, Any]] = {}

    @property
    def _base_url(self) -> str:
        return f"{self._vault_url}/v1/{self._mount_point}"

    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self._token}

    async def aclose(self) -> None:
        """释放自建的 httpx 连接池（外部注入的由调用方负责）。"""
        if self._owns_client:
            await self._client.aclose()

    async def put(
        self,
        ref: SecretRef,
        plaintext: bytes,
        aad: bytes,
        purpose: str,
        expected_version: int | None = None,
    ) -> SecretEnvelopeMeta:
        wrap_aad = _wrap_aad(purpose, ref)
        encrypt_url = f"{self._base_url}/encrypt/{self._key_name}"
        try:
            resp = await self._client.post(
                encrypt_url,
                headers=self._headers(),
                json={
                    "plaintext": base64.urlsafe_b64encode(plaintext).rstrip(b"=").decode("ascii"),
                    "context": base64.urlsafe_b64encode(wrap_aad).rstrip(b"=").decode("ascii"),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise VaultConnectionError(f"Vault encrypt failed: {exc}") from exc
        ciphertext = resp.json()["data"]["ciphertext"]

        existing = self._in_memory_store.get(ref.value)
        if expected_version is not None:
            if existing is None or existing.get("version") != expected_version:
                raise SecretVersionConflictError("expected_version CAS failed")
            version = existing["version"] + 1
        elif existing is not None:
            version = existing["version"] + 1
        else:
            version = 1

        self._in_memory_store[ref.value] = {
            "ciphertext": ciphertext,
            "purpose": purpose,
            "version": version,
            "key_version": _extract_key_version(ciphertext),
            "revoked": False,
        }
        return SecretEnvelopeMeta(
            ref=ref.value,
            purpose=purpose,
            version=version,
            envelope_version=1,
            key_id=self._key_name,
            key_version=_extract_key_version(ciphertext),
            created_at=datetime.now(UTC),
        )

    async def get(self, ref: SecretRef, aad: bytes) -> bytes:
        stored = self._in_memory_store.get(ref.value)
        if stored is None or stored.get("revoked"):
            raise SecretRevokedError("secret is revoked or does not exist")
        decrypt_url = f"{self._base_url}/decrypt/{self._key_name}"
        try:
            resp = await self._client.post(
                decrypt_url,
                headers=self._headers(),
                json={
                    "ciphertext": stored["ciphertext"],
                    "context": base64.urlsafe_b64encode(
                        _wrap_aad(stored["purpose"], ref)
                    ).rstrip(b"=").decode("ascii"),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SecretIntegrityError(f"Vault decrypt failed: {exc}") from exc
        encoded = resp.json()["data"]["plaintext"]
        return base64.urlsafe_b64decode(encoded + "===")

    async def revoke(self, ref: SecretRef) -> None:
        stored = self._in_memory_store.get(ref.value)
        if stored is not None:
            stored["revoked"] = True
        revoke_url = f"{self._base_url}/revoke/{self._key_name}"
        try:
            resp = await self._client.post(
                revoke_url,
                headers=self._headers(),
                json={"context": base64.urlsafe_b64encode(
                    canonical_json({"ref": ref.value})
                ).rstrip(b"=").decode("ascii")},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            pass

    async def rewrap(
        self, ref: SecretRef, aad: bytes, expected_version: int
    ) -> SecretEnvelopeMeta:
        stored = self._in_memory_store.get(ref.value)
        if stored is None or stored.get("revoked"):
            raise SecretRevokedError("secret is revoked or does not exist")
        if stored["version"] != expected_version:
            raise SecretVersionConflictError("expected_version CAS failed")
        rewrap_url = f"{self._base_url}/rewrap/{self._key_name}"
        try:
            resp = await self._client.post(
                rewrap_url,
                headers=self._headers(),
                json={
                    "ciphertext": stored["ciphertext"],
                    "context": base64.urlsafe_b64encode(
                        _wrap_aad(stored["purpose"], ref)
                    ).rstrip(b"=").decode("ascii"),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise VaultConnectionError(f"Vault rewrap failed: {exc}") from exc
        new_ciphertext = resp.json()["data"]["ciphertext"]
        stored["ciphertext"] = new_ciphertext
        stored["version"] = expected_version + 1
        stored["key_version"] = _extract_key_version(new_ciphertext)
        return SecretEnvelopeMeta(
            ref=ref.value,
            purpose=stored["purpose"],
            version=stored["version"],
            envelope_version=1,
            key_id=self._key_name,
            key_version=stored["key_version"],
            created_at=datetime.now(UTC),
        )

    def rotate(self, *, key_id: str | None = None, key_material: bytes | None = None) -> str:
        """Vault Transit 原生 rotation；仅验证参数有效性。"""
        if key_material is not None and len(key_material) != 32:
            raise ValueError("key material must be 32 bytes")
        return key_id or self._key_name


def _extract_key_version(ciphertext: str) -> int:
    """从 Vault ciphertext 格式 v1:<version>:<base64> 中提取 key version。"""
    parts = ciphertext.split(":")
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return 1


def _wrap_aad(purpose: str, ref: SecretRef) -> bytes:
    return canonical_json({"purpose": purpose, "ref": ref.value})
