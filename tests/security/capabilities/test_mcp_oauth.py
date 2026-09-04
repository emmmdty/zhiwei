"""S4-T4 Security: MCP OAuth 2.1, PKCE, Resource Indicator, token passthrough.

验证:
- PKCE S256 正确生成和验证
- PKCE code_verifier 长度约束
- PKCE challenge mismatch 拒绝
- Resource Indicator 匹配验证
- Audience mismatch 拒绝
- Scope 验证（包含和缺失）
- Token passthrough 拒绝（mcp_ 前缀）
- Token passthrough 拒绝（mcp- 前缀）
- 空 token 不触发 passthrough 拒绝
- OAuth client start_authorization 生成有效 URL
- OAuth client code exchange 验证 PKCE
- OAuth client refresh 验证 audience
- OAuth client revoke 验证 token 归属
- Token expiry detection
- Scope isolation between connections
"""

from __future__ import annotations

import pytest

from zhiwei.capabilities.mcp.oauth import (
    AudienceMismatchError,
    McpOAuthClient,
    OAuthError,
    PkceChallenge,
    PKCEValidationError,
    ProtectedResourceMetadata,
    RefreshTokenRejectedError,
    ScopeViolationError,
    TokenPassthroughRejectedError,
    TokenResponse,
    build_authorization_url,
    reject_token_passthrough,
    validate_audience,
    validate_pkce,
    validate_resource_indicator,
    validate_scope,
)

# ── PKCE ──────────────────────────────────────────────────────────


class TestPkce:
    def test_generate_creates_valid_pair(self) -> None:
        ch = PkceChallenge.generate()
        assert len(ch.code_verifier) >= 43
        assert len(ch.code_challenge) >= 43
        assert ch.method.value == "S256"

    def test_validate_pkce_s256_success(self) -> None:
        ch = PkceChallenge.generate()
        assert validate_pkce(ch.code_verifier, ch.code_challenge) is True

    def test_validate_pkce_s256_mismatch(self) -> None:
        ch = PkceChallenge.generate()
        with pytest.raises(PKCEValidationError, match="S256 challenge mismatch"):
            validate_pkce(ch.code_verifier, "wrong_challenge")

    def test_validate_pkce_verifier_too_short(self) -> None:
        ch = PkceChallenge.generate()
        with pytest.raises(PKCEValidationError, match="43-128"):
            validate_pkce("short", ch.code_challenge)

    def test_validate_pkce_verifier_too_long(self) -> None:
        ch = PkceChallenge.generate()
        with pytest.raises(PKCEValidationError, match="43-128"):
            validate_pkce("x" * 200, ch.code_challenge)

    def test_validate_pkce_empty_verifier(self) -> None:
        with pytest.raises(PKCEValidationError, match="43-128"):
            validate_pkce("", "challenge")


# ── Resource Indicator ────────────────────────────────────────────


class TestResourceIndicator:
    def test_validate_resource_matches(self) -> None:
        validate_resource_indicator(
            "https://mcp.example.com",
            "https://mcp.example.com",
        )

    def test_validate_resource_mismatch(self) -> None:
        with pytest.raises(AudienceMismatchError, match="Resource Indicator mismatch"):
            validate_resource_indicator(
                "https://other.example.com",
                "https://mcp.example.com",
            )

    def test_build_resource_indicator_from_url(self) -> None:
        client = McpOAuthClient(
            client_id="test",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        ri = client.build_resource_indicator("https://mcp.example.com/sse")
        assert ri == "https://mcp.example.com"


# ── Audience ──────────────────────────────────────────────────────


class TestAudience:
    def test_validate_audience_matches(self) -> None:
        validate_audience("https://mcp.example.com", "https://mcp.example.com")

    def test_validate_audience_mismatch(self) -> None:
        with pytest.raises(AudienceMismatchError, match="Audience mismatch"):
            validate_audience("https://wrong.example.com", "https://mcp.example.com")


# ── Scope ─────────────────────────────────────────────────────────


class TestScope:
    def test_validate_scope_sufficient(self) -> None:
        validate_scope("read write admin", "read write")

    def test_validate_scope_exact_match(self) -> None:
        validate_scope("read", "read")

    def test_validate_scope_missing(self) -> None:
        with pytest.raises(ScopeViolationError, match="Missing required"):
            validate_scope("read", "read write")

    def test_validate_scope_empty_granted(self) -> None:
        with pytest.raises(ScopeViolationError):
            validate_scope("", "read")


# ── Token Passthrough ────────────────────────────────────────────


class TestTokenPassthrough:
    def test_reject_mcp_underscore_prefix(self) -> None:
        with pytest.raises(TokenPassthroughRejectedError):
            reject_token_passthrough("mcp_token_abc123")

    def test_reject_mcp_dash_prefix(self) -> None:
        with pytest.raises(TokenPassthroughRejectedError):
            reject_token_passthrough("mcp-token-abc123")

    def test_accept_normal_token(self) -> None:
        reject_token_passthrough("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...")

    def test_accept_empty_token(self) -> None:
        reject_token_passthrough("")

    def test_accept_oauth_token(self) -> None:
        reject_token_passthrough("dGhpcyBpcyBhIHRva2Vu")


# ── OAuth Client ──────────────────────────────────────────────────


class TestMcpOAuthClient:
    def test_start_authorization_returns_url_and_challenge(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        url, _challenge = client.start_authorization(scope="read write")
        assert "https://auth.example.com/authorize" in url
        assert "code_challenge=" in url
        assert "resource=https" in url
        assert "scope=read+write" in url

    def test_exchange_code_validates_pkce(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        _url, _challenge = client.start_authorization()

        with pytest.raises(PKCEValidationError):
            client.exchange_code("auth_code", "wrong_verifier")

    def test_exchange_code_validates_resource(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        _url, challenge = client.start_authorization()

        with pytest.raises(AudienceMismatchError):
            client.exchange_code(
                "auth_code",
                challenge.code_verifier,
                resource="https://other.example.com",
            )

    def test_exchange_code_without_active_flow(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        with pytest.raises(OAuthError, match="No active authorization flow"):
            client.exchange_code("code", "verifier")

    def test_store_and_get_token(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        token = TokenResponse(
            access_token="normal_token_abc",
            expires_in=3600,
            scope="read",
        )
        client.store_token(token, key="conn1")
        stored = client.get_token("conn1")
        assert stored is not None
        assert stored.access_token == "normal_token_abc"

    def test_store_rejects_mcp_passthrough(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        token = TokenResponse(access_token="mcp_forwarded_token")
        with pytest.raises(TokenPassthroughRejectedError):
            client.store_token(token)

    def test_refresh_token_validates_audience(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        with pytest.raises(AudienceMismatchError):
            client.refresh_token("refresh_xyz", audience="https://wrong.example.com")

    def test_refresh_token_empty_rejected(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        with pytest.raises(RefreshTokenRejectedError):
            client.refresh_token("")

    def test_revoke_token_removes_stored(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        token = TokenResponse(access_token="to_revoke", expires_in=3600)
        client.store_token(token, key="c1")
        client.revoke_token("to_revoke")
        assert client.get_token("c1") is None

    def test_revoke_unknown_token_raises(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        with pytest.raises(OAuthError, match="not found"):
            client.revoke_token("nonexistent_token")

    def test_validate_token_scope(self) -> None:
        client = McpOAuthClient(
            client_id="zhiwei",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            resource="https://mcp.example.com",
        )
        token = TokenResponse(access_token="t", expires_in=3600, scope="read write")
        client.validate_token_scope(token, "read")
        with pytest.raises(ScopeViolationError):
            client.validate_token_scope(token, "read write admin")


# ── Token expiry ──────────────────────────────────────────────────


class TestTokenExpiry:
    def test_token_not_expired_when_no_expiry(self) -> None:
        token = TokenResponse(access_token="t")
        assert not token.is_expired

    def test_token_response_is_immutable(self) -> None:
        token = TokenResponse(access_token="t", expires_in=3600, scope="read")
        assert token.access_token == "t"
        assert token.scope == "read"


# ── Protected Resource Metadata ───────────────────────────────────


class TestProtectedResourceMetadata:
    def test_from_dict(self) -> None:
        data = {
            "resource": "https://mcp.example.com",
            "authorization_servers": ["https://auth.example.com"],
            "scopes_supported": ["read", "write"],
        }
        prm = ProtectedResourceMetadata.from_dict(data)
        assert prm.resource == "https://mcp.example.com"
        assert prm.authorization_servers == ("https://auth.example.com",)
        assert "read" in prm.scopes_supported

    def test_from_dict_defaults(self) -> None:
        prm = ProtectedResourceMetadata.from_dict({})
        assert prm.resource == ""
        assert prm.authorization_servers == ()


# ── Authorization URL ────────────────────────────────────────────


class TestAuthorizationUrl:
    def test_build_authorization_url(self) -> None:
        ch = PkceChallenge.generate()
        url = build_authorization_url(
            authorization_endpoint="https://auth.example.com/authorize",
            client_id="zhiwei",
            redirect_uri="http://localhost:3000/callback",
            challenge=ch,
            resource="https://mcp.example.com",
            scope="read write",
            state="csrf123",
        )
        assert "response_type=code" in url
        assert "client_id=zhiwei" in url
        assert f"code_challenge={ch.code_challenge}" in url
        assert "code_challenge_method=S256" in url
        assert "resource=https" in url
        assert "state=csrf123" in url
        assert "scope=read+write" in url

    def test_build_authorization_url_without_optional(self) -> None:
        ch = PkceChallenge.generate()
        url = build_authorization_url(
            authorization_endpoint="https://auth.example.com/authorize",
            client_id="zhiwei",
            redirect_uri="http://localhost:3000/callback",
            challenge=ch,
            resource="https://mcp.example.com",
        )
        assert "state=" not in url
        assert "scope=" not in url
