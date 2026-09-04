"""OIDC Authorization Code + PKCE 客户端与服务（S1-T2）。

契约（冻结）：
- 固定配置 issuer 的 discovery/JWKS；HTTP client 必须有 timeout；issuer/endpoint
  绝不从请求参数拼接；
- 验证签名（仅 RS256，用 cryptography 直接验签，不信任 JWT 头里的 alg）、issuer 精确
  匹配、audience/azp、exp/iat、nonce（与 server-side attempt 的 nonce hash 比对）；
  任何失败统一 OIDCValidationError（fail closed）；
- state/nonce/PKCE verifier 只保存在短期 server-side login attempt（state/nonce 存
  SHA-256 hash，verifier 必须可恢复用于 token exchange）；attempt 原子一次性消费；
- token exchange / refresh / revoke 走 authlib AsyncOAuth2Client（HTTP Basic 客户端
  认证 + 表单编码 + 错误归一），传输层复用调用方注入的 httpx transport（测试用
  MockTransport，生产用带超时的真实 transport）；
- 测试使用本地签名 key/JWKS + MockTransport，不访问真实 IdP。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx2 as httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from zhiwei.identity.domain import LoginAttempt

_ATTEMPT_TTL = timedelta(minutes=10)


class OIDCValidationError(RuntimeError):
    """ID token 验证失败（签名 / issuer / audience / azp / exp / iat / nonce / JWKS）。"""


class TokenExchangeError(RuntimeError):
    """token endpoint 失败（exchange / refresh / revoke）。"""

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class OIDCService:
    """OIDC provider 客户端：login attempt、code exchange、refresh、revoke、token 验证。"""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http_client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = http_client is None
        self._discovery_cache: dict[str, Any] | None = None
        self._jwks_cache: tuple[float, dict[str, Any]] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    # ------------------------------------------------------------------ discovery / jwks

    async def _discovery(self) -> dict[str, Any]:
        if self._discovery_cache is not None:
            return self._discovery_cache
        response = await self._http_client.get(
            f"{self._issuer}/.well-known/openid-configuration"
        )
        if response.status_code != 200:
            raise OIDCValidationError("OIDC discovery failed")
        metadata = response.json()
        if metadata.get("issuer") != self._issuer:
            raise OIDCValidationError("OIDC discovery issuer mismatch")
        self._discovery_cache = metadata
        return metadata

    async def _jwks(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._jwks_cache is not None and now - self._jwks_cache[0] < 300:
            return self._jwks_cache[1]
        metadata = await self._discovery()
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise OIDCValidationError("OIDC discovery lacks jwks_uri")
        response = await self._http_client.get(jwks_uri)
        if response.status_code != 200:
            raise OIDCValidationError("JWKS fetch failed")
        jwks = response.json()
        self._jwks_cache = (now, jwks)
        return jwks

    # ------------------------------------------------------------------ id token 验证

    async def validate_id_token(
        self,
        id_token: str,
        *,
        expected_nonce_hash: str,
        expected_issuer: str,
        expected_audience: str,
    ) -> dict[str, Any]:
        """验证签名（RS256）+ iss/aud/azp/exp/iat/nonce；通过后返回 claims。"""
        parts = id_token.split(".")
        if len(parts) != 3:
            raise OIDCValidationError("malformed id token")
        header_b64, payload_b64, signature_b64 = parts
        try:
            header = json.loads(_b64url_decode(header_b64))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OIDCValidationError("malformed id token header") from exc
        if header.get("alg") != "RS256":
            raise OIDCValidationError("only RS256 id tokens are accepted")

        jwks = await self._jwks()
        public_key = self._find_verification_key(jwks, header.get("kid"))
        try:
            public_key.verify(
                _b64url_decode(signature_b64),
                f"{header_b64}.{payload_b64}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, ValueError) as exc:
            raise OIDCValidationError("id token signature verification failed") from exc

        try:
            claims = json.loads(_b64url_decode(payload_b64))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OIDCValidationError("malformed id token payload") from exc

        if claims.get("iss") != expected_issuer:
            raise OIDCValidationError("id token issuer mismatch")
        audience = claims.get("aud")
        if isinstance(audience, list):
            if expected_audience not in audience:
                raise OIDCValidationError("id token audience mismatch")
            if len(audience) > 1 and claims.get("azp") != expected_audience:
                raise OIDCValidationError("id token azp mismatch")
        elif audience != expected_audience:
            raise OIDCValidationError("id token audience mismatch")
        # azp 一旦出现就必须等于 audience（fail closed：单 audience 场景同样拒绝错配）
        azp = claims.get("azp")
        if azp is not None and azp != expected_audience:
            raise OIDCValidationError("id token azp mismatch")

        now = int(time.time())
        exp = claims.get("exp")
        iat = claims.get("iat")
        if not isinstance(exp, int) or exp <= now:
            raise OIDCValidationError("id token expired")
        if not isinstance(iat, int) or iat > now:
            raise OIDCValidationError("id token issued in the future")

        nonce = claims.get("nonce")
        if not isinstance(nonce, str):
            raise OIDCValidationError("id token missing nonce")
        nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(nonce_hash, expected_nonce_hash):
            raise OIDCValidationError("id token nonce mismatch")
        return claims

    def _find_verification_key(
        self, jwks: dict[str, Any], kid: str | None
    ) -> rsa.RSAPublicKey:
        """从 JWKS 找匹配 kid 的 RSA 公钥；未知/缺 kid 时取唯一 RS256 key。

        任何歧义都拒绝：多个 key 且无法按 kid 确定 → 不猜（fail closed）。
        """
        candidates = [
            key
            for key in jwks.get("keys", [])
            if key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and key.get("alg", "RS256") == "RS256"
        ]
        if not candidates:
            raise OIDCValidationError("no usable RSA signing key in JWKS")
        matched = [key for key in candidates if kid is None or key.get("kid") == kid]
        if len(matched) != 1:
            raise OIDCValidationError("cannot resolve a unique signing key from JWKS")
        key = matched[0]
        try:
            n = int.from_bytes(_b64url_decode(key["n"]), "big")
            e = int.from_bytes(_b64url_decode(key["e"]), "big")
            return RSAPublicNumbers(e, n).public_key()
        except (KeyError, ValueError) as exc:
            raise OIDCValidationError("malformed JWK") from exc

    # ------------------------------------------------------------------ login attempt

    async def create_login_attempt(self) -> tuple[str, LoginAttempt]:
        """生成 state/nonce/PKCE verifier，返回 (authorization_url, attempt)。"""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        now = datetime.now(UTC)
        attempt = LoginAttempt(
            id=uuid4(),
            state_hash=hashlib.sha256(state.encode("utf-8")).hexdigest(),
            nonce_hash=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
            code_verifier=verifier,
            issuer=self._issuer,
            redirect_uri=self._redirect_uri,
            created_at=now,
            expires_at=now + _ATTEMPT_TTL,
        )
        metadata = await self._discovery()
        authorization_endpoint = metadata["authorization_endpoint"]
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{authorization_endpoint}?{urlencode(params)}", attempt

    async def exchange_code(
        self, *, code: str, attempt: LoginAttempt
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """用 code + attempt 内 verifier 换 token；返回 (tokens, id_token claims)。"""
        metadata = await self._discovery()
        client = self._oauth_client()
        try:
            token = await client.fetch_token(
                metadata["token_endpoint"],
                code=code,
                code_verifier=attempt.code_verifier,
                grant_type="authorization_code",
            )
        except Exception as exc:
            raise TokenExchangeError("token exchange failed", _error_code(exc)) from exc
        id_token = token.get("id_token")
        if not isinstance(id_token, str):
            raise OIDCValidationError("token response lacks id_token")
        claims = await self.validate_id_token(
            id_token,
            expected_nonce_hash=attempt.nonce_hash,
            expected_issuer=attempt.issuer,
            expected_audience=self._client_id,
        )
        return dict(token), claims

    async def refresh_tokens(self, refresh_token: str) -> dict[str, Any]:
        """grant_type=refresh_token；invalid_grant 等失败抛 TokenExchangeError。"""
        metadata = await self._discovery()
        client = self._oauth_client()
        try:
            token = await client.refresh_token(
                metadata["token_endpoint"], refresh_token=refresh_token
            )
        except Exception as exc:
            raise TokenExchangeError("token refresh failed", _error_code(exc)) from exc
        return dict(token)

    async def revoke_tokens(
        self, access_token: str | None, refresh_token: str | None
    ) -> None:
        """best-effort IdP revocation；失败抛 TokenExchangeError（调用方决定是否吞掉）。"""
        metadata = await self._discovery()
        revocation_endpoint = metadata.get("revocation_endpoint")
        if not isinstance(revocation_endpoint, str):
            return
        client = self._oauth_client()
        try:
            if refresh_token is not None:
                await client.revoke_token(
                    revocation_endpoint,
                    token=refresh_token,
                    token_type_hint="refresh_token",
                )
            elif access_token is not None:
                await client.revoke_token(
                    revocation_endpoint,
                    token=access_token,
                    token_type_hint="access_token",
                )
        except Exception as exc:
            raise TokenExchangeError("token revocation failed", _error_code(exc)) from exc

    def _oauth_client(self) -> AsyncOAuth2Client:
        # authlib 的 httpx 集成自建传输；测试注入的 MockTransport 通过提取的
        # transport 透传（httpx>=0.28 后 transport 为私有属性，pin 版本已固定）。
        return AsyncOAuth2Client(
            self._client_id,
            self._client_secret,
            redirect_uri=self._redirect_uri,
            scope="openid",
            transport=self._http_client._transport,
            timeout=self._http_client.timeout,
        )


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _error_code(exc: Exception) -> str | None:
    error = getattr(exc, "error", None)
    if isinstance(error, str):
        return error
    return None
