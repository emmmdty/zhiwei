"""S1-T2 RED：OIDC ID token 验证契约（本地签名 key + MockTransport，不访问真实 IdP）。

设计/验收方冻结（A 档）：
- 固定配置 issuer 的 discovery/JWKS；HTTP client 必须带 timeout；
- 验证签名（仅 RS256）、issuer 精确匹配、audience/azp、exp/iat、nonce；
- 任何校验失败统一抛 OIDCValidationError（fail closed），不泄露 claims；
- issuer/endpoint 不得从请求参数拼接。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

import httpx2 as httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from zhiwei.identity.oidc import OIDCService, OIDCValidationError

ISSUER = "https://idp.example.com"
CLIENT_ID = "zhiwei-bff"
CLIENT_SECRET = "ZW_TEST_CLIENT_SECRET_C9D5"
REDIRECT_URI = "https://app.example.com/auth/callback"
SUBJECT = "alice-oidc"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_jwt(claims: dict[str, Any], key: rsa.RSAPrivateKey, *, alg: str = "RS256") -> str:
    header = {"alg": alg, "typ": "JWT", "kid": "test-kid"}
    if alg == "none":
        signing_input = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64url(
            json.dumps(claims, separators=(",", ":")).encode()
        )
        return signing_input + "."
    if alg != "RS256":
        raise AssertionError("test helper only signs RS256")
    payload = header | {"alg": alg, "typ": "JWT", "kid": "test-kid"}
    signing_input = _b64url(json.dumps(payload, separators=(",", ":")).encode()) + "." + _b64url(
        json.dumps(claims, separators=(",", ":")).encode()
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


def _jwk(key: rsa.RSAPrivateKey) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "kid": "test-kid",
        "alg": "RS256",
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def _base_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": SUBJECT,
        "exp": now + 3600,
        "iat": now - 5,
        "nonce": "expected-nonce",
    }
    claims.update(overrides)
    return claims


def _oidc_service(key: rsa.RSAPrivateKey, *, extra_claims: dict[str, Any] | None = None) -> OIDCService:
    """构造 OIDCService：discovery 与 JWKS 全部走 MockTransport，绝不触碰真实网络。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "userinfo_endpoint": f"{ISSUER}/userinfo",
                    "revocation_endpoint": f"{ISSUER}/revoke",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [_jwk(key)]})
        raise AssertionError(f"unexpected discovery request: {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return OIDCService(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        http_client=client,
    )


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.asyncio
async def test_valid_id_token_returns_claims(signing_key: rsa.RSAPrivateKey) -> None:
    service = _oidc_service(signing_key)
    token = _sign_jwt(_base_claims(), signing_key)
    claims = await service.validate_id_token(
        id_token=token,
        expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
        expected_issuer=ISSUER,
        expected_audience=CLIENT_ID,
    )
    assert claims["sub"] == SUBJECT
    assert claims["iss"] == ISSUER


@pytest.mark.asyncio
async def test_invalid_signature_rejected(signing_key: rsa.RSAPrivateKey) -> None:
    service = _oidc_service(signing_key)
    token = _sign_jwt(_base_claims(), signing_key)
    mangled = token[:-3] + ("AA=" if not token.endswith("AA=") else "BB=")
    with pytest.raises(OIDCValidationError):
        await service.validate_id_token(
            id_token=mangled,
            expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
            expected_issuer=ISSUER,
            expected_audience=CLIENT_ID,
        )


@pytest.mark.asyncio
async def test_token_signed_by_other_key_rejected(signing_key: rsa.RSAPrivateKey) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    service = _oidc_service(signing_key)
    token = _sign_jwt(_base_claims(), other)
    with pytest.raises(OIDCValidationError):
        await service.validate_id_token(
            id_token=token,
            expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
            expected_issuer=ISSUER,
            expected_audience=CLIENT_ID,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("alg", ["none", "HS256"])
async def test_forbidden_algorithms_rejected(
    signing_key: rsa.RSAPrivateKey, alg: str
) -> None:
    service = _oidc_service(signing_key)
    if alg == "none":
        token = _sign_jwt(_base_claims(), signing_key, alg="none")
    else:
        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64url(
            json.dumps(_base_claims(), separators=(",", ":")).encode()
        )
        token = signing_input + "." + _b64url(b"fake-hmac")
    with pytest.raises(OIDCValidationError):
        await service.validate_id_token(
            id_token=token,
            expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
            expected_issuer=ISSUER,
            expected_audience=CLIENT_ID,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_overrides", "reason"),
    [
        ({"iss": "https://evil.example.com"}, "issuer"),
        ({"aud": ["other-client"]}, "audience"),
        ({"exp": int(time.time()) - 60}, "expiry"),
        ({"iat": int(time.time()) + 3600}, "iat"),
        ({"nonce": "wrong-nonce"}, "nonce"),
    ],
)
async def test_invalid_claims_rejected(
    signing_key: rsa.RSAPrivateKey, claim_overrides: dict[str, Any], reason: str
) -> None:
    service = _oidc_service(signing_key)
    token = _sign_jwt(_base_claims(**claim_overrides), signing_key)
    with pytest.raises(OIDCValidationError) as exc:
        await service.validate_id_token(
            id_token=token,
            expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
            expected_issuer=ISSUER,
            expected_audience=CLIENT_ID,
        )
    assert reason in str(exc.value).lower() or "token" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_missing_nonce_rejected(signing_key: rsa.RSAPrivateKey) -> None:
    service = _oidc_service(signing_key)
    claims = _base_claims()
    del claims["nonce"]
    token = _sign_jwt(claims, signing_key)
    with pytest.raises(OIDCValidationError):
        await service.validate_id_token(
            id_token=token,
            expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
            expected_issuer=ISSUER,
            expected_audience=CLIENT_ID,
        )


@pytest.mark.asyncio
async def test_multiple_audiences_require_matching_azp(signing_key: rsa.RSAPrivateKey) -> None:
    service = _oidc_service(signing_key)
    with_azp = _sign_jwt(
        _base_claims(aud=[CLIENT_ID, "other-client"], azp=CLIENT_ID), signing_key
    )
    claims = await service.validate_id_token(
        id_token=with_azp,
        expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
        expected_issuer=ISSUER,
        expected_audience=CLIENT_ID,
    )
    assert claims["sub"] == SUBJECT

    wrong_azp = _sign_jwt(
        _base_claims(aud=[CLIENT_ID, "other-client"], azp="other-client"), signing_key
    )
    with pytest.raises(OIDCValidationError):
        await service.validate_id_token(
            id_token=wrong_azp,
            expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
            expected_issuer=ISSUER,
            expected_audience=CLIENT_ID,
        )


@pytest.mark.asyncio
async def test_jwks_fetch_failure_fails_closed(signing_key: rsa.RSAPrivateKey) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "jwks_uri": f"{ISSUER}/jwks",
                    "token_endpoint": f"{ISSUER}/token",
                },
            )
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    service = OIDCService(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        http_client=client,
    )
    token = _sign_jwt(_base_claims(), signing_key)
    with pytest.raises(OIDCValidationError):
        await service.validate_id_token(
            id_token=token,
            expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
            expected_issuer=ISSUER,
            expected_audience=CLIENT_ID,
        )


@pytest.mark.asyncio
async def test_issuer_is_fixed_config_never_request_supplied(signing_key: rsa.RSAPrivateKey) -> None:
    """expected_issuer 来自固定配置；调用方不可能把请求参数变成 issuer。"""
    service = _oidc_service(signing_key)
    token = _sign_jwt(_base_claims(iss="https://attacker.example.com"), signing_key)
    with pytest.raises(OIDCValidationError):
        await service.validate_id_token(
            id_token=token,
            expected_nonce_hash=hashlib.sha256(b"expected-nonce").hexdigest(),
            expected_issuer=ISSUER,
            expected_audience=CLIENT_ID,
        )
