"""S1-T2 RED skeleton：OIDC Authorization Code + PKCE 客户端与服务。

契约（冻结）：
- 固定配置 issuer 的 discovery/JWKS；HTTP client 必须有 timeout；issuer/endpoint
  绝不从请求参数拼接；
- 验证签名（仅 RS256）、issuer 精确匹配、audience/azp、exp/iat、nonce（与 server-side
  attempt 的 nonce hash 比对）；任何失败统一 OIDCValidationError（fail closed）；
- state/nonce/PKCE verifier 只保存在短期 server-side login attempt；attempt 原子消费；
- 测试使用本地签名 key/JWKS + MockTransport，不访问真实 IdP。
"""

from __future__ import annotations

from typing import Any

from zhiwei.identity.domain import LoginAttempt


class OIDCValidationError(RuntimeError):
    """ID token 验证失败（签名 / issuer / audience / azp / exp / iat / nonce / JWKS）。"""


class OIDCService:
    """OIDC provider 客户端：login attempt、code exchange、refresh、revoke、token 验证。"""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http_client: Any,
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http_client = http_client

    async def validate_id_token(
        self,
        id_token: str,
        *,
        expected_nonce_hash: str,
        expected_issuer: str,
        expected_audience: str,
    ) -> dict[str, Any]:
        """验证签名 + iss/aud/azp/exp/iat/nonce；通过后返回 claims。"""
        raise NotImplementedError("S1-T2 OIDC id token 验证未实现")

    async def create_login_attempt(self) -> tuple[str, LoginAttempt]:
        """生成 state/nonce/PKCE verifier，持久化 attempt，返回授权 URL。"""
        raise NotImplementedError("S1-T2 OIDC login attempt 未实现")

    async def exchange_code(self, *, code: str, attempt: LoginAttempt) -> tuple[dict[str, Any], dict[str, Any]]:
        """用 code + attempt 内 verifier 换 token；返回 (tokens, id_token claims)。"""
        raise NotImplementedError("S1-T2 OIDC token exchange 未实现")

    async def refresh_tokens(self, refresh_token: str) -> dict[str, Any]:
        """grant_type=refresh_token；invalid_grant 等失败原样抛出。"""
        raise NotImplementedError("S1-T2 OIDC refresh 未实现")

    async def revoke_tokens(
        self, access_token: str | None, refresh_token: str | None
    ) -> None:
        """best-effort IdP revocation；失败不得影响本地 session 状态。"""
        raise NotImplementedError("S1-T2 OIDC revoke 未实现")
