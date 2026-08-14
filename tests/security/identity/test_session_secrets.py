"""S1-T2 RED：本地 PostgreSQL-backed AES-GCM envelope backend 与 no-secret 扫描。

设计/验收方冻结（A 档）：
- 相同 plaintext/AAD 两次 put 得到不同 nonce/ciphertext；正确 AAD + 重启可解密；
- AAD 字段（purpose/session/issuer/subject/version）任一变化均无法解密；
- ciphertext / wrapped DEK / nonce / tag 篡改全部抛统一 SecretIntegrityError；
- revoke 后拒绝解密；key rotation/rewrap 后新 key 可解密，移除旧 key 后仍可解密，
  旧 version CAS 失败；
- SecretRef / envelope / backend 的 repr 不得暴露 plaintext、key 或 ciphertext；
- sentinel 扫描：PG 可见字段、caplog、异常、trace stub、model repr 出现即失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from zhiwei.identity.domain import TokenAAD
from zhiwei.secrets.base import (
    SecretIntegrityError,
    SecretRef,
    SecretRevokedError,
    SecretVersionConflictError,
)
from zhiwei.secrets.local import LocalEnvelopeCipher, LocalSecretBackend, load_keyring

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
IDENTITY_DSN = os.environ.get(
    "ZHIWEI_TEST_IDENTITY_DSN", "postgresql://zhiwei_identity@127.0.0.1:55432/zhiwei_test"
)

ACCESS_SENTINEL = b"ZW_TEST_ACCESS_TOKEN_A7F3"
REFRESH_SENTINEL = b"ZW_TEST_REFRESH_TOKEN_B8E4"
MASTER_KEY_SENTINEL = "ZW_TEST_MASTER_KEY_D0E6"
CLIENT_SECRET_SENTINEL = "ZW_TEST_CLIENT_SECRET_C9D5"
ALL_SENTINELS = (
    b"ZW_TEST_ACCESS_TOKEN_A7F3",
    b"ZW_TEST_REFRESH_TOKEN_B8E4",
    b"ZW_TEST_CLIENT_SECRET_C9D5",
    b"ZW_TEST_MASTER_KEY_D0E6",
)

ENVELOPE_TABLES = ("auth_sessions", "oidc_login_attempts", "secret_envelopes")


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1))
    config.attributes["database_url"] = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    return config


async def _assert_safe_test_database(dsn: str) -> None:
    url = make_url(dsn)
    if url.database != "zhiwei_test" or url.username != "zhiwei_migrator":
        raise RuntimeError("destructive migration tests require the dedicated zhiwei_test database")
    connection = await asyncpg.connect(dsn)
    try:
        database, user = await connection.fetchrow("SELECT current_database(), current_user")
        if database != "zhiwei_test" or user != "zhiwei_migrator":
            raise RuntimeError("connected database identity is not the dedicated migration test target")
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[None]:
    asyncio.run(_assert_safe_test_database(ADMIN_DSN))
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


@pytest.fixture(scope="function")
def identity_sessions() -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        IDENTITY_DSN.replace("postgresql://", "postgresql+asyncpg://", 1),
        poolclass=NullPool,
    )
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(engine.dispose())


def _write_keyring(path: Path, seeds: list[tuple[str, bytes]]) -> None:
    lines = []
    for key_id, seed in seeds:
        material = hashlib.sha256(seed).digest()
        import base64

        lines.append(f"{key_id}={base64.b64encode(material).decode('ascii')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_backend(
    identity_sessions: async_sessionmaker[AsyncSession],
    keyring_path: Path,
) -> LocalSecretBackend:
    return LocalSecretBackend(
        session_factory=identity_sessions, keyring=load_keyring(keyring_path)
    )


def _token_aad(**overrides: Any) -> bytes:
    values: dict[str, Any] = {
        "purpose": "oidc_session",
        "session_id": uuid4(),
        "issuer": "https://idp.example.com",
        "subject": "alice",
        "session_version": 1,
        "schema_version": 1,
    }
    values.update(overrides)
    return TokenAAD(**values).encode()


def _assert_scan_clean(*texts: str, sentinels: tuple[bytes, ...] = ALL_SENTINELS) -> None:
    for text in texts:
        for sentinel in sentinels:
            assert sentinel.decode("utf-8") not in text, f"sentinel {sentinel!r} 出现在输出中"


# --------------------------------------------------------------------------- B. envelope 行为


@pytest.mark.asyncio
async def test_identical_puts_produce_different_nonce_and_ciphertext(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("k1", b"seed-one")])
    backend = _make_backend(identity_sessions, keyring_path)
    plaintext = b"identical-plaintext"
    aad = _token_aad()
    await backend.put(SecretRef("session:first"), plaintext, aad, purpose="oidc_session")
    await backend.put(SecretRef("session:second"), plaintext, aad, purpose="oidc_session")

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        first_row = await connection.fetchrow(
            "SELECT data_nonce, ciphertext, wrap_nonce, wrapped_dek FROM secret_envelopes WHERE ref = $1",
            "session:first",
        )
        second_row = await connection.fetchrow(
            "SELECT data_nonce, ciphertext, wrap_nonce, wrapped_dek FROM secret_envelopes WHERE ref = $1",
            "session:second",
        )
    finally:
        await connection.close()

    assert first_row is not None and second_row is not None
    assert first_row["data_nonce"] != second_row["data_nonce"]
    assert first_row["ciphertext"] != second_row["ciphertext"]
    assert first_row["wrap_nonce"] != second_row["wrap_nonce"]
    assert first_row["wrapped_dek"] != second_row["wrapped_dek"]


@pytest.mark.asyncio
async def test_decrypt_after_restart_with_same_keyring(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("k1", b"seed-one")])
    backend = _make_backend(identity_sessions, keyring_path)
    aad = _token_aad()
    await backend.put(SecretRef("session:restart"), b"durable-plaintext", aad, purpose="oidc_session")

    restarted = _make_backend(identity_sessions, keyring_path)
    assert await restarted.get(SecretRef("session:restart"), aad) == b"durable-plaintext"


@pytest.mark.asyncio
async def test_any_aad_field_change_breaks_decryption(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("k1", b"seed-one")])
    backend = _make_backend(identity_sessions, keyring_path)
    ref = SecretRef("session:aad")
    await backend.put(ref, b"aad-bound-plaintext", _token_aad(), purpose="oidc_session")

    for overrides, _label in [
        ({"purpose": "connection_credential"}, "purpose"),
        ({"session_id": uuid4()}, "session_id"),
        ({"issuer": "https://other.example.com"}, "issuer"),
        ({"subject": "mallory"}, "subject"),
        ({"session_version": 2}, "session_version"),
        ({"schema_version": 2}, "schema_version"),
    ]:
        with pytest.raises(SecretIntegrityError) as exc:
            await backend.get(ref, _token_aad(**overrides))
        _assert_scan_clean(str(exc.value), traceback.format_exc())


@pytest.mark.asyncio
async def test_tampering_any_component_fails_closed(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("k1", b"seed-one")])
    backend = _make_backend(identity_sessions, keyring_path)
    aad = _token_aad()
    ref = SecretRef("session:tamper")
    await backend.put(ref, b"tamper-target", aad, purpose="oidc_session")

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await connection.fetchrow(
            "SELECT data_nonce, ciphertext, wrap_nonce, wrapped_dek FROM secret_envelopes WHERE ref = $1",
            "session:tamper",
        )
        flipped = {name: bytes([value[0] ^ 0xFF]) + value[1:] for name, value in row.items()}
    finally:
        await connection.close()

    for column, tampered in flipped.items():
        connection = await asyncpg.connect(ADMIN_DSN)
        try:
            await connection.execute(
                f"UPDATE secret_envelopes SET {column} = $1 WHERE ref = 'session:tamper'",
                tampered,
            )
        finally:
            await connection.close()
        with pytest.raises(SecretIntegrityError) as exc:
            await backend.get(ref, aad)
        _assert_scan_clean(str(exc.value), traceback.format_exc())


@pytest.mark.asyncio
async def test_revoked_secret_refuses_decryption(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("k1", b"seed-one")])
    backend = _make_backend(identity_sessions, keyring_path)
    aad = _token_aad()
    ref = SecretRef("session:revoke")
    await backend.put(ref, b"revocable", aad, purpose="oidc_session")
    await backend.revoke(ref)
    with pytest.raises(SecretRevokedError):
        await backend.get(ref, aad)


@pytest.mark.asyncio
async def test_rotation_rewrap_and_old_key_removal(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("old-key", b"old-seed")])
    backend = _make_backend(identity_sessions, keyring_path)
    aad = _token_aad()
    ref = SecretRef("session:rotate")
    await backend.put(ref, b"rotatable", aad, purpose="oidc_session")

    new_key_id = backend.rotate(
        key_id="new-key", key_material=hashlib.sha256(b"new-seed").digest()
    )
    assert new_key_id == "new-key"
    assert backend.keyring.active_key_id == "new-key"
    await backend.rewrap(ref, aad, expected_version=1)
    assert await backend.get(ref, aad) == b"rotatable"

    # 移除旧 key 后（keyring 只剩新 key）仍可解密
    _write_keyring(keyring_path, [(new_key_id, b"new-seed")])
    restarted = _make_backend(identity_sessions, keyring_path)
    assert await restarted.get(ref, aad) == b"rotatable"


@pytest.mark.asyncio
async def test_rewrap_with_stale_expected_version_fails(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("old-key", b"old-seed")])
    backend = _make_backend(identity_sessions, keyring_path)
    aad = _token_aad()
    ref = SecretRef("session:cas")
    await backend.put(ref, b"cas-target", aad, purpose="oidc_session")
    backend.rotate()
    await backend.rewrap(ref, aad, expected_version=1)
    with pytest.raises(SecretVersionConflictError):
        await backend.rewrap(ref, aad, expected_version=1)
    # 篡改后行内容保持 v2 可解密（旧 CAS 不得覆盖新版本）
    assert await backend.get(ref, aad) == b"cas-target"


@pytest.mark.asyncio
async def test_secret_models_and_reprs_never_expose_material(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("k1", b"seed-one")])
    backend = _make_backend(identity_sessions, keyring_path)
    aad = _token_aad()
    ref = SecretRef("session:repr")
    await backend.put(ref, ACCESS_SENTINEL, aad, purpose="oidc_session")

    _assert_scan_clean(
        repr(ref),
        str(ref),
        repr(backend),
        repr(backend.keyring),
        repr(load_keyring(keyring_path)),
    )


# --------------------------------------------------------------------------- F. no-secret 扫描


@pytest.mark.asyncio
async def test_pg_visible_fields_never_contain_sentinels(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [(MASTER_KEY_SENTINEL, MASTER_KEY_SENTINEL.encode())])
    backend = _make_backend(identity_sessions, keyring_path)
    aad = _token_aad()
    await backend.put(
        SecretRef("session:scan"),
        ACCESS_SENTINEL + b"|" + REFRESH_SENTINEL,
        aad,
        purpose="oidc_session",
    )
    recovered = await backend.get(SecretRef("session:scan"), aad)
    assert recovered == ACCESS_SENTINEL + b"|" + REFRESH_SENTINEL

    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        for table in ENVELOPE_TABLES:
            rows = await connection.fetch(f"SELECT row_to_json(t)::text AS dump FROM {table} AS t")
            for row in rows:
                _assert_scan_clean(row["dump"])
            bytea_columns = await connection.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = $1 AND data_type = 'bytea'",
                table,
            )
            for column in bytea_columns:
                values = await connection.fetch(f"SELECT encode({column['column_name']}, 'hex') AS hex FROM {table}")
                for value in values:
                    hexdump = value["hex"]
                    for sentinel in ALL_SENTINELS:
                        assert sentinel.hex() not in hexdump
    finally:
        await connection.close()

    _assert_scan_clean(caplog.text)


@pytest.mark.asyncio
async def test_decrypted_plaintext_never_enters_logs_or_exceptions(
    migrated_database: None,
    identity_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("k1", b"seed-one")])
    backend = _make_backend(identity_sessions, keyring_path)
    ref = SecretRef("session:log-scan")
    await backend.put(ref, ACCESS_SENTINEL, _token_aad(), purpose="oidc_session")
    await backend.get(ref, _token_aad())

    _assert_scan_clean(caplog.text)
    _assert_scan_clean(str(CLIENT_SECRET_SENTINEL))


@pytest.mark.asyncio
async def test_envelope_cipher_pure_layer_scan(tmp_path: Path) -> None:
    keyring_path = tmp_path / "master.key"
    _write_keyring(keyring_path, [("k1", b"seed-one")])
    keyring = load_keyring(keyring_path)
    envelope = LocalEnvelopeCipher.encrypt(
        plaintext=ACCESS_SENTINEL, aad=b"aad", keyring=keyring
    )
    _assert_scan_clean(repr(envelope), str(envelope))
    assert LocalEnvelopeCipher.decrypt(envelope=envelope, aad=b"aad", keyring=keyring) == ACCESS_SENTINEL
    with pytest.raises(SecretIntegrityError):
        LocalEnvelopeCipher.decrypt(envelope=envelope, aad=b"tampered", keyring=keyring)
