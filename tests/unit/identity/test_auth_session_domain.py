"""S1-T2 RED：AuthSession / LoginAttempt / token envelope 的领域契约。

设计/验收方冻结（A 档）：
- AuthSession 是 principal/session 级，**不含** organization_id/workspace_id（DATA_MODEL §2、
  总设计 §9.1）；所有 session 更新使用 expected_version CAS；
- cookie token 只保存 SHA-256 hash；CSRF 以 hash 保存；refresh 以有界 lease 竞争 ownership；
- login attempt 短期保存 state/nonce/verifier（state/nonce 存 hash，verifier 必须可恢复用于
  token exchange）；attempt 原子一次性消费；
- token 加密 payload 必须绑定 token kind 与版本；AAD 必须包含
  purpose/session_id/issuer/subject/session_version/schema_version，**不得**包含组织字段；
- 任何模型/值的 repr 不得泄露 token 明文。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from zhiwei.identity.domain import (
    AuthSession,
    LoginAttempt,
    TokenAAD,
    TokenEnvelopePayload,
)

ACCESS_SENTINEL = "ZW_TEST_ACCESS_TOKEN_A7F3"
REFRESH_SENTINEL = "ZW_TEST_REFRESH_TOKEN_B8E4"


def _valid_session(**overrides: Any) -> AuthSession:
    values: dict[str, Any] = {
        "id": uuid4(),
        "cookie_token_hash": hashlib.sha256(b"cookie-token").hexdigest(),
        "principal_id": uuid4(),
        "issuer": "https://idp.example.com",
        "subject": "alice",
        "encrypted_token_ref": str(uuid4()),
        "csrf_hash": hashlib.sha256(b"csrf-secret").hexdigest(),
        "expires_at": datetime.now(UTC) + timedelta(hours=8),
        "idle_expires_at": datetime.now(UTC) + timedelta(minutes=30),
        "revoked_at": None,
        "version": 1,
        "refresh_state": "idle",
        "refresh_lease_expires_at": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "schema_version": 1,
    }
    values.update(overrides)
    return AuthSession(**values)


def _valid_attempt(**overrides: Any) -> LoginAttempt:
    values: dict[str, Any] = {
        "id": uuid4(),
        "state_hash": hashlib.sha256(b"state").hexdigest(),
        "nonce_hash": hashlib.sha256(b"nonce").hexdigest(),
        "code_verifier": "verifier-material",
        "issuer": "https://idp.example.com",
        "redirect_uri": "https://app.example.com/auth/callback",
        "created_at": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        "consumed_at": None,
        "schema_version": 1,
    }
    values.update(overrides)
    return LoginAttempt(**values)


# --------------------------------------------------------------------------- AuthSession 结构


def test_auth_session_has_no_organization_or_workspace_fields() -> None:
    """principal/session 级契约：组织字段不得出现（总设计 §9.1、DATA_MODEL §2）。"""
    session = _valid_session()
    assert not hasattr(session, "organization_id")
    assert not hasattr(session, "workspace_id")
    with pytest.raises(ValidationError):
        _valid_session(organization_id=uuid4())


def test_auth_session_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _valid_session(version=0)
    with pytest.raises(ValidationError):
        _valid_session(version=-3)


def test_auth_session_refresh_state_is_enum_limited() -> None:
    for invalid in ("refreshing-now", "done", ""):
        with pytest.raises(ValidationError):
            _valid_session(refresh_state=invalid)


def test_auth_session_revoked_clears_refresh_lease() -> None:
    """revoke 后不得保留 refresh lease：revoked + refreshing 状态组合是非法状态。"""
    with pytest.raises(ValidationError):
        _valid_session(
            revoked_at=datetime.now(UTC), refresh_state="refreshing"
        )
    with pytest.raises(ValidationError):
        _valid_session(
            revoked_at=datetime.now(UTC), refresh_lease_expires_at=datetime.now(UTC) + timedelta(seconds=5)
        )


def test_auth_session_cookie_token_hash_is_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        _valid_session(cookie_token_hash="short")
    with pytest.raises(ValidationError):
        _valid_session(cookie_token_hash="z" * 64)
    assert len(_valid_session().cookie_token_hash) == 64


def test_auth_session_expiry_ordering_enforced() -> None:
    """absolute expiry 不得早于 idle expiry——否则 idle 语义被 absolute 吞掉。"""
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        _valid_session(expires_at=now + timedelta(minutes=5), idle_expires_at=now + timedelta(hours=8))


# --------------------------------------------------------------------------- LoginAttempt


def test_login_attempt_state_and_nonce_are_hashed() -> None:
    attempt = _valid_attempt()
    assert len(attempt.state_hash) == 64
    assert len(attempt.nonce_hash) == 64
    with pytest.raises(ValidationError):
        _valid_attempt(state_hash="plain-state")


def test_login_attempt_keeps_code_verifier_for_token_exchange() -> None:
    """PKCE verifier 无法从回调参数恢复，必须留在短期 attempt 供 token exchange 使用。"""
    attempt = _valid_attempt(code_verifier="verifier-material")
    assert attempt.code_verifier == "verifier-material"
    with pytest.raises(ValidationError):
        _valid_attempt(code_verifier="")


def test_login_attempt_consumed_state_is_monotonic() -> None:
    """consumed_at 一旦设置不得回退为 None；expires_at 不得早于 created_at。"""
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        _valid_attempt(consumed_at=now, created_at=now + timedelta(minutes=1))
    with pytest.raises(ValidationError):
        _valid_attempt(expires_at=now - timedelta(minutes=1), created_at=now)


# --------------------------------------------------------------------------- token envelope payload


def test_token_envelope_binds_kind_and_version_in_payload() -> None:
    """token kind / 版本等可能混淆的字段必须绑定在 payload（或 AAD）中。"""
    payload = TokenEnvelopePayload(
        purpose="oidc_session",
        token_kind="access_refresh",
        access_token=ACCESS_SENTINEL,
        refresh_token=REFRESH_SENTINEL,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
        schema_version=1,
    )
    assert payload.token_kind == "access_refresh"
    assert payload.schema_version == 1
    with pytest.raises(ValidationError):
        TokenEnvelopePayload(
            purpose="oidc_session",
            token_kind="unknown_kind",
            access_token=ACCESS_SENTINEL,
            schema_version=1,
        )


def test_token_envelope_payload_repr_never_leaks_tokens() -> None:
    payload = TokenEnvelopePayload(
        purpose="oidc_session",
        token_kind="access_refresh",
        access_token=ACCESS_SENTINEL,
        refresh_token=REFRESH_SENTINEL,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
        schema_version=1,
    )
    assert ACCESS_SENTINEL not in repr(payload)
    assert REFRESH_SENTINEL not in repr(payload)
    assert ACCESS_SENTINEL not in str(payload)
    # 受控解密返回值：显式字段访问仍可取得明文
    assert payload.access_token == ACCESS_SENTINEL
    assert payload.refresh_token == REFRESH_SENTINEL


# --------------------------------------------------------------------------- token AAD


def test_token_aad_contains_exact_bound_fields() -> None:
    """AAD 必须包含 purpose/session_id/issuer/subject/session_version/schema_version。"""
    session_id = uuid4()
    aad = TokenAAD(
        purpose="oidc_session",
        session_id=session_id,
        issuer="https://idp.example.com",
        subject="alice",
        session_version=3,
        schema_version=1,
    )
    payload = json.loads(aad.encode().decode("utf-8"))
    assert set(payload) == {
        "purpose",
        "session_id",
        "issuer",
        "subject",
        "session_version",
        "schema_version",
    }
    assert payload["session_id"] == str(session_id)
    assert payload["session_version"] == 3
    assert payload["schema_version"] == 1


def test_token_aad_must_not_contain_organization() -> None:
    """AAD 明确不得包含 organization_id/workspace_id（首次登录无组织 + 多组织 membership）。"""
    aad = TokenAAD(
        purpose="oidc_session",
        session_id=uuid4(),
        issuer="https://idp.example.com",
        subject="alice",
        session_version=1,
        schema_version=1,
    )
    raw = aad.encode().decode("utf-8")
    assert "organization" not in raw.lower()
    assert "workspace" not in raw.lower()
    with pytest.raises(ValidationError):
        TokenAAD(
            purpose="oidc_session",
            session_id=uuid4(),
            issuer="https://idp.example.com",
            subject="alice",
            session_version=1,
            schema_version=1,
            organization_id=uuid4(),  # type: ignore[call-arg]
        )


def test_token_aad_encode_is_canonical_and_deterministic() -> None:
    aad = TokenAAD(
        purpose="oidc_session",
        session_id=uuid4(),
        issuer="https://idp.example.com",
        subject="alice",
        session_version=2,
        schema_version=1,
    )
    assert aad.encode() == aad.encode()
    # 同一逻辑内容两次构造得到相同字节（RFC 8785 canonical JSON 语义）
    other = TokenAAD(
        purpose="oidc_session",
        session_id=aad.session_id,
        issuer=aad.issuer,
        subject=aad.subject,
        session_version=2,
        schema_version=1,
    )
    assert other.encode() == aad.encode()
