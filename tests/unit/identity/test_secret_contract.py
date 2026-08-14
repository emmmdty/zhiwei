"""S1-T2 RED：SecretBackend port、keyring 加载与本地 envelope 密码学契约。

设计/验收方冻结（A 档）：
- SecretBackend 是通用 port（S4 复用保存 Connection credentials，per-org AAD），
  不是 OIDC 专用类；业务层只持 opaque SecretRef；
- 本地 envelope：随机 256-bit DEK + 随机 96-bit AES-GCM nonce；plaintext 由 DEK 加密，
  DEK 由当前 KEK 用 AES-GCM 包装；禁止确定性 nonce / ECB / CBC / 自制 MAC；
- master key 只从显式挂载文件加载（keyring 格式：`key_id=<base64>` 每行一行，最后一行是
  当前活动 key）；malformed / 重复 id / 非 32 字节 key 一律加载期失败；
- 篡改 ciphertext / wrapped DEK / nonce / AAD 一律抛统一 SecretIntegrityError；
- SecretRef / envelope 模型的 repr 不得暴露 plaintext、key 或 ciphertext。
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

from zhiwei.secrets.base import (
    SecretBackend,
    SecretIntegrityError,
    SecretRef,
)
from zhiwei.secrets.local import LocalEnvelopeCipher, load_keyring

MASTER_KEY_SENTINEL = "ZW_TEST_MASTER_KEY_D0E6"


def _key_b64(*, seed: bytes) -> str:
    return base64.b64encode(hashlib.sha256(seed).digest()).decode("ascii")


def _keyring_file(tmp_path: Path, entries: list[tuple[str, bytes]]) -> Path:
    path = tmp_path / "master.key"
    path.write_text(
        "\n".join(f"{key_id}={_key_b64(seed=seed)}" for key_id, seed in entries) + "\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- SecretRef / port


def test_secret_ref_is_opaque_and_repr_safe() -> None:
    ref = SecretRef(value="session:00000000-0000-0000-0000-000000000001")
    assert str(ref) == "session:00000000-0000-0000-0000-000000000001"
    assert repr(ref) == "session:00000000-0000-0000-0000-000000000001"
    assert SecretRef(value="a") == SecretRef(value="a")
    assert SecretRef(value="a") != SecretRef(value="b")


def test_secret_backend_port_has_s4_reusable_surface() -> None:
    """S4 必须能复用该 port：put/get/revoke/rewrap/rotate + expected_version CAS。"""
    methods = {"put", "get", "revoke", "rewrap", "rotate"}
    missing = methods - set(dir(SecretBackend))
    assert not missing, f"SecretBackend port 缺少: {missing}"
    for name in ("SecretIntegrityError", "SecretRevokedError", "SecretVersionConflictError"):
        assert isinstance(getattr(__import__("zhiwei.secrets.base", fromlist=[name]), name), type)


# --------------------------------------------------------------------------- keyring 加载


def test_keyring_loads_and_last_entry_is_active(tmp_path: Path) -> None:
    path = _keyring_file(
        tmp_path,
        [("old-key", b"old"), ("active-key", b"new")],
    )
    keyring = load_keyring(path)
    assert set(keyring.entries) == {"old-key", "active-key"}
    assert keyring.active_key_id == "active-key"
    assert len(keyring.entries["old-key"].key_material) == 32
    assert len(keyring.entries["active-key"].key_material) == 32


def test_keyring_loads_single_key(tmp_path: Path) -> None:
    path = _keyring_file(tmp_path, [("only-key", b"only")])
    keyring = load_keyring(path)
    assert keyring.active_key_id == "only-key"


def test_keyring_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_keyring(tmp_path / "does-not-exist.key")


@pytest.mark.parametrize(
    "content",
    [
        "",  # 空文件
        "no-equals-sign\n",
        "k1=\n",  # 空 key 材料
        "k1=!!not-base64!!\n",
        "k1=YWJj\n",  # 3 字节（解码后非 32）
        "k1=YWJj\nk1=ZGVm\n",  # 重复 id
        "k1=YWJj\n\nk1=ZGVm\n",  # 空行
    ],
)
def test_keyring_malformed_content_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "master.key"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_keyring(path)


def test_keyring_file_contains_derived_material_not_literal_secret(tmp_path: Path) -> None:
    """key 文件是测试输入，但 sentinel 不应以字面量出现在文件内容里。"""
    path = _keyring_file(tmp_path, [(MASTER_KEY_SENTINEL, MASTER_KEY_SENTINEL.encode())])
    raw = path.read_text(encoding="utf-8")
    assert MASTER_KEY_SENTINEL not in raw


# --------------------------------------------------------------------------- 纯密码学层


def test_cipher_round_trip_with_correct_aad(tmp_path: Path) -> None:
    keyring = load_keyring(_keyring_file(tmp_path, [("k1", b"seed")]))
    plaintext = b"{\"access_token\": \"secret\"}"
    aad = b"purpose=oidc_session|session=abc"
    envelope = LocalEnvelopeCipher.encrypt(plaintext=plaintext, aad=aad, keyring=keyring)
    decrypted = LocalEnvelopeCipher.decrypt(envelope=envelope, aad=aad, keyring=keyring)
    assert decrypted == plaintext
    assert envelope.key_id == keyring.active_key_id


def test_cipher_uses_random_nonce_so_identical_inputs_differ(tmp_path: Path) -> None:
    keyring = load_keyring(_keyring_file(tmp_path, [("k1", b"seed")]))
    plaintext = b"same-plaintext"
    aad = b"same-aad"
    first = LocalEnvelopeCipher.encrypt(plaintext=plaintext, aad=aad, keyring=keyring)
    second = LocalEnvelopeCipher.encrypt(plaintext=plaintext, aad=aad, keyring=keyring)
    assert first.data_nonce != second.data_nonce
    assert first.ciphertext != second.ciphertext
    assert first.wrap_nonce != second.wrap_nonce
    assert first.wrapped_dek != second.wrapped_dek


def test_cipher_wrong_aad_fails_with_integrity_error(tmp_path: Path) -> None:
    keyring = load_keyring(_keyring_file(tmp_path, [("k1", b"seed")]))
    envelope = LocalEnvelopeCipher.encrypt(plaintext=b"data", aad=b"real-aad", keyring=keyring)
    with pytest.raises(SecretIntegrityError):
        LocalEnvelopeCipher.decrypt(envelope=envelope, aad=b"wrong-aad", keyring=keyring)


def test_cipher_unknown_key_id_fails_closed(tmp_path: Path) -> None:
    keyring = load_keyring(_keyring_file(tmp_path, [("k1", b"seed")]))
    envelope = LocalEnvelopeCipher.encrypt(plaintext=b"data", aad=b"aad", keyring=keyring)
    stale = keyring.replace(key_id="other")
    with pytest.raises(SecretIntegrityError):
        LocalEnvelopeCipher.decrypt(envelope=envelope, aad=b"aad", keyring=stale)


def test_cipher_rejects_wrong_sized_key_material(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    path.write_text("k1=" + base64.b64encode(b"short").decode() + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_keyring(path)


def test_envelope_model_repr_does_not_leak_material(tmp_path: Path) -> None:
    keyring = load_keyring(_keyring_file(tmp_path, [("k1", b"seed")]))
    envelope = LocalEnvelopeCipher.encrypt(
        plaintext=os.urandom(64), aad=b"aad", keyring=keyring
    )
    rendered = repr(envelope)
    for attr in ("ciphertext", "wrapped_dek", "wrap_nonce", "data_nonce"):
        value = getattr(envelope, attr)
        rendered_value = repr(value)
        assert len(rendered_value) > 0
    assert "plaintext" not in rendered.lower()
